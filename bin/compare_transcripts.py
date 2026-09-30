#!/usr/bin/env python3
"""Compare diarized transcripts by aligned speaker, confidence, and consensus."""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import math
import re
import statistics
import subprocess
import sys
from array import array
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


class ComparisonError(RuntimeError):
    """A safe-to-display comparison error."""


def normalized_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower())


def speaker_tokens(transcript: dict[str, Any]) -> dict[str, list[str]]:
    text_by_speaker: dict[str, list[str]] = {}
    for word in transcript.get("words", []):
        if not isinstance(word, dict) or word.get("type") not in {None, "word"}:
            continue
        speaker = word.get("speaker_id")
        text = word.get("text")
        if not isinstance(speaker, str) or not isinstance(text, str):
            continue
        text_by_speaker.setdefault(speaker, []).extend(normalized_tokens(text))
    return text_by_speaker


def offset_transcript(transcript: dict[str, Any], offset_seconds: float) -> dict[str, Any]:
    """Drop pre-content words and shift retained timestamps back to content time."""
    shifted = copy.deepcopy(transcript)
    shifted_words: list[dict[str, Any]] = []
    for word in shifted.get("words", []):
        if not isinstance(word, dict):
            continue
        start = word.get("start")
        end = word.get("end")
        timestamp = start if isinstance(start, (int, float)) else end
        if isinstance(timestamp, (int, float)) and timestamp < offset_seconds:
            continue
        if isinstance(start, (int, float)):
            word["start"] = max(0.0, start - offset_seconds)
        if isinstance(end, (int, float)):
            word["end"] = max(0.0, end - offset_seconds)
        shifted_words.append(word)
    shifted["words"] = shifted_words
    return shifted


