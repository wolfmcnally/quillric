from __future__ import annotations

import io
import json
import math
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import transcribe  # noqa: E402
from transcribe import cli, levels, second_pass  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def word(text, start, end, speaker="speaker_0"):
    return {"type": "word", "text": text, "start": start, "end": end, "speaker_id": speaker}


def transcript(*texts, identifier="pass"):
    words = [word(text, index * 1.0, index * 1.0 + 0.5) for index, text in enumerate(texts)]
    return {"language_code": "eng", "language_probability": 0.9, "audio_duration_secs": float(len(texts)), "transcription_id": identifier, "words": words}


def tone_wav(path: Path, segments: list[tuple[float, float]], rate: int = 8000) -> Path:
    """Segments of (seconds, amplitude): a steady voice-band tone whose loudness we control exactly."""
    frames = bytearray()
    position = 0
    for seconds, amplitude in segments:
        for _ in range(int(seconds * rate)):
            frames += struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 440 * position / rate)))
            position += 1
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


class LevelsMathTests(unittest.TestCase):
    def test_one_steady_voice_has_no_spread(self) -> None:
        measured = levels.levels_from_windows([-20.0] * 100)
        self.assertEqual((measured.spread_db, measured.quiet_share), (0.0, 0.0))
        self.assertFalse(measured.uneven())

    def test_a_loud_and_a_quiet_voice_are_uneven_and_silence_is_ignored(self) -> None:
        measured = levels.levels_from_windows([-12.0] * 60 + [-38.0] * 40 + [-90.0] * 50)
        self.assertEqual(measured.spread_db, 26.0)
        self.assertEqual(measured.quiet_share, 0.4)
        self.assertAlmostEqual(measured.speech_share, 100 / 150, places=3)
        self.assertTrue(measured.uneven())
        self.assertFalse(measured.uneven(30.0))

    def test_too_little_audio_is_an_error_not_a_zero(self) -> None:
        with self.assertRaises(levels.LevelsError):
            levels.levels_from_windows([-20.0] * 5)
        with self.assertRaises(levels.LevelsError):
            levels.levels_from_windows([-120.0] * 5 + [-20.0] * 8 + [-119.0] * 200)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg measures recordings")
class LevelsMeasureTests(unittest.TestCase):
    def test_measured_spread_matches_the_amplitudes_we_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            even = levels.measure(tone_wav(Path(temporary) / "even.wav", [(12.0, 0.5)]))
            uneven = levels.measure(tone_wav(Path(temporary) / "uneven.wav", [(8.0, 0.5), (8.0, 0.5 / 10 ** (28 / 20))]))
        self.assertLess(even.spread_db, 1.0)
        self.assertAlmostEqual(uneven.spread_db, 28.0, delta=1.0)
        self.assertTrue(uneven.uneven() and not even.uneven())

    def test_a_file_that_is_not_audio_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bogus = Path(temporary) / "bogus.mp3"
            bogus.write_bytes(b"not audio")
            with self.assertRaises(levels.LevelsError):
                levels.measure(bogus)


class SecondPassTests(unittest.TestCase):
    def test_identical_passes_agree_and_casing_or_punctuation_is_not_a_difference(self) -> None:
        result = second_pass.compare(transcript("Hello,", "there."), transcript("hello", "There"))
        self.assertEqual((result["agreement"], result["differences"]), (1.0, []))

    def test_a_changed_word_is_located_in_time_with_context(self) -> None:
        first = transcript("we", "sent", "direct", "emails", "to", "the", "parties")
        second = transcript("we", "sent", "threat", "emails", "to", "the", "parties", identifier="second")
        result = second_pass.compare(first, second)
        self.assertEqual(len(result["differences"]), 1)
        difference = result["differences"][0]
        self.assertEqual((difference["first"], difference["second"], difference["start"]), ("direct", "threat", 2.0))
        self.assertEqual((difference["before"], difference["after"]), ("we sent", "emails to the parties"))
        self.assertLess(result["agreement"], 1.0)
        self.assertEqual(result["second_transcription_id"], "second")

    def test_words_only_one_pass_heard_are_differences_too(self) -> None:
        result = second_pass.compare(transcript("I", "do"), transcript("I", "do", "not"))
        self.assertEqual([(d["first"], d["second"]) for d in result["differences"]], [("", "not")])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg measures recordings")
