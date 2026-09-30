"""Local audio fingerprints: recognise one recording in another format, or a clip inside a longer one.

Nothing leaves the machine. A file is decoded with ``ffmpeg`` to 5512 Hz mono, and every 46 ms a
32-bit code is taken from the sign of energy differences between 33 log-spaced bands across time
and frequency (Haitsma and Kalker, "A Highly Robust Audio Fingerprinting System", ISMIR 2002). Such
codes survive re-encoding, resampling and level changes; bytes and durations do not have to match.

Matching is two steps. An index of codes votes for (other file, time offset); the winning offset
is then verified by the bit error rate over every aligned frame. Unrelated audio sits near 0.5.

Needs ``numpy`` (install ``.[fingerprint]`` from the Quillric checkout) and ``ffmpeg`` on the path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = "transcribe.fingerprint.v1"
SAMPLE_RATE = 5512
FRAME = 2048  # 0.37 s analysis window
HOP = 256  # one code every 46 ms
BANDS = 33
LOW_HZ, HIGH_HZ = 300.0, 2000.0
SECONDS_PER_CODE = HOP / SAMPLE_RATE
#: Verified bit error rate at or below this is the same audio. Transcodes measure well under it.
MATCH_BER = 0.25
#: A code shared by more places than this says nothing (silence, tones, hum) and is not indexed.
MAX_POSTINGS = 40
MIN_OVERLAP_SECONDS = 10.0


class FingerprintError(RuntimeError):
    """The file could not be decoded, or a needed tool is missing."""


def _numpy() -> Any:
    try:
        import numpy
    except ImportError as exc:
        raise FingerprintError("fingerprints need numpy: install Quillric's fingerprint extra from its checkout (pip install '.[fingerprint]')") from exc
    return numpy


@dataclass
class Fingerprint:
    """``codes`` holds one unsigned 32-bit code per 46 ms; ``voiced`` marks frames with enough energy to trust."""

    path: str
    seconds: float
    codes: Any
    voiced: Any

    def to_json(self) -> dict[str, Any]:
        import base64

        np = _numpy()
        return {
            "schema": SCHEMA,
            "path": self.path,
            "seconds": round(self.seconds, 3),
            "codes": base64.b64encode(np.asarray(self.codes, dtype="<u4").tobytes()).decode("ascii"),
            "voiced": base64.b64encode(np.packbits(np.asarray(self.voiced, dtype=bool)).tobytes()).decode("ascii"),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Fingerprint":
        import base64

        np = _numpy()
        if data.get("schema") != SCHEMA:
            raise FingerprintError(f"unsupported fingerprint schema: {data.get('schema')!r}")
        codes = np.frombuffer(base64.b64decode(data["codes"]), dtype="<u4")
        voiced = np.unpackbits(np.frombuffer(base64.b64decode(data["voiced"]), dtype=np.uint8))[: len(codes)].astype(bool)
        return cls(data["path"], float(data["seconds"]), codes, voiced)


def decode(path: Path) -> Any:
    """The first audio stream as float samples at 5512 Hz mono. Video contributes its audio track only."""
    np = _numpy()
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FingerprintError("ffmpeg is required to decode audio")
    command = [executable, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=3600)
    except subprocess.CalledProcessError as exc:
        raise FingerprintError(f"could not decode {Path(path).name}: {exc.stderr.decode(errors='replace').strip()[-200:]}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FingerprintError(f"could not decode {Path(path).name}") from exc
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def _band_edges(np: Any) -> Any:
    edges_hz = np.logspace(np.log10(LOW_HZ), np.log10(HIGH_HZ), BANDS + 1)
    return np.round(edges_hz * FRAME / SAMPLE_RATE).astype(int)


def codes_from_samples(samples: Any) -> tuple[Any, Any]:
    """(codes, voiced) for a sample array. Pure computation, so tests can feed it synthetic audio."""
    np = _numpy()
    if len(samples) < FRAME + HOP:
        return np.zeros(0, dtype=np.uint32), np.zeros(0, dtype=bool)
    count = 1 + (len(samples) - FRAME) // HOP
    window = np.hanning(FRAME).astype(np.float32)
    edges = _band_edges(np)
    energy = np.empty((count, BANDS), dtype=np.float64)
    step = 4096  # frames per block, to bound memory on long recordings
    for start in range(0, count, step):
        stop = min(count, start + step)
        index = np.arange(FRAME)[None, :] + (np.arange(start, stop) * HOP)[:, None]
        spectrum = np.abs(np.fft.rfft(samples[index] * window, axis=1)) ** 2
        for band in range(BANDS):
            energy[start:stop, band] = spectrum[:, edges[band] : max(edges[band] + 1, edges[band + 1])].sum(axis=1)
    across = energy[:, :-1] - energy[:, 1:]
    bits = (across[1:] - across[:-1]) > 0
    weights = (1 << np.arange(BANDS - 1, dtype=np.uint64))[::-1]
    codes = (bits.astype(np.uint64) * weights).sum(axis=1).astype(np.uint32)
    total = energy.sum(axis=1)[1:]
    floor = max(float(np.percentile(total, 95)) * 1e-4, 1e-9) if len(total) else 0.0
    return codes, total > floor


def fingerprint_file(path: Path | str) -> Fingerprint:
    path = Path(path)
    samples = decode(path)
    codes, voiced = codes_from_samples(samples)
    return Fingerprint(str(path), len(samples) / SAMPLE_RATE, codes, voiced)


def bit_error_rate(a: Fingerprint, b: Fingerprint, offset: int) -> tuple[float, int]:
    """(BER, frames compared) with ``b`` shifted by ``offset`` codes against ``a``; only frames voiced in both count."""
    np = _numpy()
    start_a, start_b = max(0, offset), max(0, -offset)
    length = min(len(a.codes) - start_a, len(b.codes) - start_b)
    if length <= 0:
        return 1.0, 0
    keep = a.voiced[start_a : start_a + length] & b.voiced[start_b : start_b + length]
    if not keep.any():
        return 1.0, 0
    differing = np.bitwise_xor(a.codes[start_a : start_a + length][keep], b.codes[start_b : start_b + length][keep])
    flipped = np.unpackbits(differing.view(np.uint8)).sum()
    return float(flipped) / (32.0 * int(keep.sum())), int(keep.sum())


@dataclass(frozen=True)
class Match:
    a: str
    b: str
    relation: str  # "same recording" | "a contains b" | "b contains a" | "overlap"
    ber: float
    overlap_seconds: float
    offset_seconds: float  # where b starts inside a (negative: a starts inside b)
    covers_a: float
    covers_b: float

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ber": round(self.ber, 4), "overlap_seconds": round(self.overlap_seconds, 1),
                "offset_seconds": round(self.offset_seconds, 2), "covers_a": round(self.covers_a, 3), "covers_b": round(self.covers_b, 3)}


def _relation(covers_a: float, covers_b: float) -> str:
    if covers_a >= 0.95 and covers_b >= 0.95:
        return "same recording"
    if covers_b >= 0.95:
        return "a contains b"
    if covers_a >= 0.95:
        return "b contains a"
    return "overlap"


def _verify(a: Fingerprint, b: Fingerprint, offset: int) -> Match | None:
    best = min(((bit_error_rate(a, b, offset + nudge), nudge) for nudge in (-1, 0, 1)), key=lambda item: item[0][0])
    (ber, frames), nudge = best
    overlap = frames * SECONDS_PER_CODE
    if ber > MATCH_BER or overlap < MIN_OVERLAP_SECONDS:
        return None
    span = min(len(a.codes) - max(0, offset + nudge), len(b.codes) - max(0, -(offset + nudge))) * SECONDS_PER_CODE
    covers_a, covers_b = min(1.0, span / max(a.seconds, 1e-9)), min(1.0, span / max(b.seconds, 1e-9))
    return Match(a.path, b.path, _relation(covers_a, covers_b), ber, overlap, (offset + nudge) * SECONDS_PER_CODE, covers_a, covers_b)


def find_matches(prints: Sequence[Fingerprint], *, min_votes: int = 4) -> list[Match]:
    """Every pair of fingerprints that share audio. Proposals come from two sources and the bit error rate decides.

    An exact 32-bit code survives re-encoding only a few times in a hundred, so the index needs a fair
    overlap to gather votes; votes at neighbouring offsets are pooled. Separately, files of nearly equal
    length are compared directly near offset zero, which is the common case of one recording saved twice.
    """
    np = _numpy()
    usable = [(index, item) for index, item in enumerate(prints) if len(item.codes)]
    if len(usable) < 2:
        return []
    values = np.concatenate([item.codes[item.voiced] for _, item in usable])
    owners = np.concatenate([np.full(int(item.voiced.sum()), index, dtype=np.int32) for index, item in usable])
    frames = np.concatenate([np.nonzero(item.voiced)[0].astype(np.int32) for _, item in usable])
    order = np.argsort(values, kind="stable")
    values, owners, frames = values[order], owners[order], frames[order]
    matches: list[Match] = []
    for index, item in usable:
        query_frames = np.nonzero(item.voiced)[0].astype(np.int64)
        query = item.codes[query_frames]
        low, high = np.searchsorted(values, query, "left"), np.searchsorted(values, query, "right")
        counts = high - low
        keep = (counts > 1) & (counts <= MAX_POSTINGS)
        if not keep.any():
            continue
        low, counts, query_frames = low[keep], counts[keep], query_frames[keep]
        positions = np.repeat(low, counts) + (np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts))
        other = owners[positions].astype(np.int64)
        offsets = np.repeat(query_frames, counts) - frames[positions].astype(np.int64)  # where the other file starts inside this one
        ahead = other > index  # each pair is judged once, from its lower-numbered side
        if not ahead.any():
            continue
        span = 1 << 26
        keys, votes = np.unique(other[ahead] * (2 * span) + (offsets[ahead] + span), return_counts=True)
        tally = dict(zip(keys.tolist(), votes.tolist()))
        best: dict[int, tuple[int, int]] = {}
        for key, vote in tally.items():
            pooled = vote + tally.get(key - 1, 0) + tally.get(key + 1, 0)
            target = key // (2 * span)
            if pooled >= min_votes and pooled > best.get(target, (0, 0))[0]:
                best[target] = (pooled, key % (2 * span) - span)
        for target, (_, offset) in best.items():
            match = _verify(item, prints[target], int(offset))
            if match is not None:
                matches.append(match)
    found = {(match.a, match.b) for match in matches}
    by_length = sorted(usable, key=lambda pair: pair[1].seconds)
    for position, (index, item) in enumerate(by_length):
        for other_index, candidate in by_length[position + 1 :]:
            if candidate.seconds - item.seconds > 2.0:
                break
            first, second = (item, candidate) if index < other_index else (candidate, item)
            if (first.path, second.path) in found:
                continue
            for offset in (0, -3, 3):
                match = _verify(first, second, offset)
                if match is not None:
                    matches.append(match)
                    break
    return matches


def duplicate_groups(prints: Sequence[Fingerprint], matches: Iterable[Match]) -> list[list[str]]:
    """Connected groups of files that are the same recording. Contained clips and overlaps are reported, not grouped."""
    parent = {item.path: item.path for item in prints}

    def root(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for match in matches:
        if match.relation == "same recording":
            parent[root(match.a)] = root(match.b)
    groups: dict[str, list[str]] = {}
    for name in parent:
        groups.setdefault(root(name), []).append(name)
    return sorted((sorted(members) for members in groups.values() if len(members) > 1), key=lambda members: members[0])


def load_or_compute(path: Path, cache_dir: Path | None) -> Fingerprint:
    """A cache keyed by path, size and modification time, so a rerun over a large tree decodes nothing twice."""
    import hashlib

    path = Path(path)
    if cache_dir is None:
        return fingerprint_file(path)
    stat = path.stat()
    key = hashlib.sha256(f"{path.resolve()}\x1f{stat.st_size}\x1f{stat.st_mtime_ns}\x1f{SCHEMA}".encode()).hexdigest()
    cached = Path(cache_dir) / key[:2] / f"{key}.json"
    if cached.is_file():
        return Fingerprint.from_json(json.loads(cached.read_text(encoding="utf-8")))
    result = fingerprint_file(path)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(result.to_json()), encoding="utf-8")
    return result
