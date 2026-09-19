"""How uneven is a recording's speech level? Measured locally with ``ffmpeg`` alone; nothing is sent anywhere.

The recording is cut into 0.4 s windows and each window's RMS level is read. Windows more than 30 dB
below the loudest tenth are treated as silence and dropped (a relative gate, as loudness-range
measures use). From the rest:

``spread_db``     P95 minus P10 of window level: how far apart the loud and the quiet speech sit
``quiet_share``   share of speech windows more than 15 dB below the P90 level
``level_p50_db``  median speech level in dBFS: a recording can be uniformly quiet without being uneven

Spread is what a leveling decision is taken on. It is measured over the whole recording, because a
long recording can be even within every five minutes and still uneven between its sections.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

WINDOW_SECONDS = 0.4
RATE = 8000
GATE_DB = 30.0
QUIET_DB = 15.0
#: Nothing this quiet is speech, whatever the rest of the recording holds.
SILENCE_FLOOR_DB = -70.0
#: Spread at or above this marks a recording as uneven. Set from a survey of several hundred real
#: recordings, where known-uneven multi-microphone rooms measured 26 dB and up and the median was 22.
DEFAULT_UNEVEN_DB = 25.0
_KEY = "lavfi.astats.Overall.RMS_level="


class LevelsError(RuntimeError):
    """The recording could not be measured."""


@dataclass(frozen=True)
class Levels:
    seconds: float
    speech_share: float
    spread_db: float
    quiet_share: float
    level_p50_db: float

    def uneven(self, threshold_db: float = DEFAULT_UNEVEN_DB) -> bool:
        return self.spread_db >= threshold_db

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(ordered: list[float], fraction: float) -> float:
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def levels_from_windows(window_db: list[float]) -> Levels:
    """Pure computation over per-window levels in dBFS, so it can be tested without audio."""
    if len(window_db) < 10:
        raise LevelsError("too short to measure")
    everything = sorted(window_db)
    gate = max(_percentile(everything, 0.90) - GATE_DB, SILENCE_FLOOR_DB)
    speech = [value for value in everything if value > gate]
    if len(speech) < 10:
        raise LevelsError("too little speech to measure")
    p90 = _percentile(speech, 0.90)
    return Levels(
        seconds=round(len(window_db) * WINDOW_SECONDS, 1),
        speech_share=round(len(speech) / len(window_db), 3),
        spread_db=round(_percentile(speech, 0.95) - _percentile(speech, 0.10), 1),
        quiet_share=round(sum(1 for value in speech if value < p90 - QUIET_DB) / len(speech), 3),
        level_p50_db=round(_percentile(speech, 0.50), 1),
    )


def window_levels(path: Path | str) -> list[float]:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise LevelsError("ffmpeg is required to measure a recording")
    chain = (
        f"aformat=channel_layouts=mono,aresample={RATE},asetnsamples=n={int(RATE * WINDOW_SECONDS)}:p=0,"
        "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    command = [executable, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-af", chain, "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=7200)
    except subprocess.CalledProcessError as exc:
        raise LevelsError(f"could not decode {Path(path).name}: {exc.stderr.strip()[-200:]}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LevelsError(f"could not decode {Path(path).name}") from exc
    values: list[float] = []
    for line in result.stdout.splitlines():
        if line.startswith(_KEY):
            text = line[len(_KEY) :].strip()
            values.append(-120.0 if text in {"-inf", "nan", "inf"} else max(-120.0, float(text)))
    return values


def measure(path: Path | str) -> Levels:
    return levels_from_windows(window_levels(path))