class PipelineDecisionTests(unittest.TestCase):
    def run_pipeline(self, segments, *options, responses=None):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        source = tone_wav(Path(self.temporary.name) / "call.wav", segments)
        client = Mock()
        client.transcribe.side_effect = list(responses or [transcript("we", "sent", "direct", "emails"), transcript("we", "sent", "threat", "emails", identifier="second")])
        with (
            patch.object(cli.elevenlabs, "load_api_key", return_value="key"),
            patch.object(cli.elevenlabs, "ElevenLabsClient", return_value=client),
            patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("Auphonic must not be touched")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = cli.main([str(source), "--quiet", *options])
        self.assertEqual(code, 0)
        return source, client, transcribe.Package.load(Path(self.temporary.name) / "call")

    def test_the_library_call_prints_nothing_and_never_swaps_standard_output(self) -> None:
        # Standard output is process-wide; swapping it around a call breaks every other thread's
        # output when recordings convert concurrently (a caller's whole report once vanished).
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        source = tone_wav(Path(self.temporary.name) / "call.wav", [(12.0, 0.5)])
        client = Mock()
        client.transcribe.side_effect = [transcript("we", "sent", "direct", "emails")]
        seen = []
        stream = io.StringIO()

        with (
            patch.object(cli.elevenlabs, "load_api_key", return_value="key"),
            patch.object(cli.elevenlabs, "ElevenLabsClient", return_value=client),
            patch.object(cli.auphonic, "load_api_key", side_effect=AssertionError("Auphonic must not be touched")),
            redirect_stdout(stream),
            redirect_stderr(io.StringIO()),
        ):
            original = cli.run_pipeline

            def observed(*args, **kwargs):
                seen.append(sys.stdout)
                return original(*args, **kwargs)

            with patch.object(cli, "run_pipeline", observed):
                package = transcribe.transcribe_file(source, "--output-dir", str(Path(self.temporary.name) / "call"))
            self.assertIs(sys.stdout, stream)
        self.assertEqual(seen, [stream])
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(package.directory.resolve(), (Path(self.temporary.name) / "call").resolve())

    def test_an_even_recording_is_sent_as_it_is_once_and_auphonic_is_never_touched(self) -> None:
        source, client, package = self.run_pipeline([(12.0, 0.5)])
        self.assertEqual(client.transcribe.call_count, 1)
        self.assertEqual(Path(client.transcribe.call_args.args[0]).name, "call.wav")
        self.assertEqual(sorted(path.name for path in package.directory.iterdir()), ["call-package.json", "call-raw.json", "call-speakers.json", "call-transcription.md", "call.wav"])
        self.assertIsNone(package.sidecar["files"]["adjusted"])
        self.assertEqual((package.sidecar["leveling"]["mode"], package.sidecar["leveling"]["applied"]), ("off", False))
        self.assertLess(package.sidecar["leveling"]["levels"]["spread_db"], 1.0)
        self.assertIsNone(package.sidecar["second_pass"])
        markdown = package.path("markdown").read_text()
        self.assertIn("preprocessing: null", markdown)
        self.assertIn("  applied: false", markdown)
        self.assertNotIn("## Uncertain passages", markdown)
        self.assertEqual(markdown, package.render())

    def test_an_uneven_recording_gets_a_second_pass_and_its_differences_are_shown(self) -> None:
        _, client, package = self.run_pipeline([(8.0, 0.5), (8.0, 0.5 / 10 ** (28 / 20))])
        self.assertEqual(client.transcribe.call_count, 2)
        self.assertEqual(package.sidecar["second_pass"]["differences"][0]["first"], "direct")
        self.assertEqual(json.loads((package.directory / "call-second-raw.json").read_text())["transcription_id"], "second")
        self.assertEqual(package.transcript()["transcription_id"], "pass")  # the first pass stays the transcript
        markdown = package.path("markdown").read_text()
        self.assertIn("**speaker_0:** we sent {direct | threat} emails", markdown)  # both readings, in the flow
        self.assertIn("Word disagreements: 1. Speaker disagreements: 0.", markdown)
        self.assertIn("  differences: 1", markdown)
        self.assertNotIn("## Uncertain passages", markdown)
        package.assign({"speaker_0": "Jane Smith"})
        self.assertIn("**Jane Smith (speaker_0):** we sent {direct | threat} emails", package.path("markdown").read_text())  # survives re-rendering

    def test_the_threshold_is_adjustable_and_second_pass_can_be_forced_or_refused(self) -> None:
        _, client, _ = self.run_pipeline([(8.0, 0.5), (8.0, 0.5 / 10 ** (28 / 20))], "--uneven-threshold", "35")
        self.assertEqual(client.transcribe.call_count, 1)
        _, client, _ = self.run_pipeline([(12.0, 0.5)], "--second-pass", "on")
        self.assertEqual(client.transcribe.call_count, 2)
        _, client, package = self.run_pipeline([(8.0, 0.5), (8.0, 0.5 / 10 ** (28 / 20))], "--second-pass", "off")
        self.assertEqual(client.transcribe.call_count, 1)
        self.assertIsNone(package.sidecar["leveling"]["levels"])  # nothing asked for a measurement

    def test_unmeasurable_audio_fails_cleanly_under_auto(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bogus.mp3"
            source.write_bytes(b"not audio")
            errors = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                code = cli.main([str(source), "--quiet"])
            self.assertEqual(code, 1)
            self.assertIn("could not decode", errors.getvalue())
            self.assertFalse((Path(temporary) / "bogus").exists())


if __name__ == "__main__":
    unittest.main()
