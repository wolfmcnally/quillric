from __future__ import annotations

import sys
from email.message import Message
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))
from transcribe import auphonic  # noqa: E402


class ProductionPayloadTests(unittest.TestCase):
    def test_requested_processing_is_explicit(self) -> None:
        algorithms = auphonic.production_payload(Path("conversation.mp3"))["algorithms"]

        self.assertTrue(algorithms["denoise"])
        self.assertEqual(algorithms["denoisemethod"], "dynamic")
        self.assertEqual(algorithms["denoiseamount"], 12)
        self.assertEqual(algorithms["deverbamount"], 6)
        self.assertTrue(algorithms["leveler"])
        self.assertEqual(algorithms["levelerstrength_speech"], 110)
        self.assertEqual(algorithms["compressor_speech"], "medium")
        self.assertEqual(algorithms["filtermethod"], "bwe")
        self.assertTrue(algorithms["normloudness"])

    def test_all_cutters_are_disabled(self) -> None:
        algorithms = auphonic.production_payload(Path("conversation.mp3"))["algorithms"]

        for name in ("silence_cutter", "filler_cutter", "cough_cutter", "music_cutter"):
            self.assertIs(algorithms[name], False)

    def test_output_is_wav_with_predictable_name(self) -> None:
        payload = auphonic.production_payload(Path("meeting.take.mp3"))

        self.assertEqual(payload["output_basename"], "meeting.take-auphonic")
        self.assertEqual(payload["output_files"], [{"format": "wav"}])

    def test_custom_output_suffix_changes_remote_filename(self) -> None:
        payload = auphonic.production_payload(Path("meeting.mp3"), "meeting-revised")

        self.assertEqual(payload["output_basename"], "meeting-revised")

    def test_explicit_algorithms_replace_defaults(self) -> None:
        payload = auphonic.production_payload(
            Path("meeting.mp3"), algorithms={"leveler": True}
        )

        self.assertEqual(payload["algorithms"], {"leveler": True})

    def test_custom_mp3_output_is_preserved(self) -> None:
        output_file = {
            "format": "mp3",
            "bitrate": "128",
            "filename": "meeting-adjusted.mp3",
        }

        payload = auphonic.production_payload(
            Path("meeting.wav"), output_file=output_file
        )

        self.assertEqual(payload["output_files"], [output_file])


class ConfigurationTests(unittest.TestCase):
    def test_no_defaults_starts_with_every_module_off(self) -> None:
        algorithms = auphonic.resolve_algorithms(None, True, [])

        self.assertEqual(algorithms, auphonic.ALL_PROCESSING_OFF)

    def test_cli_overrides_parse_json_and_strings(self) -> None:
        algorithms = auphonic.resolve_algorithms(
            None, True, ["leveler=true", "levelerstrength_speech=110", "compressor=off"]
        )

        self.assertIs(algorithms["leveler"], True)
        self.assertEqual(algorithms["levelerstrength_speech"], 110)
        self.assertEqual(algorithms["compressor"], "off")

    def test_config_merges_over_all_off_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "algorithms.json"
            config.write_text('{"leveler": true}', encoding="utf-8")

            algorithms = auphonic.resolve_algorithms(config, False, [])

        self.assertTrue(algorithms["leveler"])
        self.assertFalse(algorithms["denoise"])
        self.assertFalse(algorithms["filtering"])
        self.assertFalse(algorithms["normloudness"])

    def test_output_numbers_increase_across_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            first = auphonic.allocate_numbered_basename(
                output_dir, Path("meeting.mp3"), "-first"
            )
            second = auphonic.allocate_numbered_basename(
                output_dir, Path("meeting.mp3"), "-second"
            )

        self.assertEqual(first, "001-meeting-first")
        self.assertEqual(second, "002-meeting-second")


