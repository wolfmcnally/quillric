from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
SCRIPT = BIN_DIR / "transcribe"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from transcribe import cli as transcribe_cli  # noqa: E402


def sample_transcript() -> dict:
    return {
        "language_code": "eng",
        "language_probability": 0.987,
        "audio_duration_secs": 65.25,
        "transcription_id": "stt-test",
        "text": "Hello there. Yes.",
        "words": [
            {
                "type": "word",
                "text": "Hello",
                "start": 0.0,
                "end": 0.4,
                "speaker_id": "speaker_0",
            },
            {
                "type": "spacing",
                "text": " ",
                "start": 0.4,
                "end": 0.4,
                "speaker_id": "speaker_0",
            },
            {
                "type": "word",
                "text": "there.",
                "start": 0.4,
                "end": 0.9,
                "speaker_id": "speaker_0",
            },
            {
                "type": "word",
                "text": "Yes.",
                "start": 61.0,
                "end": 61.25,
                "speaker_id": "speaker_1",
            },
        ],
    }


class PathAndFormattingTests(unittest.TestCase):
    def test_default_output_is_named_for_input_beside_input(self) -> None:
        source = Path("/recordings/interview.final.wav")

        self.assertEqual(
            transcribe_cli.default_output_dir(source),
            Path("/recordings/interview.final"),
        )

    def test_webvtt_timestamp_has_hours_and_milliseconds(self) -> None:
        self.assertEqual(transcribe_cli.webvtt_timestamp(3661.125), "01:01:01.125")

    def test_markdown_has_yaml_metadata_and_webvtt_cue_times(self) -> None:
        rendered = transcribe_cli.render_transcript_markdown(
            source_name="sample.wav",
            adjusted_name="sample-adjusted.mp3",
            transcript=sample_transcript(),
            production_id="production-test",
            algorithms={"leveler": True, "levelerstrength_speech": 110},
            mp3_bitrate=128,
            model_id="scribe_v2",
            language_code=None,
            diarization_threshold=0.22,
            max_speakers=None,
            audio_events=False,
            clean_transcript=False,
            speaker_library=False,
            speaker_roles=False,
        )

        self.assertTrue(rendered.startswith("---\nfilename: \"sample.wav\""))
        self.assertIn("detected_language: \"eng\"", rendered)
        self.assertIn("language_probability: 0.987", rendered)
        self.assertIn("speaker_count: 2", rendered)
        self.assertIn("  - \"speaker_0\"", rendered)
        self.assertIn("[00:00:00.000 --> 00:00:00.900] **speaker_0:** Hello there.", rendered)
        self.assertIn("[00:01:01.000 --> 00:01:01.250] **speaker_1:** Yes.", rendered)
        self.assertNotIn("\n# ", rendered)


