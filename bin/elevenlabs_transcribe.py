#!/usr/bin/env python3
"""Create diarized, word-timestamped ElevenLabs Scribe v2 transcripts."""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import re
import shlex
import ssl
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable


API_BASE = "https://api.elevenlabs.io"


class TranscriptionError(RuntimeError):
    """A safe-to-display transcription error."""


def _parse_dotenv_value(raw_value: str) -> str:
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    values = list(lexer)
    return values[0] if values else ""


def load_api_key() -> str:
    for variable in ("ELEVENLABS_API_KEY", "ELEVEN_LABS_API_KEY"):
        if value := os.environ.get(variable):
            return value

    candidates = (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for line in resolved.read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\s*(?:export\s+)?(ELEVENLABS_API_KEY|ELEVEN_LABS_API_KEY)\s*=\s*(.*)$",
                line,
            )
            if match and (value := _parse_dotenv_value(match.group(2))):
                return value
    raise TranscriptionError(
        "No ElevenLabs key found; set ELEVENLABS_API_KEY or add it to .env"
    )


def _form_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def transcription_fields(
    num_speakers: int | None = None,
    diarization_threshold: float | None = None,
    *,
    model_id: str = "scribe_v2",
    language_code: str | None = "eng",
    tag_audio_events: bool = False,
    no_verbatim: bool = False,
    use_speaker_library: bool = False,
    detect_speaker_roles: bool = False,
) -> dict[str, str]:
    fields = {
        "model_id": model_id,
        "diarize": "true",
        "tag_audio_events": str(tag_audio_events).lower(),
        "timestamps_granularity": "word",
        "no_verbatim": str(no_verbatim).lower(),
    }
    if language_code is not None:
        fields["language_code"] = language_code
    if num_speakers is not None:
        fields["num_speakers"] = str(num_speakers)
    if diarization_threshold is not None:
        fields["diarization_threshold"] = str(diarization_threshold)
    if use_speaker_library:
        fields["use_speaker_library"] = "true"
    if detect_speaker_roles:
        fields["detect_speaker_roles"] = "true"
    return fields


class ElevenLabsClient:
    def __init__(self, api_key: str, api_base: str = API_BASE, timeout: float = 900.0):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def transcribe(
        self,
        audio_path: Path,
        num_speakers: int | None = None,
        diarization_threshold: float | None = None,
        *,
        model_id: str = "scribe_v2",
        language_code: str | None = "eng",
        tag_audio_events: bool = False,
        no_verbatim: bool = False,
        use_speaker_library: bool = False,
        detect_speaker_roles: bool = False,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise TranscriptionError(f"Unsupported API base URL: {self.api_base}")
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname,
                port=parsed.port,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname, port=parsed.port, timeout=self.timeout
            )

        boundary = f"----elevenlabs-{uuid.uuid4().hex}"
        fields = transcription_fields(
            num_speakers,
            diarization_threshold,
            model_id=model_id,
            language_code=language_code,
            tag_audio_events=tag_audio_events,
            no_verbatim=no_verbatim,
            use_speaker_library=use_speaker_library,
            detect_speaker_roles=detect_speaker_roles,
        )
        field_body = b"".join(
            _form_field(boundary, name, value) for name, value in fields.items()
        )
        safe_name = audio_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        audio_size = audio_path.stat().st_size
        content_length = len(field_body) + len(file_header) + audio_size + len(suffix)
        request_path = f"{parsed.path.rstrip('/')}/v1/speech-to-text"

        try:
            connection.putrequest("POST", request_path)
            connection.putheader("xi-api-key", self.api_key)
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={boundary}"
            )
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            connection.send(field_body)
            connection.send(file_header)
            bytes_sent = 0
            if progress is not None:
                progress(bytes_sent, audio_size)
            with audio_path.open("rb") as audio:
                while chunk := audio.read(1024 * 1024):
                    connection.send(chunk)
                    bytes_sent += len(chunk)
                    if progress is not None:
                        progress(bytes_sent, audio_size)
            connection.send(suffix)
            response = connection.getresponse()
            response_body = response.read()
        except (OSError, http.client.HTTPException) as error:
            raise TranscriptionError(f"Transcription request failed: {error}") from error
        finally:
            connection.close()

        try:
            document = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TranscriptionError(
                f"ElevenLabs returned HTTP {response.status} with a non-JSON response"
            ) from error
        if response.status != 200:
            detail = document.get("detail") if isinstance(document, dict) else None
            if isinstance(detail, dict):
                message = detail.get("message") or detail.get("status")
            else:
                message = detail
            raise TranscriptionError(
                f"ElevenLabs returned HTTP {response.status}: {message or 'unknown error'}"
            )
        if not isinstance(document, dict) or not isinstance(document.get("words"), list):
            raise TranscriptionError("ElevenLabs returned an unexpected transcript shape")
        return document


