#!/usr/bin/env python3
"""Render timestamped, speaker-aligned differences between two transcripts."""

from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from compare_transcripts import ComparisonError, normalized_tokens, offset_transcript


def format_timestamp(seconds: float | int | None) -> str:
    value = max(0.0, float(seconds or 0.0))
    minutes, remainder = divmod(value, 60)
    return f"{int(minutes):02d}:{remainder:06.3f}"


def token_records(document: dict[str, Any], speaker_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for word in document.get("words", []):
        if not isinstance(word, dict) or word.get("type") not in {None, "word"}:
            continue
        if word.get("speaker_id") != speaker_id or not isinstance(word.get("text"), str):
            continue
        for token in normalized_tokens(word["text"]):
            records.append(
                {"token": token, "start": word.get("start"), "end": word.get("end")}
            )
    return records


def _phrase(records: list[dict[str, Any]], first: int, last: int) -> str:
    return " ".join(record["token"] for record in records[first:last]) or "∅"


def _time(records: list[dict[str, Any]], first: int, last: int) -> str:
    if first == last or not records:
        return "—"
    start = records[first].get("start")
    end = records[last - 1].get("end")
    return f"{format_timestamp(start)}–{format_timestamp(end)}"


def render_role_differences(
    role: str,
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> list[str]:
    matcher = SequenceMatcher(
        None,
        [record["token"] for record in baseline],
        [record["token"] for record in candidate],
        autojunk=False,
    )
    lines = [f"## {role.title()} voice", "", "| Time | Baseline | Candidate | Change |", "|---|---|---|---|"]
    changes = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes += 1
        timestamp = _time(candidate, j1, j2)
        if timestamp == "—":
            timestamp = _time(baseline, i1, i2)
        baseline_phrase = _phrase(baseline, i1, i2).replace("|", "\\|")
        candidate_phrase = _phrase(candidate, j1, j2).replace("|", "\\|")
        lines.append(
            f"| {timestamp} | {baseline_phrase} | {candidate_phrase} | {tag} |"
        )
    if not changes:
        lines.append("| — | — | — | no differences |")
    lines.append("")
    return lines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--comparison", required=True, type=Path)
    parser.add_argument("--candidate-offset-seconds", type=float, default=0.0)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        comparison = json.loads(args.comparison.read_text(encoding="utf-8"))
        candidate_name = args.candidate.stem
        experiment = comparison["experiments"][candidate_name]
        baseline_experiment = comparison["experiments"][comparison["baseline"]]
        candidate = offset_transcript(candidate, args.candidate_offset_seconds)
        lines = [
            f"# {candidate_name} disagreements",
            "",
            "Baseline wording is a reference, not ground truth; each row requires contextual review.",
            "",
        ]
        for role in ("quiet", "loud"):
            baseline_records = token_records(
                baseline, baseline_experiment[role]["speaker_id"]
            )
            candidate_records = token_records(candidate, experiment[role]["speaker_id"])
            lines.extend(render_role_differences(role, baseline_records, candidate_records))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines), encoding="utf-8")
    except (ComparisonError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
