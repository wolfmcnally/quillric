"""The transcription package: source identity, sidecar, speaker table, and derived Markdown.

The raw provider JSON is the immutable evidence. The sidecar describes the package, the speaker
table records names assigned to the provider's speaker identities, and the Markdown is derived
from those three and can be rendered again whenever the table changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import elevenlabs
from .render import render_transcript_markdown

PACKAGE_SCHEMA = "transcribe.package.v1"
SPEAKERS_SCHEMA = "transcribe.speakers.v1"


class PackageError(RuntimeError):
    """A package is missing, malformed, or asked to do something it cannot."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def speaker_rows(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per speaker identity that labels a turn, in order of first appearance."""
    words = transcript.get("words", [])
    rows: dict[str, dict[str, Any]] = {}
    for turn in elevenlabs.speaker_turns(words):
        if not str(turn.get("text") or "").strip():
            continue
        row = rows.setdefault(
            str(turn["speaker"]),
            {
                "id": str(turn["speaker"]),
                "name": None,
                "note": None,
                "turns": 0,
                "words": 0,
                "speaking_seconds": 0.0,
                "first_start": turn.get("start"),
                "last_end": turn.get("end"),
            },
        )
        row["turns"] += 1
        start, end = turn.get("start"), turn.get("end")
        if start is not None and end is not None:
            row["speaking_seconds"] = round(row["speaking_seconds"] + max(0.0, float(end) - float(start)), 3)
        if end is not None:
            row["last_end"] = end
    for word in words:
        if isinstance(word, dict) and word.get("type") in {None, "word"} and word.get("text"):
            row = rows.get(str(word.get("speaker_id") or "unassigned"))
            if row is not None:
                row["words"] += 1
    return list(rows.values())


def new_speaker_table(transcript: dict[str, Any], source_sha256: str) -> dict[str, Any]:
    return {
        "schema": SPEAKERS_SCHEMA,
        "source_sha256": source_sha256,
        "speakers": speaker_rows(transcript),
    }


def people(table: dict[str, Any]) -> dict[str, list[str]]:
    """Group speaker identities by assigned name; one person may hold several identities."""
    grouped: dict[str, list[str]] = {}
    for row in table.get("speakers", []):
        if row.get("name"):
            grouped.setdefault(str(row["name"]), []).append(str(row["id"]))
    return grouped


