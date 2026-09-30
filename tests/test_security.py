from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from concurrent.futures import ThreadPoolExecutor
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import transcribe
from transcribe import auphonic, cli
from test_package import build
from test_transcribe_cli import FakeAuphonicClient, sample_transcript


class DownloadSecurityTests(unittest.TestCase):
    def test_initial_authentication_is_origin_scoped(self):
        for url, authenticated in [("https://auphonic.com/a", True),
                                   ("https://auphonic.com:443/a", True),
                                   ("https://auphonic.com:444/a", False),
                                   ("https://auphonic.com:0/a", False),
                                   ("https://storage.example/a?signature=a%20b", False)]:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                opener = MagicMock()
                response = opener.open.return_value.__enter__.return_value
                response.headers = {}
                response.read.return_value = b""
                with patch.object(auphonic.urllib.request, "build_opener", return_value=opener):
                    auphonic.AuphonicClient("secret").download(url, Path(temporary) / "a")
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_header("Authorization"), "Bearer secret" if authenticated else None)
                self.assertEqual(request.full_url, url)

    def test_unsafe_initial_urls_fail_before_network_access(self):
        for url in ["http://auphonic.com/a", "http://external.example/a",
                    "https://user:password@auphonic.com/a", "https://auphonic.com:bad/a",
                    "https://auphonic.com:99999/a", "file:///tmp/a"]:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                with patch.object(auphonic.urllib.request, "build_opener") as opener:
                    with self.assertRaises(auphonic.AuphonicError):
                        auphonic.AuphonicClient("secret").download(url, Path(temporary) / "a")
                    opener.return_value.open.assert_not_called()

    def test_redirects_cannot_downgrade_or_restore_stripped_auth(self):
        handler = auphonic.CrossOriginAuthStripper()
        request = auphonic.urllib.request.Request("https://auphonic.com/a", headers={"Authorization": "Bearer secret"})
        for url in ["http://auphonic.com/a", "https://user@auphonic.com/a"]:
            with self.assertRaises(auphonic.AuphonicError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        external = handler.redirect_request(request, None, 302, "Found", {}, "https://external.example/a")
        returned = handler.redirect_request(external, None, 302, "Found", {}, "https://auphonic.com/a")
        self.assertIsNone(returned.get_header("Authorization"))


class JsonApiSecurityTests(unittest.TestCase):
    def test_unsafe_api_bases_fail_before_network(self):
        for base in ["http://auphonic.com", "file:///tmp/api",
                     "https://user:password@auphonic.com", "https://auphonic.com:bad",
                     "https://auphonic.com:99999", "https://auphonic.com?key=a",
                     "https://auphonic.com#fragment"]:
            with self.subTest(base=base), patch.object(auphonic.urllib.request, "build_opener") as opener:
                with self.assertRaises(auphonic.AuphonicError):
                    auphonic.AuphonicClient("dummy-key", api_base=base)
                opener.assert_not_called()

    def test_json_api_uses_safe_opener_for_get_and_post(self):
        for method, payload in [("GET", None), ("POST", {"algorithms": {"leveler": True}})]:
            with self.subTest(method=method):
                opener = MagicMock()
                opener.open.return_value.__enter__.return_value.read.return_value = b'{"status_code": 200, "data": {"uuid": "synthetic"}}'
                with patch.object(auphonic.urllib.request, "build_opener", return_value=opener) as build_opener, patch.object(auphonic.urllib.request, "urlopen", side_effect=AssertionError("unsafe default opener")):
                    result = auphonic.AuphonicClient("dummy-key")._request_json(method, "/api/productions.json", payload)
                self.assertEqual(result, {"uuid": "synthetic"})
                handler = build_opener.call_args.args[0]
                self.assertIsInstance(handler, auphonic.CrossOriginAuthStripper)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_header("Authorization"), "Bearer dummy-key")
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.data, json.dumps(payload).encode() if payload is not None else None)
                # Exercise the actual handler bound to API calls, including ports and a return hop.
                same = handler.redirect_request(request, None, 302, "Found", {}, "https://auphonic.com:443/api/next.json")
                self.assertEqual(same.get_header("Authorization"), "Bearer dummy-key")
                for other in ["https://external.example/api/next.json", "https://auphonic.com:444/api/next.json", "https://auphonic.com:0/api/next.json"]:
                    redirected = handler.redirect_request(request, None, 302, "Found", {}, other)
                    self.assertIsNone(redirected.get_header("Authorization"))
                    returned = handler.redirect_request(redirected, None, 302, "Found", {}, "https://auphonic.com/api/next.json")
                    self.assertIsNone(returned.get_header("Authorization"))
                for unsafe in ["http://auphonic.com/api/next.json", "http://external.example/api/next.json", "https://user@auphonic.com/api/next.json"]:
                    with self.assertRaises(auphonic.AuphonicError):
                        handler.redirect_request(request, None, 302, "Found", {}, unsafe)

    def test_real_opener_redirect_chain_never_restores_credentials(self):
        from email.message import Message
        from urllib.response import addinfourl

        seen = []
        routes = ["https://auphonic.com/api/production/synthetic.json",
                  "https://auphonic.com:443/api/next.json",
                  "https://storage.example/api/next.json",
                  "https://auphonic.com/api/final.json"]

        class OfflineHTTPSHandler(auphonic.urllib.request.HTTPSHandler):
            def https_open(self, request):
                seen.append((request.full_url, request.get_header("Authorization")))
                index = routes.index(request.full_url)
                headers = Message()
                if index + 1 < len(routes):
                    headers["Location"] = routes[index + 1]
                    response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                    response.msg = "Found"
                else:
                    response = addinfourl(io.BytesIO(b'{"status_code": 200, "data": {"uuid": "synthetic"}}'), headers, request.full_url, 200)
                    response.msg = "OK"
                return response

        # Only transport is replaced; urllib's opener and redirect dispatch are real.
        with patch.object(auphonic.urllib.request, "HTTPSHandler", OfflineHTTPSHandler):
            result = auphonic.AuphonicClient("dummy-key").details("synthetic")
        self.assertEqual(result, {"uuid": "synthetic"})
        self.assertEqual(seen, list(zip(routes, ["Bearer dummy-key", "Bearer dummy-key", None, None])))

    def test_mutated_base_cannot_send_plaintext_json_or_upload_credentials(self):
        client = auphonic.AuphonicClient("dummy-key")
        client.api_base = "http://127.0.0.1:9999"
        with patch.object(auphonic.urllib.request, "build_opener") as opener, patch.object(auphonic.http.client, "HTTPConnection") as connection:
            with self.assertRaises(auphonic.AuphonicError):
                client.details("synthetic")
            with self.assertRaises(auphonic.AuphonicError):
                client.upload("synthetic", Path("does-not-exist.wav"))
            opener.assert_not_called()
            connection.assert_not_called()


class PackageSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "package"
        self.directory.mkdir()
        build(self.directory)
        self.sidecar_path = self.directory / "call-package.json"
        self.sidecar = json.loads(self.sidecar_path.read_text())
        second = self.directory / "call-second-raw.json"
        second.write_bytes((self.directory / self.sidecar["files"]["raw"]["filename"]).read_bytes())
        self.sidecar["files"]["second_raw"] = {"filename": second.name, "sha256": self.sidecar["files"]["raw"]["sha256"]}
        self.sidecar_path.write_text(json.dumps(self.sidecar))
        self.outside = self.root / "outside"
        self.outside.write_text("untouched")

    def test_all_references_are_checked_before_loading(self):
        for role in ["source", *self.sidecar["files"]]:
            if role != "source" and self.sidecar["files"][role] is None:
                continue
            for filename in [str(self.outside), "../outside", "nested/../call-raw.json", "..", "", r"..\outside"]:
                data = json.loads(json.dumps(self.sidecar))
                entry = data["source"] if role == "source" else data["files"][role]
                entry["filename"] = filename
                self.sidecar_path.write_text(json.dumps(data))
                with self.subTest(role=role, filename=filename), self.assertRaises(transcribe.PackageError):
                    transcribe.Package.load(self.directory)

    def test_external_symlinks_in_every_role_and_sidecar_are_refused(self):
        for role in ["source", *self.sidecar["files"]]:
            entry = self.sidecar["source"] if role == "source" else self.sidecar["files"][role]
            if entry is None:
                continue
            path = self.directory / entry["filename"]
            content = path.read_bytes()
            path.unlink()
            path.symlink_to(self.outside)
            with self.subTest(role=role), self.assertRaises(transcribe.PackageError):
                transcribe.Package.load(self.directory)
            path.unlink()
            path.write_bytes(content)
        self.sidecar_path.unlink()
        self.sidecar_path.symlink_to(self.outside)
        with self.assertRaises(transcribe.PackageError):
            transcribe.Package.load(self.directory)
        self.assertEqual(self.outside.read_text(), "untouched")

    def test_second_response_and_late_write_symlinks_are_checked(self):
        package = transcribe.Package.load(self.directory)
        package.sidecar["files"]["second_raw"] = {"filename": "../outside", "sha256": "irrelevant"}
        with self.assertRaises(transcribe.PackageError):
            package.second_transcript()
        del package.sidecar["files"]["second_raw"]
        speakers_before = package.path("speakers").read_bytes()
        path = package.path("markdown")
        path.unlink()
        path.symlink_to(self.outside)
        with self.assertRaises(transcribe.PackageError):
            package.assign({"speaker_0": "Name"})
        self.assertEqual(package.path("speakers").read_bytes(), speakers_before)
        self.assertEqual(package.people(), {})
        self.assertEqual(self.outside.read_text(), "untouched")

    def test_internal_symlinks_and_symlinked_package_root_work(self):
        raw = self.directory / self.sidecar["files"]["raw"]["filename"]
        target = self.directory / "kept-raw.json"
        raw.rename(target)
        raw.symlink_to(target.name)
        alias = self.root / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        package = transcribe.Package.load(alias)
        self.assertEqual(package.directory, self.directory.resolve())
        self.assertTrue(package.transcript()["words"])

    def test_dangling_external_links_and_symlink_loops_are_refused(self):
        raw = self.directory / self.sidecar["files"]["raw"]["filename"]
        raw.unlink()
        for target in [self.root / "missing", raw.name]:
            raw.symlink_to(target)
            with self.assertRaises(transcribe.PackageError):
                transcribe.Package.load(self.directory)
            raw.unlink()

    def test_identity_only_source_records_remain_loadable(self):
        del self.sidecar["source"]["filename"]
        self.sidecar_path.write_text(json.dumps(self.sidecar))
        package = transcribe.Package.load(self.directory)
        self.assertEqual(package.source_sha256, self.sidecar["source"]["sha256"])


