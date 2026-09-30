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

from . import elevenlabs, merge
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


def new_speaker_table(transcript: dict[str, Any], source_sha256: str, second_transcript: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = speaker_rows(transcript)
    if second_transcript is not None:
        # A voice only the second pass told apart still needs a row, or it could never be named.
        turns = merge.merged_turns(transcript, second_transcript)
        for identity in merge.second_only_speakers(turns):
            mine = [turn for turn in turns if identity in turn["speakers"]]
            rows.append({
                "id": identity, "name": None, "note": "heard as a separate voice by the second pass only",
                "turns": len(mine), "words": sum(len(turn["text"].split()) for turn in mine),
                "speaking_seconds": round(sum(max(0.0, float(turn["end"] or 0) - float(turn["start"] or 0)) for turn in mine), 3),
                "first_start": mine[0]["start"], "last_end": mine[-1]["end"],
            })
    return {
        "schema": SPEAKERS_SCHEMA,
        "source_sha256": source_sha256,
        "speakers": rows,
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
    adjusted: Path | None,
    raw_json: Path,
    speakers_name: str,
    markdown_name: str,
    transcript: dict[str, Any],
    settings: dict[str, Any],
    leveling: dict[str, Any] | None = None,
    second_pass: dict[str, Any] | None = None,
    second_raw: Path | None = None,
) -> dict[str, Any]:
    files: dict[str, Any] = {
        "adjusted": {"filename": adjusted.name, "sha256": sha256_file(adjusted)} if adjusted is not None else None,
        "raw": {"filename": raw_json.name, "sha256": sha256_file(raw_json)},
        "speakers": {"filename": speakers_name},
        "markdown": {"filename": markdown_name},
    }
    if second_raw is not None:
        files["second_raw"] = {"filename": second_raw.name, "sha256": sha256_file(second_raw)}
    return {
        "schema": PACKAGE_SCHEMA,
        "tool_version": version,
        "generated_at": generated_at,
        "source": {
            "filename": source.name,
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
        },
        "files": files,
        "leveling": leveling or {"mode": "on", "applied": adjusted is not None, "uneven_threshold_db": None, "levels": None},
        "second_pass": second_pass,
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


def _package_path(directory: Path, filename: str) -> Path:
    """Confine a flat package filename to its resolved directory."""
    if (not isinstance(filename, str) or not filename or filename in {".", ".."}
            or "/" in filename or "\\" in filename or "\x00" in filename):
        raise PackageError("package filenames must be nonempty basenames")
    path = directory / filename
    try:
        path.resolve(strict=path.is_symlink()).relative_to(directory.resolve())
    except (ValueError, OSError, RuntimeError) as error:
        raise PackageError("package file escapes its directory") from error
    return path


@dataclass
class Package:
    """A published transcription package on disk."""

    directory: Path
    sidecar: dict[str, Any]
    speakers: dict[str, Any]

    @classmethod
    def load(cls, directory: Path) -> "Package":
        directory = Path(directory).resolve()
        sidecars = sorted(directory.glob("*-package.json"))
        if len(sidecars) != 1:
            raise PackageError(f"expected one *-package.json in {directory}, found {len(sidecars)}")
        sidecar = json.loads(_package_path(directory, sidecars[0].name).read_text(encoding="utf-8"))
        if sidecar.get("schema") != PACKAGE_SCHEMA:
            raise PackageError(f"unsupported package schema: {sidecar.get('schema')!r}")
        package = cls(directory, sidecar, {})
        package._validate_paths()
        speakers = json.loads(package.path("speakers").read_text(encoding="utf-8"))
        if speakers.get("schema") != SPEAKERS_SCHEMA:
            raise PackageError(f"unsupported speaker table schema: {speakers.get('schema')!r}")
        if speakers.get("source_sha256") != sidecar["source"]["sha256"]:
            raise PackageError("speaker table belongs to a different source recording")
        return cls(directory, sidecar, speakers)

    def _validate_paths(self) -> None:
        # Identity-only source records have no path to dereference; preserve their
        # historical loadability while validating any filename that is supplied.
        if "filename" in self.sidecar["source"]:
            _package_path(self.directory, self.sidecar["source"]["filename"])
        for entry in self.sidecar["files"].values():
            if entry is not None:
                _package_path(self.directory, entry["filename"])

    def path(self, role: str) -> Path:
        return _package_path(self.directory, self.sidecar["files"][role]["filename"])

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

    def second_transcript(self) -> dict[str, Any] | None:
        """The second pass, when one was made, verified against the hash the sidecar recorded."""
        entry = self.sidecar["files"].get("second_raw")
        if not entry:
            return None
        path = self.path("second_raw")
        if sha256_file(path) != entry["sha256"]:
            raise PackageError(f"second transcript changed since packaging: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def merged_turns(self) -> list[dict[str, Any]]:
        """The transcript as turns, with both passes' disagreements kept in the flow."""
        return merge.merged_turns(self.transcript(), self.second_transcript())

    def render(self) -> str:
        return render_markdown(self.transcript(), self.sidecar, self.speakers, self.second_transcript())

    def assign(
        self, names: dict[str, str | None], notes: dict[str, str | None] | None = None
    ) -> None:
        """Assign names, then write the table and the re-rendered Markdown."""
        self._validate_paths()
        assign_names(self.speakers, names, notes)
        markdown = self.render()
        _write_json(self.path("speakers"), self.speakers)
        self.path("markdown").write_text(markdown, encoding="utf-8")


def render_markdown(
    transcript: dict[str, Any], sidecar: dict[str, Any], speakers: dict[str, Any], second_transcript: dict[str, Any] | None = None
) -> str:
    return render_transcript_markdown(
        transcript=transcript,
        speakers=speakers,
        second_transcript=second_transcript,
        source_name=sidecar["source"]["filename"],
        source_sha256=sidecar["source"]["sha256"],
        adjusted_name=(sidecar["files"]["adjusted"] or {}).get("filename"),
        leveling=sidecar.get("leveling"),
        second_pass=sidecar.get("second_pass"),
        generated_at=sidecar["generated_at"],
        **sidecar["settings"],
    )


def write_package_files(
    *,
    version: str,
    generated_at: str,
    source: Path,
    adjusted: Path | None,
    raw_json: Path,
    transcript: dict[str, Any],
    settings: dict[str, Any],
    leveling: dict[str, Any] | None = None,
    second_pass: dict[str, Any] | None = None,
    second_transcript: dict[str, Any] | None = None,
) -> Package:
    """Write raw JSON, speaker table, sidecar, and Markdown beside SOURCE's copy in its directory."""
    directory = raw_json.parent
    stem = source.stem
    _write_json(raw_json, transcript)
    second_raw: Path | None = None
    if second_transcript is not None:
        second_raw = directory / f"{stem}-second-raw.json"
        _write_json(second_raw, second_transcript)
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
        leveling=leveling,
        second_pass=second_pass,
        second_raw=second_raw,
    )
    speakers = new_speaker_table(transcript, sidecar["source"]["sha256"], second_transcript)
    package = Package(directory, sidecar, speakers)
    _write_json(directory / f"{stem}-package.json", sidecar)
    _write_json(package.path("speakers"), speakers)
    package.path("markdown").write_text(render_markdown(transcript, sidecar, speakers, second_transcript), encoding="utf-8")
    return package