class ApiKeyTests(unittest.TestCase):
    def test_environment_takes_precedence_without_reading_dotenv(self) -> None:
        with patch.dict("os.environ", {"AUPHONIC_API_KEY": "from-environment"}, clear=True):
            self.assertEqual(auphonic.load_api_key(), "from-environment")

    def test_dotenv_value_parser_handles_quotes_and_comments(self) -> None:
        self.assertEqual(auphonic._parse_dotenv_value("'secret value' # comment"), "secret value")


class ResultTests(unittest.TestCase):
    def test_download_url_encodes_spaces_without_double_encoding_signatures(self) -> None:
        url = (
            "https://auphonic.com/api/download/My File.mp3"
            "?token=a%2Fb+c&label=hello world"
        )

        normalized = auphonic._normalized_request_url(url)

        self.assertEqual(
            normalized,
            "https://auphonic.com/api/download/My%20File.mp3"
            "?token=a%2Fb+c&label=hello%20world",
        )

    def test_download_transport_error_is_wrapped_without_leaking_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "result.mp3"
            opener = Mock()
            opener.open.side_effect = auphonic.http.client.InvalidURL("bad URL")
            with patch.object(
                auphonic.urllib.request, "build_opener", return_value=opener
            ):
                with self.assertRaisesRegex(
                    auphonic.AuphonicError, "Could not download the result: bad URL"
                ):
                    auphonic.AuphonicClient("key").download(
                        "https://example.test/result.mp3", destination
                    )

            self.assertFalse(destination.exists())
            self.assertFalse((destination.parent / ".result.mp3.part").exists())

    def test_download_uses_encoded_request_url_and_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "result.mp3"
            response = Mock()
            response.headers = {"Content-Length": "3"}
            response.read.side_effect = [b"mp3", b""]
            response_context = MagicMock()
            response_context.__enter__.return_value = response
            response_context.__exit__.return_value = False
            opener = Mock()
            opener.open.return_value = response_context
            with patch.object(
                auphonic.urllib.request, "build_opener", return_value=opener
            ):
                auphonic.AuphonicClient("key").download(
                    "https://example.test/audio/My Result.mp3", destination
                )

            request = opener.open.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://example.test/audio/My%20Result.mp3",
            )
            self.assertEqual(destination.read_bytes(), b"mp3")

    def test_selects_wav_download(self) -> None:
        details = {
            "output_files": [
                {"format": "mp3", "download_url": "https://example.test/a.mp3"},
                {"format": "wav", "download_url": "https://example.test/a.wav"},
            ]
        }
        self.assertEqual(
            auphonic._result_download_url(details), "https://example.test/a.wav"
        )

    def test_missing_wav_is_an_error(self) -> None:
        with self.assertRaises(auphonic.AuphonicError):
            auphonic._result_download_url({"output_files": []})

    def test_selects_requested_mp3_download(self) -> None:
        details = {
            "output_files": [
                {"format": "wav", "download_url": "https://example.test/a.wav"},
                {"format": "mp3", "download_url": "https://example.test/a.mp3"},
            ]
        }

        self.assertEqual(
            auphonic._result_download_url(details, "mp3"),
            "https://example.test/a.mp3",
        )


class RedirectTests(unittest.TestCase):
    def redirect(self, target: str):
        request = auphonic.urllib.request.Request(
            "https://auphonic.com/download.wav",
            headers={"Authorization": "Bearer secret"},
        )
        response_headers = Message()
        response_headers["Location"] = target
        return auphonic.CrossOriginAuthStripper().redirect_request(
            request, None, 302, "Found", response_headers, target
        )

    def test_strips_authentication_on_cross_origin_redirect(self) -> None:
        redirected = self.redirect("https://storage.example/result.wav")

        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_preserves_authentication_on_same_origin_redirect(self) -> None:
        redirected = self.redirect("https://auphonic.com/other.wav")

        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")


class CliTests(unittest.TestCase):
    def test_missing_input_fails_before_api_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.mp3"
            with patch.object(auphonic, "load_api_key") as load_api_key:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(auphonic.main([str(missing)]), 2)
                load_api_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