class ExplicitCredentialTests(unittest.TestCase):
    def test_auphonic_keys_and_clients_work_for_new_and_resumed_productions(self):
        for resume in [False, True]:
            for use_client in [False, True]:
                with self.subTest(resume=resume, client=use_client), tempfile.TemporaryDirectory() as temporary:
                    source = Path(temporary) / "call.mp3"
                    source.write_bytes(b"synthetic")
                    cleanup = FakeAuphonicClient()
                    recognition = Mock()
                    recognition.transcribe.return_value = sample_transcript()
                    kwargs = {"auphonic_client": cleanup} if use_client else {"auphonic_api_key": "cleanup-secret"}
                    flags = ["--leveling", "on", "--second-pass", "off"]
                    if resume:
                        flags += ["--resume-auphonic-production", "production-test"]
                    with (patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("fallback")),
                          patch.object(cli.auphonic, "AuphonicClient", return_value=cleanup) as constructor):
                        package = transcribe.transcribe_file(source, *flags, elevenlabs_client=recognition, **kwargs)
                    self.assertTrue(package.sidecar["leveling"]["applied"])
                    if use_client:
                        constructor.assert_not_called()
                    else:
                        constructor.assert_called_once_with("cleanup-secret")
                    self.assertNotIn("cleanup-secret", json.dumps(package.sidecar))

    def test_concurrent_calls_keep_clients_and_credentials_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            barrier = threading.Barrier(2)
            before = dict(os.environ)

            def run(index):
                source = Path(temporary) / f"call-{index}.mp3"
                source.write_bytes(b"synthetic")
                client = Mock()

                def recognize(*args, **kwargs):
                    barrier.wait(timeout=10)
                    document = sample_transcript()
                    document["transcription_id"] = f"client-{index}"
                    return document

                client.transcribe.side_effect = recognize
                return transcribe.transcribe_file(source, "--second-pass", "off", elevenlabs_client=client)

            with ThreadPoolExecutor(max_workers=2) as pool:
                packages = list(pool.map(run, [0, 1]))
            self.assertEqual([p.transcript()["transcription_id"] for p in packages], ["client-0", "client-1"])
            self.assertEqual(dict(os.environ), before)

    def test_keys_and_clients_bypass_loaders_and_do_not_enter_artifacts(self):
        for use_client in [False, True]:
            with self.subTest(client=use_client), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "call.mp3"
                source.write_bytes(b"synthetic")
                client = Mock()
                client.transcribe.return_value = sample_transcript()
                kwargs = {"elevenlabs_client": client} if use_client else {"elevenlabs_api_key": "explicit-secret"}
                before = dict(os.environ)
                with (patch.object(cli.elevenlabs, "load_api_key", side_effect=AssertionError("fallback")),
                      patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("unused")),
                      patch.object(cli.elevenlabs, "ElevenLabsClient", return_value=client) as constructor):
                    package = transcribe.transcribe_file(source, "--second-pass", "on", **kwargs)
                self.assertEqual(client.transcribe.call_count, 2)
                if use_client:
                    constructor.assert_not_called()
                else:
                    constructor.assert_called_once_with("explicit-secret")
                self.assertEqual(dict(os.environ), before)
                self.assertNotIn("explicit-secret", json.dumps(package.sidecar))

    def test_conflicts_and_empty_keys_fail_before_writes(self):
        for kwargs in [{"elevenlabs_api_key": ""}, {"auphonic_api_key": ""},
                       {"elevenlabs_api_key": "key", "elevenlabs_client": Mock()},
                       {"auphonic_api_key": "key", "auphonic_client": Mock()}]:
            with self.subTest(kwargs=list(kwargs)), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "call.mp3"
                source.write_bytes(b"synthetic")
                with self.assertRaises(ValueError):
                    transcribe.transcribe_file(source, **kwargs)
                self.assertEqual(list(Path(temporary).iterdir()), [source])

    def test_client_failure_leaves_environment_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "call.mp3"
            source.write_bytes(b"synthetic")
            before = dict(os.environ)
            client = Mock()
            client.transcribe.side_effect = RuntimeError("offline failure")
            with redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
                transcribe.transcribe_file(source, "--second-pass", "off", elevenlabs_client=client)
            self.assertEqual(dict(os.environ), before)
            self.assertEqual(list(Path(temporary).iterdir()), [source])
