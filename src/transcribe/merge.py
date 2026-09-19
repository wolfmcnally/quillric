"""One transcript from two transcriptions of the same audio, with their disagreements kept in the flow.

Where the passes heard different words, both readings are set off inline: ``{direct | threat}``,
first pass first, with ``—`` for a pass that heard nothing there. Where they gave the same words to
different speakers, that stretch becomes its own turn labelled with both: ``speaker_1 or speaker_3``.
Neither pass is treated as right.

The provider's speaker labels are arbitrary per run, so the second pass's labels are first matched
to the first pass's by how many identically recognised words they share, and failing that by when
they spoke. A second-pass speaker with no counterpart keeps its own identity, prefixed ``second:``, so it can still be named.

Reading the first alternative everywhere gives back the first pass word for word, and the second
alternative the second pass; nothing is invented and nothing is dropped.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
NOTHING = "—"
UNASSIGNED = "unassigned"
SECOND_PREFIX = "second:"


def _words(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Recognised words with the punctuation the provider attached; spacing entries carry no content."""
    out = []
    for word in transcript.get("words", []):
        if isinstance(word, dict) and word.get("type") in {None, "word"} and str(word.get("text") or "").strip():
            text = str(word["text"]).strip()
            out.append({
                "text": text,
                "key": " ".join(_TOKEN.findall(text.lower())),
                "start": word.get("start"),
                "end": word.get("end"),
                "speaker": str(word.get("speaker_id") or UNASSIGNED),
            })
    return out


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("|", "\\|")


def match_speakers(first: list[dict[str, Any]], second: list[dict[str, Any]], opcodes: list[tuple]) -> dict[str, str]:
    """Second-pass identity -> first-pass identity, greedily by shared identical words, one to one."""
    shared: Counter[tuple[str, str]] = Counter()
    for operation, a1, a2, b1, _ in opcodes:
        if operation == "equal":
            for offset in range(a2 - a1):
                shared[(second[b1 + offset]["speaker"], first[a1 + offset]["speaker"])] += 1
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for (theirs, ours), _ in shared.most_common():
        if theirs not in mapping and ours not in taken:
            mapping[theirs] = ours
            taken.add(ours)
    # A speaker with no identically recognised word in common can still be matched by when it spoke.
    starts = [float(word["start"] or 0.0) for word in first]
    timed: Counter[tuple[str, str]] = Counter()
    for word in second:
        if word["speaker"] in mapping or word["start"] is None or not first:
            continue
        middle = (float(word["start"]) + float(word["end"] or word["start"])) / 2
        nearest = first[max(0, bisect.bisect_right(starts, middle) - 1)]
        if abs(float(nearest["start"] or 0.0) - middle) <= 2.0:
            timed[(word["speaker"], nearest["speaker"])] += 1
    for (theirs, ours), _ in timed.most_common():
        if theirs not in mapping and ours not in taken:
            mapping[theirs] = ours
            taken.add(ours)
    for word in second:
        mapping.setdefault(word["speaker"], SECOND_PREFIX + word["speaker"])
    return mapping


def merged_turns(first_pass: dict[str, Any], second_pass: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Turns of the merged transcript: ``speakers`` (one identity, or the disputed ones in first-pass-first
    order), ``start``, ``end``, ``text``, and ``choices`` (how many word disagreements the text holds)."""
    first = _words(first_pass)
    if second_pass is None:
        pieces = [{"speakers": (word["speaker"],), "start": word["start"], "end": word["end"], "text": word["text"], "choice": False} for word in first]
        return _turns(pieces)
    second = _words(second_pass)
    opcodes = SequenceMatcher(None, [word["key"] for word in first], [word["key"] for word in second], autojunk=False).get_opcodes()
    mapping = match_speakers(first, second, opcodes)
    pieces: list[dict[str, Any]] = []
    last_speaker = first[0]["speaker"] if first else UNASSIGNED
    for operation, a1, a2, b1, b2 in opcodes:
        if operation == "equal":
            for offset in range(a2 - a1):
                ours, theirs = first[a1 + offset], second[b1 + offset]
                other = mapping[theirs["speaker"]]
                speakers = (ours["speaker"],) if other == ours["speaker"] else (ours["speaker"], other)
                pieces.append({"speakers": speakers, "start": ours["start"], "end": ours["end"], "text": ours["text"], "choice": False})
                last_speaker = ours["speaker"]
            continue
        ours, theirs = first[a1:a2], second[b1:b2]
        order = [word["speaker"] for word in ours] or [last_speaker]
        order += [mapping[word["speaker"]] for word in theirs]
        speakers = tuple(dict.fromkeys(order))
        anchor = ours or theirs
        text = "{" + (_escape(" ".join(word["text"] for word in ours)) or NOTHING) + " | " + (_escape(" ".join(word["text"] for word in theirs)) or NOTHING) + "}"
        pieces.append({"speakers": speakers, "start": anchor[0]["start"], "end": anchor[-1]["end"], "text": text, "choice": True})
        if ours:
            last_speaker = ours[-1]["speaker"]
    return _turns(pieces)


def _turns(pieces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for piece in pieces:
        if turns and turns[-1]["speakers"] == piece["speakers"]:
            turn = turns[-1]
            turn["text"] += " " + piece["text"]
            if piece["end"] is not None:
                turn["end"] = piece["end"]
        else:
            turn = {"speakers": piece["speakers"], "start": piece["start"], "end": piece["end"], "text": piece["text"], "choices": 0}
            turns.append(turn)
        turn["choices"] += int(piece["choice"])
    return turns


def second_only_speakers(turns: list[dict[str, Any]]) -> list[str]:
    """Identities only the second pass found, in order of appearance, so the speaker table can hold them."""
    seen = dict.fromkeys(identity for turn in turns for identity in turn["speakers"] if identity.startswith(SECOND_PREFIX))
    return list(seen)


def speaker_label(speakers: tuple[str, ...], names: dict[str, str]) -> str:
    """``Jane Smith (speaker_0)``; ``speaker_1 or speaker_3`` when the passes disagree; and one name with
    both identities when the disputed identities have been given the same name, since that is no dispute."""
    named = [names.get(identity) for identity in speakers]
    if len(speakers) > 1 and named[0] and all(name == named[0] for name in named):
        return f"{named[0]} ({', '.join(speakers)})"
    return " or ".join(f"{name} ({identity})" if name else identity for identity, name in zip(speakers, named))


def summary(turns: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "word_disagreements": sum(turn["choices"] for turn in turns),
        "speaker_disagreements": sum(1 for turn in turns if len(turn["speakers"]) > 1),
    }
