from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from transcribe import merge  # noqa: E402


def passage(*words):
    """words: (text, speaker). One second per word."""
    return {"words": [{"type": "word", "text": text, "start": float(i), "end": i + 0.5, "speaker_id": speaker} for i, (text, speaker) in enumerate(words)]}


def lines(first, second, names=None):
    return [f"{merge.speaker_label(turn['speakers'], names or {})}: {turn['text']}" for turn in merge.merged_turns(first, second)]


class MergeTests(unittest.TestCase):
    def test_one_pass_is_just_its_turns(self) -> None:
        only = passage(("Good", "speaker_0"), ("morning.", "speaker_0"), ("Morning.", "speaker_1"))
        self.assertEqual(lines(only, None), ["speaker_0: Good morning.", "speaker_1: Morning."])

    def test_a_different_word_is_shown_both_ways_inside_the_sentence(self) -> None:
        first = passage(("We", "speaker_0"), ("sent", "speaker_0"), ("direct", "speaker_0"), ("emails.", "speaker_0"))
        second = passage(("We", "speaker_0"), ("sent", "speaker_0"), ("threat", "speaker_0"), ("emails.", "speaker_0"))
        self.assertEqual(lines(first, second), ["speaker_0: We sent {direct | threat} emails."])
        self.assertEqual(merge.summary(merge.merged_turns(first, second)), {"word_disagreements": 1, "speaker_disagreements": 0})

    def test_a_word_only_one_pass_heard_is_shown_against_a_dash(self) -> None:
        first = passage(("I", "speaker_0"), ("do.", "speaker_0"))
        second = passage(("I", "speaker_0"), ("do", "speaker_0"), ("not.", "speaker_0"))
        self.assertEqual(lines(first, second), ["speaker_0: I do. {— | not.}"])
        self.assertEqual(lines(second, first), ["speaker_0: I do {not. | —}"])

    def test_casing_and_punctuation_are_not_disagreements_and_the_first_pass_wording_is_kept(self) -> None:
        first = passage(("Yes,", "speaker_0"), ("Your", "speaker_0"), ("Honor.", "speaker_0"))
        second = passage(("yes", "speaker_0"), ("your", "speaker_0"), ("honor", "speaker_0"))
        self.assertEqual(lines(first, second), ["speaker_0: Yes, Your Honor."])

    def test_second_pass_labels_are_matched_before_speakers_are_compared(self) -> None:
        first = passage(("Call", "speaker_0"), ("your", "speaker_0"), ("witness.", "speaker_0"), ("Thank", "speaker_1"), ("you.", "speaker_1"))
        relabelled = passage(("Call", "speaker_4"), ("your", "speaker_4"), ("witness.", "speaker_4"), ("Thank", "speaker_2"), ("you.", "speaker_2"))
        self.assertEqual(lines(first, relabelled), ["speaker_0: Call your witness.", "speaker_1: Thank you."])

    def test_words_given_to_different_speakers_become_a_turn_labelled_with_both(self) -> None:
        first = passage(("Did", "speaker_0"), ("you", "speaker_0"), ("go?", "speaker_0"), ("I", "speaker_1"), ("will", "speaker_1"), ("not.", "speaker_1"), ("Okay.", "speaker_0"), ("Next", "speaker_0"), ("question.", "speaker_0"))
        second = passage(("Did", "speaker_0"), ("you", "speaker_0"), ("go?", "speaker_0"), ("I", "speaker_1"), ("will", "speaker_1"), ("not.", "speaker_1"), ("Okay.", "speaker_1"), ("Next", "speaker_0"), ("question.", "speaker_0"))
        self.assertEqual(lines(first, second), ["speaker_0: Did you go?", "speaker_1: I will not.", "speaker_0 or speaker_1: Okay.", "speaker_0: Next question."])
        self.assertEqual(merge.summary(merge.merged_turns(first, second))["speaker_disagreements"], 1)

    def test_names_show_beside_identities_and_one_name_on_both_sides_ends_the_dispute(self) -> None:
        first = passage(("Fine.", "speaker_0"), ("Okay.", "speaker_0"))
        second = passage(("Fine.", "speaker_0"), ("Okay.", "speaker_1"))
        self.assertEqual(lines(first, second, {"speaker_0": "Jane Smith"})[1], "Jane Smith (speaker_0) or second:speaker_1: Okay.")
        same = {"speaker_0": "Jane Smith", "second:speaker_1": "Jane Smith"}
        self.assertEqual(lines(first, second, same)[1], "Jane Smith (speaker_0, second:speaker_1): Okay.")

    def test_a_voice_only_the_second_pass_told_apart_keeps_an_identity_that_can_be_named(self) -> None:
        first = passage(("One", "speaker_0"), ("two", "speaker_0"), ("three", "speaker_0"), ("I", "speaker_0"), ("do.", "speaker_0"))
        second = passage(("One", "speaker_0"), ("two", "speaker_0"), ("three", "speaker_0"), ("I", "speaker_1"), ("do.", "speaker_1"))
        turns = merge.merged_turns(first, second)
        self.assertEqual(merge.second_only_speakers(turns), ["second:speaker_1"])
        self.assertEqual(lines(first, second)[1], "speaker_0 or second:speaker_1: I do.")

    def test_each_side_of_the_merge_reads_back_as_its_own_pass(self) -> None:
        import re

        first = passage(("the", "speaker_0"), ("parties", "speaker_0"), ("here", "speaker_0"), ("and", "speaker_0"), ("now", "speaker_1"), ("did", "speaker_1"), ("agree", "speaker_1"))
        second = passage(("the", "speaker_0"), ("parts", "speaker_0"), ("in", "speaker_0"), ("the", "speaker_0"), ("room", "speaker_0"), ("would", "speaker_1"), ("agree", "speaker_1"))
        text = " ".join(turn["text"] for turn in merge.merged_turns(first, second))
        side = lambda which: re.sub(r"\{(.*?) \| (.*?)\}", lambda m: m.group(which).replace("—", ""), text).split()  # noqa: E731
        self.assertEqual(side(1), [w["text"] for w in first["words"]])
        self.assertEqual(side(2), [w["text"] for w in second["words"]])

    def test_delimiters_inside_the_words_themselves_are_escaped(self) -> None:
        first = passage(("a|b", "speaker_0"), ("{x}", "speaker_0"))
        second = passage(("ab", "speaker_0"), ("y", "speaker_0"))
        self.assertEqual(lines(first, second), [r"speaker_0: {a\|b \{x\} | ab y}"])


if __name__ == "__main__":
    unittest.main()
