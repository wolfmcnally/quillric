"""Find recordings that are the same audio in another format, or a clip of a longer recording. Runs locally."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import fingerprint as fp

AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".aiff", ".aif"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="transcribe duplicates", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="recordings, or directories searched recursively")
    parser.add_argument("--cache", type=Path, help="directory that keeps fingerprints between runs")
    parser.add_argument("--workers", type=int, default=4, help="files decoded at once")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    return parser.parse_args(argv)


def collect(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES)
        else:
            found.append(path)
    return found


def _one(job: tuple[str, str | None]) -> tuple[str, dict | None, str | None]:
    path, cache = job
    try:
        return path, fp.load_or_compute(Path(path), Path(cache) if cache else None).to_json(), None
    except (fp.FingerprintError, OSError) as error:
        return path, None, str(error)


def report(paths: list[Path], cache: Path | None, workers: int) -> dict:
    jobs = [(str(path), str(cache) if cache else None) for path in paths]
    prints, failures = [], {}
    with ProcessPoolExecutor(max(1, workers)) as pool:
        for path, data, error in pool.map(_one, jobs, chunksize=4):
            if data is None:
                failures[path] = error
            else:
                prints.append(fp.Fingerprint.from_json(data))
    matches = fp.find_matches(prints)
    groups = fp.duplicate_groups(prints, matches)
    seconds = {item.path: item.seconds for item in prints}
    redundant = sum(len(group) - 1 for group in groups)
    grouped = {name for group in groups for name in group}
    unique_seconds = sum(value for name, value in seconds.items() if name not in grouped) + sum(max(seconds[name] for name in group) for group in groups)
    return {
        "files": len(paths),
        "fingerprinted": len(prints),
        "undecodable": failures,
        "same_recording_groups": groups,
        "redundant_files": redundant,
        "unique_recordings": len(prints) - redundant,
        "unique_hours": round(unique_seconds / 3600, 2),
        "total_hours": round(sum(seconds.values()) / 3600, 2),
        "contained_or_overlapping": [match.as_dict() for match in matches if match.relation != "same recording"],
        "matches": [match.as_dict() for match in matches],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = report(collect(args.paths), args.cache, args.workers)
    except fp.FingerprintError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['fingerprinted']} of {result['files']} files fingerprinted; {result['unique_recordings']} unique recordings, {result['unique_hours']} of {result['total_hours']} hours")
    for group in result["same_recording_groups"]:
        print("same recording:")
        for name in group:
            print(f"  {name}")
    for match in result["contained_or_overlapping"]:
        print(f"{match['relation']}: a={match['a']} b={match['b']} ({match['overlap_seconds']}s shared, b starts {match['offset_seconds']}s into a)")
    for name, error in result["undecodable"].items():
        print(f"undecodable: {name}: {error}", file=sys.stderr)
    return 0
