from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))
from transcribe import elevenlabs as transcribe  # noqa: E402


class FormattingTests(unittest.TestCase):
    def test_timestamp_format(self) -> None:
        self.assertEqual(transcribe.format_timestamp(65.125), "01:05.125")

    def test_words_are_grouped_into_speaker_turns(self) -> None:
        words = [
            {"text": "Hello", "start": 0.0, "end": 0.4, "speaker_id": "speaker_0"},
            {"text": " there", "start": 0.4, "end": 0.8, "speaker_id": "speaker_0"},
            {"text": "Yes", "start": 1.0, "end": 1.2, "speaker_id": "speaker_1"},
        ]

        turns = transcribe.speaker_turns(words)

        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["text"], "Hello there")
        self.assertEqual(turns[1]["speaker"], "speaker_1")

    def test_spacing_does_not_extend_a_turn_across_silence(self) -> None:
        words = [
            {
                "type": "word",
                "text": "Done",
                "start": 1.0,
                "end": 1.4,
                "speaker_id": "speaker_0",
            },
            {
                "type": "spacing",
                "text": " ",
                "start": 1.4,
                "end": 20.0,
                "speaker_id": "speaker_0",
            },
        ]

        turns = transcribe.speaker_turns(words)

        self.assertEqual(turns[0]["text"], "Done ")
        self.assertEqual(turns[0]["end"], 1.4)

    def test_markdown_contains_speakers_and_times(self) -> None:
        transcript = {
            "language_code": "eng",
            "words": [
                {"text": "Hello", "start": 0.0, "end": 0.4, "speaker_id": "speaker_0"}
            ],
        }

        rendered = transcribe.render_markdown(Path("sample.wav"), transcript)

        self.assertIn("# sample.wav", rendered)
        self.assertIn("[00:00.000–00:00.400] **speaker_0**: Hello", rendered)

    def test_known_speaker_count_is_sent(self) -> None:
        fields = transcribe.transcription_fields(2)

        self.assertEqual(fields["num_speakers"], "2")
        self.assertEqual(fields["diarize"], "true")

    def test_unknown_speaker_count_is_omitted(self) -> None:
        self.assertNotIn("num_speakers", transcribe.transcription_fields())

    def test_diarization_threshold_is_sent(self) -> None:
        fields = transcribe.transcription_fields(diarization_threshold=0.22)

        self.assertEqual(fields["diarization_threshold"], "0.22")
        self.assertNotIn("num_speakers", fields)

    def test_automatic_language_and_optional_speaker_features(self) -> None:
        fields = transcribe.transcription_fields(
            language_code=None,
            tag_audio_events=True,
            no_verbatim=True,
            use_speaker_library=True,
            detect_speaker_roles=True,
        )

        self.assertNotIn("language_code", fields)
        self.assertEqual(fields["tag_audio_events"], "true")
        self.assertEqual(fields["no_verbatim"], "true")
        self.assertEqual(fields["use_speaker_library"], "true")
        self.assertEqual(fields["detect_speaker_roles"], "true")


class KeyTests(unittest.TestCase):
    def test_environment_key_takes_precedence(self) -> None:
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}, clear=True):
            self.assertEqual(transcribe.load_api_key(), "test-key")


class WritingTests(unittest.TestCase):
    def test_writes_json_and_markdown_atomically(self) -> None:
        transcript = {
            "language_code": "eng",
            "words": [
                {"text": "Hello", "start": 0.0, "end": 0.4, "speaker_id": "speaker_0"}
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)

            json_path, markdown_path = transcribe.write_transcript(
                output_dir, Path("sample.wav"), transcript
            )

            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertFalse((output_dir / "sample.json.part").exists())


if __name__ == "__main__":
    unittest.main()
