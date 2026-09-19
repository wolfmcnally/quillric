"""Where two transcriptions of the same audio differ.

A speech-to-text service does not return the same words twice for hard audio. Transcribing a
recording a second time and listing where the two passes differ points a reviewer at exactly the
words the service is unsure of. Neither pass is treated as right; the first stays the transcript.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

SCHEMA = "transcribe.second_pass.v1"
_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def tokens(transcript: dict[str, Any]) -> list[tuple[str, float, float]]:
    """(normalised word, start, end) for every recognised word; casing and punctuation are not differences."""
    out: list[tuple[str, float, float]] = []
    for word in transcript.get("words", []):
        if not isinstance(word, dict) or word.get("type") not in {None, "word"}:
            continue
        start, end = float(word.get("start") or 0.0), float(word.get("end") or word.get("start") or 0.0)
        for token in _TOKEN.findall(str(word.get("text") or "").lower()):
            out.append((token, start, end))
    return out


def compare(first: dict[str, Any], second: dict[str, Any], *, context: int = 4) -> dict[str, Any]:
    a, b = tokens(first), tokens(second)
    words_a, words_b = [item[0] for item in a], [item[0] for item in b]
    matcher = SequenceMatcher(None, words_a, words_b, autojunk=False)
    differences: list[dict[str, Any]] = []
    for operation, a1, a2, b1, b2 in matcher.get_opcodes():
        if operation == "equal":
            continue
        anchor = a[a1] if a1 < len(a) else (a[-1] if a else ("", 0.0, 0.0))
        last = a[a2 - 1] if a2 > a1 else anchor
        differences.append(
            {
                "start": round(anchor[1], 3),
                "end": round(max(last[2], anchor[1]), 3),
                "first": " ".join(words_a[a1:a2]),
                "second": " ".join(words_b[b1:b2]),
                "before": " ".join(words_a[max(0, a1 - context) : a1]),
                "after": " ".join(words_a[a2 : a2 + context]),
            }
        )
    return {
        "schema": SCHEMA,
        "agreement": round(matcher.ratio(), 4) if (a or b) else 1.0,
        "first_words": len(a),
        "second_words": len(b),
        "second_transcription_id": second.get("transcription_id"),
        "differences": differences,
    }
