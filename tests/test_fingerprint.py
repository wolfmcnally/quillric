from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from transcribe import cli  # noqa: E402
from transcribe import fingerprint as fp  # noqa: E402

NEEDS = "fingerprints need numpy and ffmpeg"
READY = np is not None and shutil.which("ffmpeg") is not None


def speechlike(seed: int, seconds: float = 40.0, rate: int = 16000):
    """Deterministic, busy, band-limited audio: bursts of filtered noise with a moving pitch. Not a recording of anyone."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    pitch = 120 + 60 * np.sin(2 * np.pi * 0.31 * t + seed) + 25 * np.sin(2 * np.pi * 1.7 * t)
    voiced = sum(np.sin(2 * np.pi * np.cumsum(pitch * k) / rate) / k for k in range(1, 12))
    envelope = np.clip(np.interp(t, np.arange(0, seconds, 0.18), rng.random(int(seconds / 0.18) + 1)) * 1.6 - 0.4, 0, 1)
    noise = np.convolve(rng.standard_normal(len(t)), np.ones(6) / 6, mode="same")
    signal = envelope * (0.7 * voiced + 0.6 * noise)
    return (signal / np.abs(signal).max() * 0.8 * 32767).astype("<i2")


def write_wav(path: Path, samples, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


def transcode(source: Path, target: Path, *options: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *options[:0], "-i", str(source), *options, str(target)], check=True, capture_output=True)
    return target


@unittest.skipUnless(READY, NEEDS)
class FingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.original = write_wav(root / "original.wav", speechlike(1))
        cls.as_mp3 = transcode(cls.original, root / "copy.mp3", "-ac", "1", "-ar", "8000", "-c:a", "libmp3lame", "-b:a", "24k")
        cls.as_m4a = transcode(cls.original, root / "copy.m4a", "-c:a", "aac", "-b:a", "48k")
        cls.clip = transcode(cls.original, root / "clip.m4a", "-ss", "12", "-t", "20", "-c:a", "aac", "-b:a", "48k")
        cls.other = write_wav(root / "other.wav", speechlike(2))
        cls.silence = write_wav(root / "silence.wav", np.zeros(16000 * 20, dtype="<i2"))
        cls.prints = {path.name: fp.fingerprint_file(path) for path in (cls.original, cls.as_mp3, cls.as_m4a, cls.clip, cls.other, cls.silence)}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_other_formats_are_the_same_recording(self) -> None:
        matches = fp.find_matches(list(self.prints.values()))
        groups = fp.duplicate_groups(list(self.prints.values()), matches)
        self.assertEqual([[Path(name).name for name in group] for group in groups], [["copy.m4a", "copy.mp3", "original.wav"]])
        for match in matches:
            self.assertLessEqual(match.ber, fp.MATCH_BER)

    def test_a_clip_is_reported_as_contained_at_its_offset_and_not_grouped(self) -> None:
        matches = fp.find_matches([self.prints["original.wav"], self.prints["clip.m4a"]])
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match.relation, "a contains b")
        self.assertAlmostEqual(match.offset_seconds, 12.0, delta=0.15)
        self.assertGreaterEqual(match.covers_b, 0.95)
        self.assertLess(match.covers_a, 0.6)
        self.assertEqual(fp.duplicate_groups([self.prints["original.wav"], self.prints["clip.m4a"]], matches), [])

    def test_different_audio_does_not_match_at_any_offset(self) -> None:
        self.assertEqual(fp.find_matches([self.prints["original.wav"], self.prints["other.wav"]]), [])
        best = min(fp.bit_error_rate(self.prints["original.wav"], self.prints["other.wav"], offset)[0] for offset in range(-60, 61))
        self.assertGreater(best, 0.4)

    def test_silence_matches_nothing_not_even_more_silence(self) -> None:
        silent = self.prints["silence.wav"]
        self.assertFalse(silent.voiced.any())
        self.assertEqual(fp.find_matches([silent, silent, self.prints["original.wav"]]), [])

    def test_fingerprint_survives_its_json_form_and_the_cache_is_used(self) -> None:
        original = self.prints["original.wav"]
        restored = fp.Fingerprint.from_json(json.loads(json.dumps(original.to_json())))
        self.assertTrue((restored.codes == original.codes).all() and (restored.voiced == original.voiced).all())
        with tempfile.TemporaryDirectory() as cache:
            first = fp.load_or_compute(self.original, Path(cache))
            cached_files = list(Path(cache).rglob("*.json"))
            self.assertEqual(len(cached_files), 1)
            cached_files[0].write_text(json.dumps({**first.to_json(), "seconds": 123.0}))
            self.assertEqual(fp.load_or_compute(self.original, Path(cache)).seconds, 123.0)

    def test_a_file_with_no_audio_is_an_error_not_a_blank_fingerprint(self) -> None:
        bogus = Path(self.temporary.name) / "bogus.mp3"
        bogus.write_bytes(b"not audio at all")
        with self.assertRaises(fp.FingerprintError):
            fp.fingerprint_file(bogus)

    def test_duplicates_command_reports_unique_recordings_and_hours(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["duplicates", self.temporary.name, "--json", "--workers", "2"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual((report["files"], report["fingerprinted"], report["redundant_files"], report["unique_recordings"]), (7, 6, 2, 4))
        self.assertEqual(list(report["undecodable"]), [str(Path(self.temporary.name) / "bogus.mp3")])
        self.assertEqual({entry["relation"] for entry in report["contained_or_overlapping"]}, {"a contains b", "b contains a"} & {entry["relation"] for entry in report["contained_or_overlapping"]})
        self.assertTrue(report["contained_or_overlapping"])


if __name__ == "__main__":
    unittest.main()
