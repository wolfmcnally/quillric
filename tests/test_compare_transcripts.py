from __future__ import annotations

import importlib.util
import stat
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "compare_transcripts.py"
SPEC = importlib.util.spec_from_file_location("compare_transcripts", SCRIPT)
assert SPEC and SPEC.loader
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


class ScriptContractTests(unittest.TestCase):
    def test_comparison_cli_is_executable(self) -> None:
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)


class TokenTests(unittest.TestCase):
    def test_normalizes_words_without_losing_apostrophes(self) -> None:
        self.assertEqual(
            compare.normalized_tokens("Don't—stop!"), ["don't", "stop"]
        )

    def test_extracts_tokens_by_speaker(self) -> None:
        document = {
            "words": [
                {"type": "word", "speaker_id": "speaker_0", "text": "Hello"},
                {"type": "spacing", "speaker_id": "speaker_0", "text": " "},
                {"type": "word", "speaker_id": "speaker_1", "text": "There"},
            ]
        }

        self.assertEqual(
            compare.speaker_tokens(document),
            {"speaker_0": ["hello"], "speaker_1": ["there"]},
        )

    def test_offset_drops_intro_and_shifts_content_timestamps(self) -> None:
        document = {
            "words": [
                {"type": "word", "text": "jingle", "start": 1.0, "end": 1.5},
                {"type": "word", "text": "content", "start": 8.4, "end": 8.8},
            ]
        }

        shifted = compare.offset_transcript(document, 6.4)

        self.assertEqual([word["text"] for word in shifted["words"]], ["content"])
        self.assertAlmostEqual(shifted["words"][0]["start"], 2.0)
        self.assertAlmostEqual(shifted["words"][0]["end"], 2.4)
        self.assertEqual(document["words"][1]["start"], 8.4)


class AlignmentTests(unittest.TestCase):
    def test_aligns_swapped_speaker_ids(self) -> None:
        reference = {
            "speaker_0": ["alpha", "beta", "alpha"],
            "speaker_1": ["gamma", "delta", "gamma"],
        }
        candidate = {
            "speaker_a": ["gamma", "delta", "gamma"],
            "speaker_b": ["alpha", "beta", "alpha"],
        }

        mapping = compare.align_speakers(reference, candidate)

        self.assertEqual(
            mapping, {"speaker_a": "speaker_1", "speaker_b": "speaker_0"}
        )

    def test_similarity_penalizes_missing_words(self) -> None:
        complete = ["one", "two", "three"]
        incomplete = ["one", "three"]

        self.assertLess(
            compare.sequence_similarity(complete, incomplete),
            compare.sequence_similarity(complete, complete),
        )


class ComparisonTests(unittest.TestCase):
    @staticmethod
    def _document(quiet: list[str], loud: list[str], confidence: float) -> dict:
        words = []
        for speaker, tokens, start in (
            ("speaker_0", quiet, 0.0),
            ("speaker_1", loud, 1.0),
        ):
            for index, token in enumerate(tokens):
                word_start = start + index * 0.1
                words.append(
                    {
                        "type": "word",
                        "speaker_id": speaker,
                        "text": token,
                        "start": word_start,
                        "end": word_start + 0.08,
                        "logprob": confidence,
                    }
                )
        return {"words": words}

    def test_compares_both_voice_roles_and_ranks_experiments(self) -> None:
        documents = {
            "baseline": self._document(["soft", "voice"], ["loud", "voice"], -0.1),
            "strong": self._document(["soft", "voice"], ["loud", "voice"], -0.05),
            "weak": self._document(["wrong"], ["different"], -2.0),
        }
        with (
            patch.object(compare, "decode_mono_pcm", return_value=array("h", [0])),
            patch.object(
                compare,
                "speaker_energy_dbfs",
                return_value={"speaker_0": -35.0, "speaker_1": -12.0},
            ),
        ):
            result = compare.compare_documents(
                documents, "baseline", Path("source.wav")
            )

        self.assertEqual(result["ranking"], ["strong", "weak"])
        self.assertEqual(
            result["source_speaker_energy_dbfs"], {"quiet": -35.0, "loud": -12.0}
        )
        self.assertIn("quiet", result["experiments"]["strong"])
        self.assertIn("loud", result["experiments"]["strong"])


if __name__ == "__main__":
    unittest.main()
