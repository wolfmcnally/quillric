"""Render a transcription package's Markdown from its raw transcript, settings, and speaker table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import merge


def webvtt_timestamp(seconds: float | int | None) -> str:
    """Render a fixed-width WebVTT timestamp with millisecond precision."""
    total_milliseconds = round(max(0.0, float(seconds or 0.0)) * 1000)
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    total_minutes, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}.{milliseconds:03d}"


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def transcript_speakers(transcript: dict[str, Any]) -> list[str]:
    speakers = {
        str(word["speaker_id"])
        for word in transcript.get("words", [])
        if isinstance(word, dict)
        and word.get("type") in {None, "word"}
        and word.get("speaker_id") is not None
    }
    return sorted(speakers)


def render_frontmatter(
    *,
    source_name: str,
    adjusted_name: str | None,
    transcript: dict[str, Any],
    production_id: str | None,
    algorithms: dict[str, Any],
    mp3_bitrate: int,
    model_id: str,
    language_code: str | None,
    diarization_threshold: float | None,
    max_speakers: int | None,
    audio_events: bool,
    clean_transcript: bool,
    speaker_library: bool,
    speaker_roles: bool,
    source_sha256: str | None = None,
    generated_at: str | None = None,
    leveling: dict[str, Any] | None = None,
    second_pass: dict[str, Any] | None = None,
) -> list[str]:
    speakers = transcript_speakers(transcript)
    lines = [
        "---",
        f"filename: {yaml_scalar(source_name)}",
    ]
    if source_sha256 is not None:
        lines.append(f"source_sha256: {yaml_scalar(source_sha256)}")
    lines.extend(
        [
            f"adjusted_filename: {yaml_scalar(adjusted_name)}",
            f"detected_language: {yaml_scalar(transcript.get('language_code'))}",
            f"language_probability: {yaml_scalar(transcript.get('language_probability'))}",
            f"speaker_count: {len(speakers)}",
            "speakers:",
        ]
    )
    lines.extend(f"  - {yaml_scalar(speaker)}" for speaker in speakers)
    if not speakers:
        lines[-1] = "speakers: []"
    lines.extend(
        [
            f"audio_duration_seconds: {yaml_scalar(transcript.get('audio_duration_secs'))}",
            f"transcription_id: {yaml_scalar(transcript.get('transcription_id'))}",
            f"generated_at: {yaml_scalar(generated_at or datetime.now(timezone.utc).isoformat())}",
        ]
    )
    measured = (leveling or {}).get("levels") or {}
    if leveling is not None:
        lines.extend(
            [
                "leveling:",
                f"  mode: {yaml_scalar(leveling.get('mode'))}",
                f"  applied: {yaml_scalar(bool(leveling.get('applied')))}",
                f"  speech_level_spread_db: {yaml_scalar(measured.get('spread_db'))}",
                f"  quiet_speech_share: {yaml_scalar(measured.get('quiet_share'))}",
                f"  uneven_threshold_db: {yaml_scalar(leveling.get('uneven_threshold_db'))}",
            ]
        )
    if production_id is None:
        lines.append("preprocessing: null")
    else:
        lines.extend(
            [
                "preprocessing:",
                "  provider: \"Auphonic\"",
                f"  production_id: {yaml_scalar(production_id)}",
                "  output_format: \"mp3\"",
                f"  output_bitrate_kbps: {mp3_bitrate}",
                "  algorithms:",
            ]
        )
        for key in sorted(algorithms):
            lines.append(f"    {key}: {yaml_scalar(algorithms[key])}")
    if second_pass is not None:
        lines.extend(
            [
                "second_pass:",
                f"  agreement: {yaml_scalar(second_pass.get('agreement'))}",
                f"  differences: {len(second_pass.get('differences', []))}",
            ]
        )
    lines.extend(
        [
            "speech_to_text:",
            "  provider: \"ElevenLabs\"",
            f"  model: {yaml_scalar(model_id)}",
            f"  requested_language: {yaml_scalar(language_code or 'auto')}",
            "  diarization: true",
            f"  diarization_threshold: {yaml_scalar(diarization_threshold)}",
            f"  max_speakers: {yaml_scalar(max_speakers)}",
            "  timestamp_granularity: \"word\"",
            f"  verbatim: {yaml_scalar(not clean_transcript)}",
            f"  audio_events: {yaml_scalar(audio_events)}",
            f"  speaker_library: {yaml_scalar(speaker_library)}",
            f"  speaker_roles: {yaml_scalar(speaker_roles)}",
            "---",
            "",
        ]
    )
    return lines


def _table_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _duration(seconds: float | int | None) -> str:
    return webvtt_timestamp(seconds)[:8]


def render_speaker_table(speakers: dict[str, Any]) -> list[str]:
    """Render the speaker table: one row per provider identity, names assignable later."""
    lines = [
        "## Speakers",
        "",
        "| Speaker | Name | Turns | Words | Speaking time | First heard | Note |",
        "| --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for row in speakers.get("speakers", []):
        lines.append(
            "| {id} | {name} | {turns} | {words} | {time} | {first} | {note} |".format(
                id=_table_cell(row.get("id")),
                name=_table_cell(row.get("name")),
                turns=row.get("turns", 0),
                words=row.get("words", 0),
                time=_duration(row.get("speaking_seconds")),
                first=webvtt_timestamp(row.get("first_start")),
                note=_table_cell(row.get("note")),
            )
        )
    lines.append("")
    return lines


def render_disagreement_note(counts: dict[str, int]) -> list[str]:
    """How to read a transcript merged from two passes. Neither pass is treated as right."""
    return [
        "This transcript merges two transcriptions of the same audio. Where they heard different words, both readings "
        "are shown as `{first | second}`, with `—` where one heard nothing. Where they gave the same words to different "
        "speakers, that stretch is its own turn labelled with both. "
        f"Word disagreements: {counts['word_disagreements']}. Speaker disagreements: {counts['speaker_disagreements']}.",
        "",
    ]


def render_transcript_markdown(
    *,
    transcript: dict[str, Any],
    speakers: dict[str, Any] | None = None,
    second_transcript: dict[str, Any] | None = None,
    **frontmatter: Any,
) -> str:
    lines = render_frontmatter(transcript=transcript, **frontmatter)
    turns = merge.merged_turns(transcript, second_transcript)
    names: dict[str, str] = {}
    if speakers is not None:
        names = {str(row["id"]): str(row["name"]) for row in speakers.get("speakers", []) if row.get("name")}
        lines.extend(render_speaker_table(speakers))
        lines.extend(["## Transcript", ""])
        if second_transcript is not None:
            lines.extend(render_disagreement_note(merge.summary(turns)))
    for turn in turns:
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        start = webvtt_timestamp(turn.get("start"))
        end = webvtt_timestamp(turn.get("end"))
        lines.append(f"[{start} --> {end}] **{merge.speaker_label(turn['speakers'], names)}:** {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