class ProgressTests(unittest.TestCase):
    def test_tty_progress_is_truncated_before_it_can_wrap(self) -> None:
        stream = io.StringIO()
        reporter = transcribe_cli.ProgressReporter("tty", stream)
        long_name = "A very long recording filename that exceeds the terminal.mp3"

        with patch.object(
            transcribe_cli.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((40, 24)),
        ):
            reporter.event(
                "copy", "active", f"Copying {long_name}", current=1, total=10
            )

        visible_line = stream.getvalue().split("\033[2K", 1)[1]
        self.assertLessEqual(
            sum(
                0
                if transcribe_cli.unicodedata.combining(character)
                else 2
                if transcribe_cli.unicodedata.east_asian_width(character) in {"F", "W"}
                else 1
                for character in visible_line
            ),
            39,
        )
        self.assertTrue(visible_line.endswith("…"))
        self.assertNotIn("\n", visible_line)

    def test_measured_progress_is_rate_limited_but_completion_is_immediate(self) -> None:
        stream = io.StringIO()
        with patch.object(
            transcribe_cli.time,
            "monotonic",
            side_effect=(0.0, 0.0, 0.05, 0.1),
        ):
            reporter = transcribe_cli.ProgressReporter("plain", stream)
            reporter.event("copy", "active", "Copying", current=0, total=100)
            reporter.event("copy", "active", "Copying", current=50, total=100)
            reporter.event("copy", "active", "Copying", current=100, total=100)

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("0.0%", lines[0])
        self.assertIn("100.0%", lines[1])

    def test_plain_progress_is_line_oriented_and_reports_percentage(self) -> None:
        stream = io.StringIO()
        reporter = transcribe_cli.ProgressReporter("plain", stream)

        reporter.event(
            "copy", "active", "Copying source", current=5, total=10, unit="bytes"
        )

        self.assertEqual(
            stream.getvalue().count("\n"),
            1,
        )
        self.assertIn("[copy] active: Copying source  50.0%", stream.getvalue())
        self.assertNotIn("\r", stream.getvalue())

    def test_json_progress_is_ndjson_with_a_versioned_schema(self) -> None:
        stream = io.StringIO()
        reporter = transcribe_cli.ProgressReporter("json", stream)

        reporter.event(
            "upload", "active", "Uploading", current=1, total=4, unit="bytes"
        )

        record = json.loads(stream.getvalue())
        self.assertEqual(record["schema"], "transcribe.progress.v1")
        self.assertEqual(record["stage"], "upload")
        self.assertEqual(record["percent"], 25.0)
        self.assertEqual(record["unit"], "bytes")

    def test_quiet_progress_emits_nothing(self) -> None:
        stream = io.StringIO()
        reporter = transcribe_cli.ProgressReporter("quiet", stream)

        reporter.event("pipeline", "completed", "Done")

        self.assertEqual(stream.getvalue(), "")

    def test_subprocess_keeps_primary_output_and_json_progress_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "sample.mp3"
            source.write_bytes(b"audio")
            completed = subprocess.run(
                [
                    str(SCRIPT),
                    str(source),
                    "--dry-run",
                    "--progress",
                    "json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        settings = json.loads(completed.stdout)
        events = [json.loads(line) for line in completed.stderr.splitlines()]
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(settings["input"], str(source.resolve()))
        self.assertGreaterEqual(len(events), 3)
        self.assertTrue(
            all(event["schema"] == "transcribe.progress.v1" for event in events)
        )
        self.assertEqual(events[-1]["state"], "completed")

    def test_subprocess_quiet_mode_has_no_routine_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "sample.mp3"
            source.write_bytes(b"audio")
            completed = subprocess.run(
                [str(SCRIPT), str(source), "--dry-run", "--quiet"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["input"], str(source.resolve()))


class CliValidationTests(unittest.TestCase):
    def test_dry_run_uses_best_defaults_without_loading_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "recording.mp3"
            source.write_bytes(b"audio")
            output = io.StringIO()
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key") as auphonic_key,
                patch.object(transcribe_cli.elevenlabs, "load_api_key") as elevenlabs_key,
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = transcribe_cli.main([str(source), "--dry-run"])

        settings = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            Path(settings["output_directory"]), source.resolve().with_suffix("")
        )
        self.assertTrue(settings["auphonic"]["algorithms"]["leveler"])
        self.assertEqual(
            settings["auphonic"]["algorithms"]["levelerstrength_speech"], 110
        )
        self.assertFalse(settings["auphonic"]["algorithms"]["denoise"])
        self.assertFalse(settings["auphonic"]["algorithms"]["normloudness"])
        self.assertEqual(settings["elevenlabs"]["diarization_threshold"], 0.22)
        self.assertEqual(settings["elevenlabs"]["language_code"], "auto")
        auphonic_key.assert_not_called()
        elevenlabs_key.assert_not_called()

    def test_output_may_not_contain_input_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "recording.mp3"
            source.write_bytes(b"audio")
            errors = io.StringIO()
            with redirect_stderr(errors):
                exit_code = transcribe_cli.main(
                    [str(source), "--output-dir", temporary_directory, "--force"]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("cannot contain the input file", errors.getvalue())

    def test_overrides_resolve_without_editing_code(self) -> None:
        args = transcribe_cli.parse_args(
            [
                "sample.mp3",
                "--speech-leveler-strength",
                "120",
                "--auphonic-set",
                "compressor_speech=soft",
                "--max-speakers",
                "4",
            ]
        )

        algorithms, threshold = transcribe_cli.resolved_settings(args)

        self.assertEqual(algorithms["levelerstrength_speech"], 120)
        self.assertEqual(algorithms["compressor_speech"], "soft")
        self.assertIsNone(threshold)

    def test_resume_production_rejects_path_characters_before_api_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "recording.mp3"
            source.write_bytes(b"audio")
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key") as load_key,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = transcribe_cli.main(
                    [
                        str(source),
                        "--resume-auphonic-production",
                        "../not-safe",
                    ]
                )

        self.assertEqual(exit_code, 1)
        load_key.assert_not_called()


class FakeAuphonicClient:
    def __init__(self) -> None:
        self.created_output: dict | None = None

    def create_production(self, source, basename, algorithms, output_file):
        self.created_output = output_file
        return "production-test"

    def upload(self, production_id, source, progress=None):
        if progress is not None:
            progress(source.stat().st_size, source.stat().st_size)
        return None

    def start(self, production_id):
        return None

    def details(self, production_id):
        return {
            "status": transcribe_cli.auphonic.TERMINAL_STATUS_DONE,
            "algorithms": {"leveler": True, "levelerstrength_speech": 110},
            "output_files": [
                {
                    "format": "mp3",
                    "bitrate": 96,
                    "download_url": "https://example.test/My Result.mp3",
                }
            ],
        }

    def download(self, url, destination, progress=None):
        destination.write_bytes(b"adjusted mp3")
        if progress is not None:
            progress(destination.stat().st_size, destination.stat().st_size)


class FakeElevenLabsClient:
    def __init__(self) -> None:
        self.audio_path: Path | None = None
        self.options: dict | None = None

    def transcribe(self, audio_path, num_speakers, threshold, **options):
        self.audio_path = audio_path
        self.options = {
            "num_speakers": num_speakers,
            "threshold": threshold,
            **options,
        }
        if progress := options.get("progress"):
            progress(audio_path.stat().st_size, audio_path.stat().st_size)
        return sample_transcript()


class PipelineTests(unittest.TestCase):
    def test_pipeline_creates_complete_default_package_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            source = parent / "hearing.wav"
            source.write_bytes(b"original audio")
            fake_auphonic = FakeAuphonicClient()
            fake_elevenlabs = FakeElevenLabsClient()
            standard_output = io.StringIO()
            standard_error = io.StringIO()
            details = {
                "output_files": [
                    {"format": "mp3", "download_url": "https://example.test/result"}
                ]
            }
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key", return_value="a"),
                patch.object(
                    transcribe_cli.auphonic,
                    "AuphonicClient",
                    return_value=fake_auphonic,
                ),
                patch.object(
                    transcribe_cli.auphonic,
                    "wait_for_completion",
                    return_value=details,
                ),
                patch.object(
                    transcribe_cli.elevenlabs,
                    "load_api_key",
                    return_value="e",
                ),
                patch.object(
                    transcribe_cli.elevenlabs,
                    "ElevenLabsClient",
                    return_value=fake_elevenlabs,
                ),
                redirect_stdout(standard_output),
                redirect_stderr(standard_error),
            ):
                exit_code = transcribe_cli.main([str(source), "--quiet"])

            destination = parent / "hearing"
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                [
                    "hearing-adjusted.mp3",
                    "hearing-package.json",
                    "hearing-raw.json",
                    "hearing-speakers.json",
                    "hearing-transcription.md",
                    "hearing.wav",
                ],
            )
            sidecar = json.loads((destination / "hearing-package.json").read_text())
            self.assertEqual(sidecar["schema"], "transcribe.package.v1")
            self.assertEqual(
                sidecar["source"],
                {
                    "filename": "hearing.wav",
                    "sha256": hashlib.sha256(b"original audio").hexdigest(),
                    "bytes": len(b"original audio"),
                },
            )
            self.assertEqual(
                sidecar["files"]["adjusted"]["sha256"],
                hashlib.sha256(b"adjusted mp3").hexdigest(),
            )
            markdown = (destination / "hearing-transcription.md").read_text()
            self.assertIn(f'source_sha256: "{sidecar["source"]["sha256"]}"', markdown)
            self.assertIn("| speaker_0 |  |", markdown)
            self.assertEqual((destination / "hearing.wav").read_bytes(), b"original audio")
            self.assertEqual(
                (destination / "hearing-adjusted.mp3").read_bytes(), b"adjusted mp3"
            )
            self.assertEqual(
                json.loads((destination / "hearing-raw.json").read_text()),
                sample_transcript(),
            )
            self.assertEqual(fake_auphonic.created_output["format"], "mp3")
            self.assertEqual(fake_auphonic.created_output["filename"], "hearing-adjusted.mp3")
            self.assertEqual(fake_elevenlabs.options["threshold"], 0.22)
            self.assertFalse(any(parent.glob(".hearing.transcribe-*")))
            self.assertEqual(standard_output.getvalue(), f"{destination}\n")
            self.assertEqual(standard_error.getvalue(), "")

    def test_api_failure_removes_partial_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            source = parent / "hearing.wav"
            source.write_bytes(b"original audio")
            fake_auphonic = FakeAuphonicClient()
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key", return_value="a"),
                patch.object(
                    transcribe_cli.auphonic,
                    "AuphonicClient",
                    return_value=fake_auphonic,
                ),
                patch.object(
                    transcribe_cli.auphonic,
                    "wait_for_completion",
                    side_effect=transcribe_cli.auphonic.AuphonicError("failed"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = transcribe_cli.main([str(source)])

            self.assertEqual(exit_code, 1)
            self.assertFalse((parent / "hearing").exists())
            self.assertFalse(any(parent.glob(".hearing.transcribe-*")))

    def test_resume_reuses_completed_auphonic_production(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            source = parent / "hearing with spaces.wav"
            source.write_bytes(b"original audio")
            fake_auphonic = FakeAuphonicClient()
            fake_auphonic.create_production = Mock(
                side_effect=AssertionError("must not create a new production")
            )
            fake_auphonic.upload = Mock(
                side_effect=AssertionError("must not upload the source again")
            )
            fake_auphonic.start = Mock(
                side_effect=AssertionError("must not restart completed processing")
            )
            fake_elevenlabs = FakeElevenLabsClient()
            wait_for_completion = Mock(
                side_effect=AssertionError("completed production must not be polled")
            )
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key", return_value="a"),
                patch.object(
                    transcribe_cli.auphonic,
                    "AuphonicClient",
                    return_value=fake_auphonic,
                ),
                patch.object(
                    transcribe_cli.auphonic,
                    "wait_for_completion",
                    wait_for_completion,
                ),
                patch.object(
                    transcribe_cli.elevenlabs,
                    "load_api_key",
                    return_value="e",
                ),
                patch.object(
                    transcribe_cli.elevenlabs,
                    "ElevenLabsClient",
                    return_value=fake_elevenlabs,
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = transcribe_cli.main(
                    [
                        str(source),
                        "--resume-auphonic-production",
                        "jDThHAYXcvCMMgzcMwdzbB",
                        "--quiet",
                    ]
                )

            destination = parent / "hearing with spaces"
            self.assertEqual(exit_code, 0)
            self.assertTrue(
                (destination / "hearing with spaces-adjusted.mp3").is_file()
            )
            markdown = (
                destination / "hearing with spaces-transcription.md"
            ).read_text(encoding="utf-8")
            self.assertIn(
                'production_id: "jDThHAYXcvCMMgzcMwdzbB"', markdown
            )
            self.assertIn("levelerstrength_speech: 110", markdown)
            self.assertIn("output_bitrate_kbps: 96", markdown)
            fake_auphonic.create_production.assert_not_called()
            fake_auphonic.upload.assert_not_called()
            fake_auphonic.start.assert_not_called()
            wait_for_completion.assert_not_called()

    def test_force_keeps_existing_package_when_replacement_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            source = parent / "hearing.wav"
            source.write_bytes(b"original audio")
            destination = parent / "hearing"
            destination.mkdir()
            (destination / "prior.txt").write_text("keep me", encoding="utf-8")
            fake_auphonic = FakeAuphonicClient()
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key", return_value="a"),
                patch.object(
                    transcribe_cli.auphonic,
                    "AuphonicClient",
                    return_value=fake_auphonic,
                ),
                patch.object(
                    transcribe_cli.auphonic,
                    "wait_for_completion",
                    side_effect=transcribe_cli.auphonic.AuphonicError("failed"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = transcribe_cli.main([str(source), "--force"])

            self.assertEqual(exit_code, 1)
            self.assertEqual(
                (destination / "prior.txt").read_text(encoding="utf-8"), "keep me"
            )
            self.assertFalse(any(parent.glob(".hearing.transcribe-*")))

    def test_keyboard_interrupt_removes_partial_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            source = parent / "hearing.wav"
            source.write_bytes(b"original audio")
            fake_auphonic = FakeAuphonicClient()
            with (
                patch.object(transcribe_cli.auphonic, "load_api_key", return_value="a"),
                patch.object(
                    transcribe_cli.auphonic,
                    "AuphonicClient",
                    return_value=fake_auphonic,
                ),
                patch.object(
                    transcribe_cli.auphonic,
                    "wait_for_completion",
                    side_effect=KeyboardInterrupt,
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = transcribe_cli.main([str(source)])

            self.assertEqual(exit_code, 130)
            self.assertFalse((parent / "hearing").exists())
            self.assertFalse(any(parent.glob(".hearing.transcribe-*")))

    def test_command_is_executable(self) -> None:
        self.assertNotEqual(SCRIPT.stat().st_mode & 0o111, 0)


if __name__ == "__main__":
    unittest.main()