def sequence_similarity(left: list[str], right: list[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def align_speakers(
    reference: dict[str, list[str]], candidate: dict[str, list[str]], count: int = 2
) -> dict[str, str]:
    """Map candidate speaker IDs to reference IDs by maximum lexical agreement."""
    reference_ids = sorted(reference, key=lambda key: len(reference[key]), reverse=True)[:count]
    candidate_ids = sorted(candidate, key=lambda key: len(candidate[key]), reverse=True)[:count]
    if len(reference_ids) != count or len(candidate_ids) != count:
        raise ComparisonError(
            f"Expected {count} content speakers, found reference={len(reference_ids)} "
            f"candidate={len(candidate_ids)}"
        )
    best_score = -1.0
    best_mapping: dict[str, str] = {}
    for permutation in itertools.permutations(reference_ids):
        # Both sequences have the validated count; zip(strict=) requires Python 3.10.
        mapping = dict(zip(candidate_ids, permutation))
        score = sum(
            sequence_similarity(candidate[candidate_id], reference[reference_id])
            for candidate_id, reference_id in mapping.items()
        )
        if score > best_score:
            best_score = score
            best_mapping = mapping
    return best_mapping


def decode_mono_pcm(audio_path: Path, sample_rate: int = 16000) -> array[int]:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise ComparisonError(f"Could not decode source audio: {error}") from error
    samples = array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def speaker_energy_dbfs(
    transcript: dict[str, Any], samples: array[int], sample_rate: int = 16000
) -> dict[str, float]:
    energies: dict[str, list[float]] = {}
    for word in transcript.get("words", []):
        if not isinstance(word, dict) or word.get("type") not in {None, "word"}:
            continue
        speaker = word.get("speaker_id")
        start = word.get("start")
        end = word.get("end")
        if not isinstance(speaker, str) or not isinstance(start, (int, float)):
            continue
        if not isinstance(end, (int, float)) or end <= start:
            continue
        first = max(0, int(start * sample_rate))
        last = min(len(samples), int(end * sample_rate))
        if last <= first:
            continue
        mean_square = sum(sample * sample for sample in samples[first:last]) / (last - first)
        if mean_square <= 0:
            continue
        rms = math.sqrt(mean_square)
        energies.setdefault(speaker, []).append(20 * math.log10(rms / 32768.0))
    return {
        speaker: statistics.median(values)
        for speaker, values in energies.items()
        if values
    }


def speaker_confidence(transcript: dict[str, Any], speaker: str) -> float | None:
    probabilities: list[float] = []
    for word in transcript.get("words", []):
        if not isinstance(word, dict) or word.get("speaker_id") != speaker:
            continue
        log_probability = word.get("logprob")
        if isinstance(log_probability, (int, float)):
            probabilities.append(min(1.0, math.exp(log_probability)))
    return statistics.fmean(probabilities) if probabilities else None


def compare_documents(
    documents: dict[str, dict[str, Any]],
    baseline_name: str,
    source_audio: Path,
    experiment_audio: dict[str, Path] | None = None,
) -> dict[str, Any]:
    if baseline_name not in documents:
        raise ComparisonError(f"Missing baseline transcript {baseline_name}")
    baseline = documents[baseline_name]
    baseline_tokens = speaker_tokens(baseline)
    top_baseline = sorted(
        baseline_tokens, key=lambda key: len(baseline_tokens[key]), reverse=True
    )[:2]
    if len(top_baseline) != 2:
        raise ComparisonError("Baseline does not contain exactly two usable content speakers")

    energies = speaker_energy_dbfs(baseline, decode_mono_pcm(source_audio))
    if any(speaker not in energies for speaker in top_baseline):
        raise ComparisonError("Could not measure both baseline speakers in source audio")
    quiet_speaker = min(top_baseline, key=lambda speaker: energies[speaker])
    loud_speaker = max(top_baseline, key=lambda speaker: energies[speaker])
    roles = {quiet_speaker: "quiet", loud_speaker: "loud"}

    aligned: dict[str, dict[str, dict[str, Any]]] = {}
    for name, document in documents.items():
        tokens = speaker_tokens(document)
        mapping = (
            {speaker: speaker for speaker in top_baseline}
            if name == baseline_name
            else align_speakers(baseline_tokens, tokens)
        )
        by_role: dict[str, dict[str, Any]] = {}
        for candidate_speaker, reference_speaker in mapping.items():
            role = roles[reference_speaker]
            by_role[role] = {
                "speaker_id": candidate_speaker,
                "tokens": tokens[candidate_speaker],
                "word_count": len(tokens[candidate_speaker]),
                "confidence": speaker_confidence(document, candidate_speaker),
                "baseline_similarity": sequence_similarity(
                    tokens[candidate_speaker], baseline_tokens[reference_speaker]
                ),
            }
        aligned[name] = by_role

    experiment_names = [name for name in documents if name != baseline_name]
    for role in ("quiet", "loud"):
        maximum_words = max(aligned[name][role]["word_count"] for name in experiment_names)
        for name in experiment_names:
            metrics = aligned[name][role]
            peer_similarities = [
                sequence_similarity(metrics["tokens"], aligned[peer][role]["tokens"])
                for peer in experiment_names
                if peer != name
            ]
            consensus = statistics.fmean(peer_similarities) if peer_similarities else 0.0
            confidence = metrics["confidence"] if metrics["confidence"] is not None else 0.0
            coverage = metrics["word_count"] / maximum_words if maximum_words else 0.0
            metrics["consensus"] = consensus
            metrics["coverage"] = coverage
            metrics["heuristic_score"] = 0.45 * consensus + 0.35 * confidence + 0.20 * coverage

    for name in documents:
        for role in ("quiet", "loud"):
            del aligned[name][role]["tokens"]

    ranking = sorted(
        experiment_names,
        key=lambda name: statistics.fmean(
            aligned[name][role]["heuristic_score"] for role in ("quiet", "loud")
        ),
        reverse=True,
    )
    processed_energies: dict[str, dict[str, float]] = {}
    if experiment_audio is not None:
        for name in experiment_names:
            if name not in experiment_audio:
                raise ComparisonError(f"Missing processed audio for {name}")
            measured = speaker_energy_dbfs(
                baseline, decode_mono_pcm(experiment_audio[name])
            )
            if any(speaker not in measured for speaker in top_baseline):
                raise ComparisonError(f"Could not measure both speakers in {name}")
            processed_energies[name] = {
                roles[speaker]: measured[speaker] for speaker in top_baseline
            }
    return {
        "baseline": baseline_name,
        "source_speaker_energy_dbfs": {
            roles[speaker]: energies[speaker] for speaker in top_baseline
        },
        "experiments": aligned,
        "processed_speaker_energy_dbfs": processed_energies,
        "ranking": ranking,
        "proxy_warning": (
            "The heuristic combines cross-run consensus, model confidence, and word coverage. "
            "Consensus can reward shared errors; confidence can reward fluent hallucinations; "
            "coverage can reward insertions. Inspect disagreement passages before choosing."
        ),
    }


def render_report(comparison: dict[str, Any]) -> str:
    lines = [
        "# Diarized transcription comparison",
        "",
        f"> {comparison['proxy_warning']}",
        "",
        "## Source speaker levels",
        "",
    ]
    for role, energy in comparison["source_speaker_energy_dbfs"].items():
        lines.append(f"- {role}: {energy:.2f} dBFS median during attributed words")
    lines.extend(["", "## Ranking", ""])
    for index, name in enumerate(comparison["ranking"], start=1):
        quiet = comparison["experiments"][name]["quiet"]
        loud = comparison["experiments"][name]["loud"]
        overall = statistics.fmean(
            [quiet["heuristic_score"], loud["heuristic_score"]]
        )
        lines.append(
            f"{index}. **{name}** — overall `{overall:.4f}`, "
            f"quiet `{quiet['heuristic_score']:.4f}`, loud `{loud['heuristic_score']:.4f}`"
        )
    processed_energies = comparison.get("processed_speaker_energy_dbfs", {})
    if processed_energies:
        lines.extend(
            [
                "",
                "## Processed speaker levels",
                "",
                "| Experiment | Quiet | Loud | Loud–quiet gap |",
                "|---|---:|---:|---:|",
            ]
        )
        for name in comparison["ranking"]:
            energy = processed_energies[name]
            gap = energy["loud"] - energy["quiet"]
            lines.append(
                f"| {name} | {energy['quiet']:.2f} dBFS | {energy['loud']:.2f} dBFS | {gap:.2f} dB |"
            )
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            "| Experiment | Voice | Words | Confidence | Consensus | Baseline similarity | Score |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in comparison["ranking"]:
        for role in ("quiet", "loud"):
            metrics = comparison["experiments"][name][role]
            confidence = metrics["confidence"]
            confidence_text = f"{confidence:.4f}" if confidence is not None else "n/a"
            lines.append(
                f"| {name} | {role} | {metrics['word_count']} | {confidence_text} | "
                f"{metrics['consensus']:.4f} | {metrics['baseline_similarity']:.4f} | "
                f"{metrics['heuristic_score']:.4f} |"
            )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript_dir", type=Path)
    parser.add_argument("--baseline", default="fixture-1")
    parser.add_argument("--source-audio", required=True, type=Path)
    parser.add_argument(
        "--experiment-audio-dir",
        type=Path,
        help="directory containing <transcript-stem>.flac for acoustic measurements",
    )
    parser.add_argument(
        "--experiment-offset-seconds",
        type=float,
        default=0.0,
        help="drop this leading interval from every non-baseline transcript",
    )
    parser.add_argument("--output", default=Path("analysis/transcript-comparison"), type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        paths = sorted(args.transcript_dir.glob("*.json"))
        if not paths:
            raise ComparisonError(f"No transcript JSON files in {args.transcript_dir}")
        documents = {
            path.stem: json.loads(path.read_text(encoding="utf-8")) for path in paths
        }
        if args.experiment_offset_seconds < 0:
            raise ComparisonError("Experiment offset cannot be negative")
        if args.experiment_offset_seconds:
            documents = {
                name: (
                    document
                    if name == args.baseline
                    else offset_transcript(document, args.experiment_offset_seconds)
                )
                for name, document in documents.items()
            }
        experiment_audio = None
        if args.experiment_audio_dir is not None:
            experiment_audio = {
                name: args.experiment_audio_dir / f"{name}.flac"
                for name in documents
                if name != args.baseline
            }
        comparison = compare_documents(
            documents, args.baseline, args.source_audio, experiment_audio
        )
        output_base = args.output.expanduser().resolve()
        output_base.parent.mkdir(parents=True, exist_ok=True)
        output_base.with_suffix(".json").write_text(
            json.dumps(comparison, indent=2) + "\n", encoding="utf-8"
        )
        output_base.with_suffix(".md").write_text(
            render_report(comparison), encoding="utf-8"
        )
    except (ComparisonError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"JSON: {output_base.with_suffix('.json')}")
    print(f"Report: {output_base.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