def format_timestamp(seconds: float | int | None) -> str:
    value = max(0.0, float(seconds or 0.0))
    minutes, remainder = divmod(value, 60)
    return f"{int(minutes):02d}:{remainder:06.3f}"


def speaker_turns(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in words:
        text = word.get("text")
        if not isinstance(text, str) or not text:
            continue
        speaker = str(word.get("speaker_id") or "unassigned")
        if word.get("type") == "spacing":
            if current is not None and current["speaker"] == speaker:
                current["text"] += text
            continue
        start = word.get("start")
        end = word.get("end")
        if current is None or current["speaker"] != speaker:
            current = {
                "speaker": speaker,
                "start": start,
                "end": end,
                "text": text,
            }
            turns.append(current)
        else:
            current["text"] += text
            if end is not None:
                current["end"] = end
    return turns


def render_markdown(audio_path: Path, transcript: dict[str, Any]) -> str:
    lines = [
        f"# {audio_path.name}",
        "",
        f"Language: `{transcript.get('language_code', 'unknown')}`",
        "",
    ]
    for turn in speaker_turns(transcript["words"]):
        text = turn["text"].strip()
        if not text:
            continue
        lines.append(
            f"[{format_timestamp(turn['start'])}–{format_timestamp(turn['end'])}] "
            f"**{turn['speaker']}**: {text}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_transcript(
    output_dir: Path, audio_path: Path, transcript: dict[str, Any]
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{audio_path.stem}.json"
    markdown_path = output_dir / f"{audio_path.stem}.md"
    json_partial = json_path.with_suffix(".json.part")
    markdown_partial = markdown_path.with_suffix(".md.part")
    json_partial.write_text(
        json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    markdown_partial.write_text(render_markdown(audio_path, transcript), encoding="utf-8")
    json_partial.replace(json_path)
    markdown_partial.replace(markdown_path)
    return json_path, markdown_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_files", nargs="+", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("transcripts"),
        help="transcript directory (default: transcripts)",
    )
    parser.add_argument("--force", action="store_true", help="replace existing transcripts")
    parser.add_argument(
        "--num-speakers",
        type=int,
        choices=range(1, 33),
        metavar="N",
        help="maximum number of content speakers (1–32)",
    )
    parser.add_argument(
        "--diarization-threshold",
        type=float,
        metavar="N",
        help="speaker merge/split threshold (0.1–0.4; requires automatic speaker count)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.diarization_threshold is not None:
        if not 0.1 <= args.diarization_threshold <= 0.4:
            print("error: diarization threshold must be between 0.1 and 0.4", file=sys.stderr)
            return 2
        if args.num_speakers is not None:
            print(
                "error: --diarization-threshold cannot be combined with --num-speakers",
                file=sys.stderr,
            )
            return 2
    sources = [path.expanduser().resolve() for path in args.audio_files]
    missing = [path for path in sources if not path.is_file()]
    if missing:
        for path in missing:
            print(f"error: audio file does not exist: {path}", file=sys.stderr)
        return 2
    output_dir = args.output_dir.expanduser().resolve()

    try:
        client = ElevenLabsClient(load_api_key())
        for index, source in enumerate(sources, start=1):
            json_path = output_dir / f"{source.stem}.json"
            markdown_path = output_dir / f"{source.stem}.md"
            if not args.force and json_path.exists() and markdown_path.exists():
                print(f"[{index}/{len(sources)}] Existing: {source.name}", flush=True)
                continue
            print(f"[{index}/{len(sources)}] Transcribing: {source.name}", flush=True)
            transcript = client.transcribe(
                source, args.num_speakers, args.diarization_threshold
            )
            written_json, written_markdown = write_transcript(
                output_dir, source, transcript
            )
            print(f"  JSON: {written_json}", flush=True)
            print(f"  Text: {written_markdown}", flush=True)
    except (OSError, TranscriptionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
