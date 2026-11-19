"""Synthetic, provider-free command integration and protocol regressions."""

import fcntl
import hashlib
import json
import os
import ssl
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conric import SchemaError

import transcribe
from transcribe import cli, elevenlabs, http_settings, media, providers
from transcribe.config import validate_config
from transcribe.package import PackageError


HELPER = Path(__file__).resolve().with_name("command_helper.py")


class CommandProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.wav"
        self.source.write_bytes(b"fake audio for command fixture")

    def command(self, *args, **options):
        return {"argv": [sys.executable, str(HELPER), *args], "egress": "local", **options}

    def settings(self, **sections):
        return validate_config({
            "local_only": True,
            "paths": {"output_dir": str(self.root / "out"), "staging_dir": str(self.root / "stage"),
                      "work_dir": str(self.root / "work")},
            "transcription": {"provider": "command", "command": self.command(), "model": "first"},
            "diarization": {"provider": "command", "command": self.command()},
            "second_pass": {"mode": "on"},
            **sections,
        })

    def test_deterministic_repeat_preserves_raw_and_verified_speaker_projection(self):
        with patch.dict(os.environ, {"QUILLRIC_UNLISTED_TEST": "never passed"}):
            package = transcribe.transcribe_file(self.source, settings=self.settings())
        self.assertEqual(package.transcript()["words"][0]["speaker_id"], "raw")
        self.assertEqual(package.sidecar["source"]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertFalse(package.sidecar["second_pass"]["independent"])
        self.assertNotIn("agreement", package.sidecar["second_pass"])
        self.assertIsNone(package.second_transcript())
        self.assertEqual(package.merged_turns()[0]["speakers"], ("speaker_0",))
        package.assign({"speaker_0": "First person"})
        self.assertIn("First person (speaker_0)", package.render())
        self.assertIn("skipped", package.render())
        self.assertEqual(list((self.root / "stage").iterdir()), [])
        self.assertEqual(list((self.root / "work").iterdir()), [])
        package.path("diarization").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(PackageError, "changed"):
            package.merged_turns()

    def test_distinct_model_alternatives_include_unicode(self):
        settings = self.settings(second_pass={"mode": "on", "model": "second"})
        package = transcribe.transcribe_file(self.source, settings=settings)
        comparison = package.sidecar["second_pass"]
        self.assertEqual(comparison["agreement"], 0)
        self.assertEqual(comparison["basis"], "distinct_configuration")
        self.assertEqual(package.second_transcript()["words"][0]["text"], "再见")
        self.assertIn("{你好 | 再见}", package.render())

    def test_command_failures_leave_no_package(self):
        for mode, message in [("overrun", "output limit"), ("timeout", "timeout"),
                              ("bad", "invalid response"), ("error", "refused"),
                              ("nonzero", "unsuccessful exit"), ("wrong-schema", "invalid response"),
                              ("unordered", "invalid word timing"), ("positive-logprob", "invalid word")]:
            with self.subTest(mode=mode):
                limit = {"timeout_seconds": 0.1 if mode == "timeout" else 5}
                settings = self.settings(transcription={"provider": "command",
                                                         "command": self.command(mode, **limit)})
                with (patch.object(providers, "STDOUT_LIMIT", 2048),
                      self.assertRaisesRegex(providers.ProviderError, message)):
                    transcribe.transcribe_file(self.source, settings=settings)
                self.assertFalse((self.root / "out").exists())
                self.assertEqual(list((self.root / "stage").iterdir()), [])

    def test_command_kills_grandchild_holding_stdout(self):
        spec = providers.Command((sys.executable, str(HELPER), "grandchild"), timeout_seconds=2)
        runner = providers.CommandTranscription(spec, self.root, providers.work_environment(self.root, None))
        with self.assertRaisesRegex(providers.ProviderError, "timeout"):
            runner.transcribe(self.source, "first", None)
        with (self.root / "grandchild.lock").open("r") as lease:
            cut_off = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() >= cut_off:
                        self.fail("grandchild still holds its lock")
                    time.sleep(0.01)
                else:
                    break

    def test_process_group_permission_error_keeps_categorical_failure(self):
        spec = providers.Command((sys.executable, str(HELPER), "nonzero"))
        runner = providers.CommandTranscription(spec, self.root, providers.work_environment(self.root, None))
        with (patch.object(providers.os, "killpg", side_effect=PermissionError),
              self.assertRaisesRegex(providers.ProviderError, "unsuccessful exit")):
            runner.transcribe(self.source, "first", None)

    def test_pass_env_is_explicit_and_path_is_absent(self):
        command = self.command("check-env", pass_env=["QUILLRIC_PASSED_TEST"])
        settings = self.settings(transcription={"provider": "command", "command": command})
        with patch.dict(os.environ, {"QUILLRIC_PASSED_TEST": "listed", "QUILLRIC_UNLISTED_TEST": "ignored"}):
            package = transcribe.transcribe_file(self.source, settings=settings)
        self.assertEqual(package.transcript()["language_code"], "zh")

    def test_assigned_word_ordinals_must_match_the_request(self):
        settings = self.settings(diarization={"provider": "command",
                                              "command": self.command("wrong-assignment")})
        with self.assertRaisesRegex(providers.ProviderError, "invalid assignments"):
            transcribe.transcribe_file(self.source, settings=settings)
        self.assertFalse((self.root / "out").exists())

    def test_unknown_and_incompatible_providers_fail_schema_validation(self):
        for section in ("diarization", "second_pass"):
            with self.subTest(section=section), self.assertRaises(SchemaError):
                self.settings(**{section: {"provider": "unknown"}})
        with self.assertRaises(SchemaError):
            self.settings(second_pass={"provider": "elevenlabs"})

    def test_hand_built_settings_are_checked_before_source_access(self):
        valid = self.settings()
        invalid = [
            (replace(valid, transcription="unknown"), "unknown configured provider"),
            (replace(valid, second_provider="elevenlabs"), "requires an ElevenLabs first pass"),
            (replace(valid, diarization_command=None), "diarization provider and command"),
            (replace(valid, transcription_command=providers.Command(("relative",))), "absolute executable"),
            (replace(valid, transcription_command=replace(valid.transcription_command, max_concurrency=0)),
             "limits are invalid"),
        ]
        for settings, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(cli.PipelineError, message):
                transcribe.transcribe_file(self.root / "absent.wav", settings=settings)
        self.assertFalse((self.root / "stage").exists())

    def test_explicit_mode_without_reference_ignores_ambient_keys(self):
        (self.root / ".env").write_text("ELEVENLABS_API_KEY=synthetic\n", encoding="utf-8")
        settings = validate_config({"second_pass": {"mode": "off"}})
        with (patch.dict(os.environ, {"ELEVENLABS_API_KEY": "synthetic"}),
              patch.object(elevenlabs, "load_api_key", side_effect=AssertionError("ambient lookup")),
              patch.object(elevenlabs, "ElevenLabsClient", side_effect=AssertionError("client")),
              self.assertRaisesRegex(cli.PipelineError, "requires a secret reference")):
            transcribe.transcribe_file(self.source, settings=settings)
        self.assertFalse(self.source.with_suffix("").exists())

    def test_explicit_tls_loads_platform_cas(self):
        with patch.object(ssl.SSLContext, "load_default_certs", autospec=True,
                          side_effect=ssl.SSLContext.load_default_certs) as load_default:
            context = http_settings.tls_context(None)
        load_default.assert_called_once_with(context)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_diarization_off_keeps_raw_labels_but_does_not_project_them(self):
        settings = self.settings(diarization={"provider": "off"})
        package = transcribe.transcribe_file(self.source, settings=settings)
        self.assertEqual(package.transcript()["words"][0]["speaker_id"], "raw")
        self.assertEqual(package.merged_turns()[0]["speakers"], ("unassigned",))
        self.assertIn("diarization: false", package.render())

    def test_elevenlabs_can_use_command_diarization(self):
        response = {"words": [{"type": "word", "text": "Hello", "start": 0, "end": 0.4}]}
        client = Mock()
        client.transcribe.return_value = response
        settings = self.settings(transcription={"provider": "elevenlabs"}, local_only=False,
                                 second_pass={"mode": "off"})
        package = transcribe.transcribe_file(self.source, settings=settings, elevenlabs_client=client)
        self.assertIs(client.transcribe.call_args.kwargs["diarize"], False)
        self.assertEqual(package.transcript(), response)
        self.assertEqual(package.merged_turns()[0]["speakers"], ("speaker_0",))

    def test_ffmpeg_leveling_refuses_auphonic_options(self):
        with self.assertRaises(SchemaError):
            self.settings(leveling={"provider": "ffmpeg", "algorithms": {}})
        settings = self.settings(leveling={"provider": "ffmpeg", "mode": "on"})
        with self.assertRaisesRegex(cli.PipelineError, "Auphonic options require"):
            transcribe.transcribe_file(self.root / "absent.wav", "--auphonic-set", "leveler=true",
                                       settings=settings)

    def test_timeout_change_does_not_change_comparison_basis(self):
        first = self.command(deterministic=False)
        second = self.command(deterministic=False, timeout_seconds=20)
        settings = self.settings(transcription={"provider": "command", "command": first},
                                 second_pass={"mode": "on", "command": second})
        package = transcribe.transcribe_file(self.source, settings=settings)
        self.assertEqual(package.sidecar["second_pass"]["basis"], "repeat")

    def test_egress_and_unsupported_extras_fail_before_source_access(self):
        invalid = [
            (self.settings(transcription={"provider": "command", "command": {
                "argv": [sys.executable, str(HELPER)]}}), "local-only"),
            (self.settings(transcription={"provider": "command", "command": self.command(),
                                          "no_verbatim": True}), "clean_transcript"),
        ]
        for settings, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(cli.PipelineError, message):
                transcribe.transcribe_file(self.root / "absent.wav", settings=settings)
        self.assertFalse((self.root / "stage").exists())

    def test_plaintext_requires_explicit_loopback_fake(self):
        for base, allowed in [("http://127.0.0.1:9000", False), ("http://192.0.2.3", True),
                              ("https://user@example.invalid", False)]:
            with self.subTest(base=base), self.assertRaises(elevenlabs.TranscriptionError):
                elevenlabs.ElevenLabsClient("synthetic", base, allow_insecure_local_http=allowed)
        elevenlabs.ElevenLabsClient("synthetic", "http://127.0.0.1:9000", allow_insecure_local_http=True)

    def test_standalone_vendor_video_upload_uses_original_container(self):
        source = self.root / "clip.mp4"
        source.write_bytes(b"standalone video fixture")

        def transcribe_original(audio, *_args, **_kwargs):
            self.assertEqual(audio.name, source.name)
            self.assertEqual(audio.read_bytes(), source.read_bytes())
            return {"words": []}

        client = Mock()
        client.transcribe.side_effect = transcribe_original
        with patch.object(cli.media, "executable", side_effect=AssertionError("media tool was invoked")):
            package = transcribe.transcribe_file(source, "--second-pass", "off", elevenlabs_client=client)
        self.assertEqual(package.sidecar["source"]["filename"], source.name)
        client.transcribe.assert_called_once()

    def test_video_demux_only_downmixes_layouts_flac_cannot_encode(self):
        video = self.root / "clip.mkv"
        output = self.root / "audio.flac"
        for channels in (6, 10):
            probe = json.dumps({"streams": [
                {"codec_type": "video"},
                {"codec_type": "audio", "channels": channels},
                {"codec_type": "audio", "channels": 2},
            ]}).encode("utf-8")
            with (self.subTest(channels=channels),
                  patch.object(media, "_run", side_effect=[probe, b""]) as tool):
                self.assertEqual(media.video_audio(video, output, "/tools/ffprobe",
                                                   lambda: "/tools/ffmpeg", {}), output)
                command = tool.call_args.args[0]
                if channels > 8:
                    self.assertEqual(command[command.index("-ac") + 1], "2")
                else:
                    self.assertNotIn("-ac", command)

    def test_video_demux_and_local_leveling(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        self.assertIsNotNone(ffmpeg, "ffmpeg is required for qualification")
        self.assertIsNotNone(ffprobe, "ffprobe is required for qualification")
        video = self.root / "clip.mkv"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=1",
                        "-f", "lavfi", "-i", "sine=f=440:d=1", "-c:v", "ffv1",
                        "-c:a", "pcm_s16le", "-shortest", str(video)], check=True)
        paths = {"output_dir": str(self.root / "out"), "ffmpeg": ffmpeg, "ffprobe": ffprobe}
        demux = self.settings(paths=paths, transcription={"provider": "command",
                                                          "command": self.command("require-flac")})
        package = transcribe.transcribe_file(video, settings=demux)
        self.assertEqual(package.sidecar["source"]["filename"], "clip.mkv")
        settings = self.settings(paths=paths, leveling={"provider": "ffmpeg", "mode": "on"})
        package = transcribe.transcribe_file(video, "--output-dir", str(self.root / "leveled"), settings=settings)
        self.assertEqual(package.sidecar["source"]["filename"], "clip.mkv")
        self.assertGreater(package.path("adjusted").stat().st_size, 0)
        self.assertEqual(package.sidecar["leveling"]["algorithm"], "dynaudnorm")
        self.assertIn('provider: "ffmpeg"', package.render())


if __name__ == "__main__":
    unittest.main()