def assign_names(
    table: dict[str, Any],
    names: dict[str, str | None],
    notes: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Set or clear names (and notes) by speaker identity. The same name may be given to several
    identities. An identity the table does not hold is refused, so a typo cannot add a speaker."""
    known = {str(row["id"]) for row in table.get("speakers", [])}
    unknown = sorted((set(names) | set(notes or {})) - known)
    if unknown:
        raise PackageError(
            f"unknown speaker identity: {', '.join(unknown)}; known: {', '.join(sorted(known)) or 'none'}"
        )
    for row in table["speakers"]:
        identity = str(row["id"])
        if identity in names:
            value = names[identity]
            row["name"] = value.strip() if value and value.strip() else None
        if notes and identity in notes:
            value = notes[identity]
            row["note"] = value.strip() if value and value.strip() else None
    return table


def build_sidecar(
    *,
    version: str,
    generated_at: str,
    source: Path,
    adjusted: Path,
    raw_json: Path,
    speakers_name: str,
    markdown_name: str,
    transcript: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": PACKAGE_SCHEMA,
        "tool_version": version,
        "generated_at": generated_at,
        "source": {
            "filename": source.name,
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
        },
        "files": {
            "adjusted": {"filename": adjusted.name, "sha256": sha256_file(adjusted)},
            "raw": {"filename": raw_json.name, "sha256": sha256_file(raw_json)},
            "speakers": {"filename": speakers_name},
            "markdown": {"filename": markdown_name},
        },
        "transcript": {
            "transcription_id": transcript.get("transcription_id"),
            "detected_language": transcript.get("language_code"),
            "language_probability": transcript.get("language_probability"),
            "audio_duration_seconds": transcript.get("audio_duration_secs"),
            "word_timestamps": True,
        },
        "settings": settings,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass
class Package:
    """A published transcription package on disk."""

    directory: Path
    sidecar: dict[str, Any]
    speakers: dict[str, Any]

    @classmethod
    def load(cls, directory: Path) -> "Package":
        directory = Path(directory)
        sidecars = sorted(directory.glob("*-package.json"))
        if len(sidecars) != 1:
            raise PackageError(f"expected one *-package.json in {directory}, found {len(sidecars)}")
        sidecar = json.loads(sidecars[0].read_text(encoding="utf-8"))
        if sidecar.get("schema") != PACKAGE_SCHEMA:
            raise PackageError(f"unsupported package schema: {sidecar.get('schema')!r}")
        speakers = json.loads((directory / sidecar["files"]["speakers"]["filename"]).read_text(encoding="utf-8"))
        if speakers.get("schema") != SPEAKERS_SCHEMA:
            raise PackageError(f"unsupported speaker table schema: {speakers.get('schema')!r}")
        if speakers.get("source_sha256") != sidecar["source"]["sha256"]:
            raise PackageError("speaker table belongs to a different source recording")
        return cls(directory, sidecar, speakers)

    def path(self, role: str) -> Path:
        return self.directory / self.sidecar["files"][role]["filename"]

    @property
    def source_sha256(self) -> str:
        return str(self.sidecar["source"]["sha256"])

    def transcript(self) -> dict[str, Any]:
        """The raw provider response, verified against the hash the sidecar recorded."""
        raw = self.path("raw")
        if sha256_file(raw) != self.sidecar["files"]["raw"]["sha256"]:
            raise PackageError(f"raw transcript changed since packaging: {raw}")
        return json.loads(raw.read_text(encoding="utf-8"))

    def people(self) -> dict[str, list[str]]:
        return people(self.speakers)

    def render(self) -> str:
        return render_markdown(self.transcript(), self.sidecar, self.speakers)

    def assign(
        self, names: dict[str, str | None], notes: dict[str, str | None] | None = None
    ) -> None:
        """Assign names, then write the table and the re-rendered Markdown."""
        assign_names(self.speakers, names, notes)
        markdown = self.render()
        _write_json(self.path("speakers"), self.speakers)
        self.path("markdown").write_text(markdown, encoding="utf-8")


def render_markdown(
    transcript: dict[str, Any], sidecar: dict[str, Any], speakers: dict[str, Any]
) -> str:
    return render_transcript_markdown(
        transcript=transcript,
        speakers=speakers,
        source_name=sidecar["source"]["filename"],
        source_sha256=sidecar["source"]["sha256"],
        adjusted_name=sidecar["files"]["adjusted"]["filename"],
        generated_at=sidecar["generated_at"],
        **sidecar["settings"],
    )


def write_package_files(
    *,
    version: str,
    generated_at: str,
    source: Path,
    adjusted: Path,
    raw_json: Path,
    transcript: dict[str, Any],
    settings: dict[str, Any],
) -> Package:
    """Write raw JSON, speaker table, sidecar, and Markdown beside SOURCE's copy in its directory."""
    directory = raw_json.parent
    stem = source.stem
    _write_json(raw_json, transcript)
    sidecar = build_sidecar(
        version=version,
        generated_at=generated_at,
        source=source,
        adjusted=adjusted,
        raw_json=raw_json,
        speakers_name=f"{stem}-speakers.json",
        markdown_name=f"{stem}-transcription.md",
        transcript=transcript,
        settings=settings,
    )
    speakers = new_speaker_table(transcript, sidecar["source"]["sha256"])
    package = Package(directory, sidecar, speakers)
    _write_json(directory / f"{stem}-package.json", sidecar)
    _write_json(package.path("speakers"), speakers)
    package.path("markdown").write_text(render_markdown(transcript, sidecar, speakers), encoding="utf-8")
    return package
