from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import transcribe  # noqa: E402
from transcribe import package as package_module  # noqa: E402
from transcribe import cli  # noqa: E402


def word(text: str, start: float, end: float, speaker: str) -> dict:
    return {"type": "word", "text": text, "start": start, "end": end, "speaker_id": speaker, "logprob": -0.1}


def spacing(speaker: str) -> dict:
    return {"type": "spacing", "text": " ", "speaker_id": speaker}


def split_speaker_transcript() -> dict:
    """The provider hears one person as speaker_0 and again as speaker_2."""
    return {
        "language_code": "eng",
        "language_probability": 0.99,
        "audio_duration_secs": 12.0,
        "transcription_id": "stt-split",
        "words": [
            word("Good", 0.0, 0.4, "speaker_0"), spacing("speaker_0"), word("morning.", 0.5, 1.0, "speaker_0"),
            word("Morning.", 2.0, 2.5, "speaker_1"),
            word("Shall", 4.0, 4.3, "speaker_2"), spacing("speaker_2"), word("we?", 4.4, 4.8, "speaker_2"),
            word("Yes.", 6.0, 6.2, "speaker_1"),
        ],
    }


SETTINGS = {
    "production_id": "prod-1", "algorithms": {"leveler": True}, "mp3_bitrate": 128, "model_id": "scribe_v2",
    "language_code": None, "diarization_threshold": 0.22, "max_speakers": None, "audio_events": False,
    "clean_transcript": False, "speaker_library": False, "speaker_roles": False,
}


def build(directory: Path) -> transcribe.Package:
    source = directory / "call.mp3"
    source.write_bytes(b"source audio")
    adjusted = directory / "call-adjusted.mp3"
    adjusted.write_bytes(b"adjusted audio")
    return package_module.write_package_files(
        version="test", generated_at="2026-01-01T00:00:00+00:00", source=source, adjusted=adjusted,
        raw_json=directory / "call-raw.json", transcript=split_speaker_transcript(), settings=SETTINGS,
    )


class SpeakerTableTests(unittest.TestCase):
    def test_rows_describe_each_identity_in_order_of_appearance(self) -> None:
        rows = package_module.speaker_rows(split_speaker_transcript())
        self.assertEqual([row["id"] for row in rows], ["speaker_0", "speaker_1", "speaker_2"])
        self.assertEqual([row["name"] for row in rows], [None, None, None])
        second = rows[1]
        self.assertEqual((second["turns"], second["words"]), (2, 2))
        self.assertEqual((second["first_start"], second["last_end"]), (2.0, 6.2))
        self.assertAlmostEqual(second["speaking_seconds"], 0.7)

    def test_words_without_a_speaker_get_a_nameable_row(self) -> None:
        transcript = {"words": [{"type": "word", "text": "Hello", "start": 0.0, "end": 0.3}]}
        self.assertEqual([row["id"] for row in package_module.speaker_rows(transcript)], ["unassigned"])

    def test_one_name_may_cover_several_identities(self) -> None:
        table = package_module.new_speaker_table(split_speaker_transcript(), "abc")
        package_module.assign_names(table, {"speaker_0": "Jane Smith", "speaker_2": " Jane Smith ", "speaker_1": "Judge"})
        self.assertEqual(package_module.people(table), {"Jane Smith": ["speaker_0", "speaker_2"], "Judge": ["speaker_1"]})

    def test_clearing_and_blank_names_leave_the_identity_unnamed(self) -> None:
        table = package_module.new_speaker_table(split_speaker_transcript(), "abc")
        package_module.assign_names(table, {"speaker_0": "Jane Smith", "speaker_1": "  "})
        package_module.assign_names(table, {"speaker_0": None})
        self.assertEqual(package_module.people(table), {})

    def test_unknown_identity_is_refused_and_nothing_changes(self) -> None:
        table = package_module.new_speaker_table(split_speaker_transcript(), "abc")
        with self.assertRaisesRegex(transcribe.PackageError, "speaker_9"):
            package_module.assign_names(table, {"speaker_0": "Jane Smith", "speaker_9": "Nobody"})
        self.assertEqual(package_module.people(table), {})


class PackageTests(unittest.TestCase):
    def test_package_records_source_identity_and_is_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build(Path(temporary))
            package = transcribe.Package.load(Path(temporary))
            self.assertEqual(package.source_sha256, hashlib.sha256(b"source audio").hexdigest())
            self.assertEqual(package.sidecar["source"]["bytes"], len(b"source audio"))
            self.assertEqual(package.speakers["source_sha256"], package.source_sha256)
            self.assertEqual(package.transcript(), split_speaker_transcript())
            self.assertEqual(package.path("markdown").read_text(), package.render())

    def test_assigning_names_rewrites_table_and_markdown_but_not_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build(Path(temporary))
            raw_before = (Path(temporary) / "call-raw.json").read_bytes()
            package = transcribe.Package.load(Path(temporary))
            package.assign({"speaker_0": "Jane Smith", "speaker_2": "Jane Smith"}, {"speaker_2": "same voice, later"})

            reloaded = transcribe.Package.load(Path(temporary))
            self.assertEqual(reloaded.people(), {"Jane Smith": ["speaker_0", "speaker_2"]})
            markdown = reloaded.path("markdown").read_text()
            self.assertIn("| speaker_2 | Jane Smith | 1 | 2 | 00:00:00 | 00:00:04.000 | same voice, later |", markdown)
            self.assertIn("[00:00:00.000 --> 00:00:01.000] **Jane Smith (speaker_0):** Good morning.", markdown)
            self.assertIn("[00:00:04.000 --> 00:00:04.800] **Jane Smith (speaker_2):** Shall we?", markdown)
            self.assertIn("**speaker_1:** Morning.", markdown)
            self.assertIn('generated_at: "2026-01-01T00:00:00+00:00"', markdown)
            self.assertEqual((Path(temporary) / "call-raw.json").read_bytes(), raw_before)

    def test_altered_evidence_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build(Path(temporary))
            (Path(temporary) / "call-raw.json").write_text("{}")
            with self.assertRaisesRegex(transcribe.PackageError, "changed since packaging"):
                transcribe.Package.load(Path(temporary)).assign({"speaker_0": "Jane Smith"})

    def test_table_from_another_recording_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build(Path(temporary))
            table_path = Path(temporary) / "call-speakers.json"
            table = json.loads(table_path.read_text())
            table["source_sha256"] = "0" * 64
            table_path.write_text(json.dumps(table))
            with self.assertRaisesRegex(transcribe.PackageError, "different source"):
                transcribe.Package.load(Path(temporary))


class SpeakersCommandTests(unittest.TestCase):
    def run_command(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["speakers", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_assign_then_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build(Path(temporary))
            code, out, _ = self.run_command(temporary, "speaker_0=Jane Smith", "speaker_2=Jane Smith")
            self.assertEqual(code, 0)
            self.assertIn("# Jane Smith: speaker_0, speaker_2", out)
            code, out, _ = self.run_command(temporary, "--clear", "speaker_2")
            self.assertEqual(code, 0)
            self.assertNotIn("# Jane Smith", out)
            self.assertEqual(transcribe.Package.load(Path(temporary)).people(), {"Jane Smith": ["speaker_0"]})

    def test_unknown_identity_fails_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build(Path(temporary))
            before = (Path(temporary) / "call-speakers.json").read_bytes()
            code, _, err = self.run_command(temporary, "speaker_7=Nobody")
            self.assertEqual(code, 1)
            self.assertIn("unknown speaker identity: speaker_7", err)
            self.assertEqual((Path(temporary) / "call-speakers.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
