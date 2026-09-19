"""Process audio with Auphonic, then create a diarized ElevenLabs transcript."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from . import auphonic, elevenlabs
from .package import write_package_files
from .render import (  # noqa: F401  (re-exported for callers of this module)
    render_frontmatter,
    render_transcript_markdown,
    transcript_speakers,
    webvtt_timestamp,
    yaml_scalar,
)


VERSION = "1.2.0"
PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_AUPHONIC_CONFIG = PACKAGE_ROOT / "leveling-only.json"
DEFAULT_DIARIZATION_THRESHOLD = 0.22
DEFAULT_MP3_BITRATE = 128


class PipelineError(RuntimeError):
    """A safe-to-display pipeline error."""


class ProgressReporter:
    """Render progress appropriately for terminals, logs, or automation."""

    SCHEMA = "transcribe.progress.v1"

    def __init__(self, mode: str = "auto", stream: TextIO | None = None):
        self.stream = stream or sys.stderr
        self.mode = (
            "tty"
            if mode == "auto" and self.stream.isatty()
            else "plain" if mode == "auto" else mode
        )
        self.started = time.monotonic()
        self._tty_line_open = False
        self._last_measured_update: dict[str, float] = {}

    @staticmethod
    def _fit_terminal_width(text: str, columns: int) -> str:
        """Truncate TEXT to terminal cells so a live status never wraps."""
        if columns <= 0:
            return ""
        widths = [
            0
            if unicodedata.combining(character)
            else 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
            for character in text
        ]
        if sum(widths) <= columns:
            return text
        if columns == 1:
            return "…"
        result: list[str] = []
        used = 0
        for character, width in zip(text, widths):
            if used + width > columns - 1:
                break
            result.append(character)
            used += width
        return "".join(result) + "…"

    def _terminal_columns(self) -> int:
        try:
            return os.get_terminal_size(self.stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            return shutil.get_terminal_size(fallback=(80, 24)).columns

    def event(
        self,
        stage: str,
        state: str,
        message: str,
        *,
        current: int | float | None = None,
        total: int | float | None = None,
        unit: str | None = None,
        **details: Any,
    ) -> None:
        if self.mode == "quiet":
            return
        percent = None
        if current is not None and total is not None and total > 0:
            percent = min(100.0, max(0.0, float(current) / float(total) * 100.0))
        now = time.monotonic()
        if state == "active" and current is not None:
            measurement_complete = total is not None and current >= total
            last_update = self._last_measured_update.get(stage)
            if (
                not measurement_complete
                and last_update is not None
                and now - last_update < 0.2
            ):
                return
            self._last_measured_update[stage] = now
        elapsed = round(now - self.started, 3)
        if self.mode == "json":
            record: dict[str, Any] = {
                "schema": self.SCHEMA,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": elapsed,
                "stage": stage,
                "state": state,
                "message": message,
            }
            if current is not None:
                record["current"] = current
            if total is not None:
                record["total"] = total
            if unit is not None:
                record["unit"] = unit
            if percent is not None:
                record["percent"] = round(percent, 1)
            record.update(details)
            print(json.dumps(record, ensure_ascii=False), file=self.stream, flush=True)
            return

        progress_text = f" {percent:5.1f}%" if percent is not None else ""
        elapsed_text = f" ({elapsed:.1f}s)" if state == "active" else ""
        line = f"[{stage}] {state}: {message}{progress_text}{elapsed_text}"
        if self.mode == "tty":
            columns = self._terminal_columns()
            line = self._fit_terminal_width(line, max(1, columns - 1))
            if state == "active":
                self.stream.write(f"\r\033[2K{line}")
                self._tty_line_open = True
            else:
                if self._tty_line_open:
                    self.stream.write("\r\033[2K")
                self.stream.write(line + "\n")
                self._tty_line_open = False
            self.stream.flush()
            return
        print(line, file=self.stream, flush=True)


def byte_progress(
    reporter: ProgressReporter, stage: str, message: str
) -> Callable[[int, int | None], None]:
    def report(current: int, total: int | None) -> None:
        reporter.event(
            stage,
            "active",
            message,
            current=current,
            total=total,
            unit="bytes",
        )

    return report


def copy_with_progress(
    source: Path,
    destination: Path,
    progress: Callable[[int, int | None], None],
) -> None:
    total = source.stat().st_size
    copied = 0
    progress(copied, total)
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while chunk := input_file.read(1024 * 1024):
            output_file.write(chunk)
            copied += len(chunk)
            progress(copied, total)
    shutil.copystat(source, destination)


def default_output_dir(source: Path) -> Path:
    """Return SOURCE's sibling directory named for its final-extension-free stem."""
    return source.parent / source.stem


def resolved_settings(args: argparse.Namespace) -> tuple[dict[str, Any], float | None]:
    config_path = (
        args.auphonic_config.expanduser().resolve()
        if args.auphonic_config is not None
        else DEFAULT_AUPHONIC_CONFIG
    )
    overrides = list(args.auphonic_set)
    if args.speech_leveler_strength is not None:
        overrides.append(f"levelerstrength_speech={args.speech_leveler_strength}")
    algorithms = auphonic.resolve_algorithms(config_path, False, overrides)
    threshold = (
        None
        if args.max_speakers is not None
        else (
            args.diarization_threshold
            if args.diarization_threshold is not None
            else DEFAULT_DIARIZATION_THRESHOLD
        )
    )
    return algorithms, threshold


def validate_args(args: argparse.Namespace, source: Path, output_dir: Path) -> None:
    if not source.is_file():
        raise PipelineError(f"input file does not exist: {source}")
    if output_dir == source:
        raise PipelineError("output directory cannot be the input file")
    if source.is_relative_to(output_dir):
        raise PipelineError(
            "output directory cannot contain the input file; choose a sibling or "
            "another directory"
        )
    if not 32 <= args.mp3_bitrate <= 320:
        raise PipelineError("MP3 bitrate must be between 32 and 320 kbps")
    if args.diarization_threshold is not None and not 0.1 <= args.diarization_threshold <= 0.4:
        raise PipelineError("diarization threshold must be between 0.1 and 0.4")
    if args.speaker_roles and args.max_speakers not in {None, 2}:
        raise PipelineError("speaker-role detection is only compatible with at most two speakers")
    if args.poll_interval <= 0:
        raise PipelineError("poll interval must be greater than zero")
    if args.wait_timeout <= 0:
        raise PipelineError("wait timeout must be greater than zero")
    if args.resume_auphonic_production is not None and not re.fullmatch(
        r"[A-Za-z0-9_-]+", args.resume_auphonic_production
    ):
        raise PipelineError("Auphonic production ID contains invalid characters")


def replace_directory(staging: Path, destination: Path, force: bool) -> None:
    if not destination.exists():
        staging.replace(destination)
        return
    if not force:
        raise PipelineError(f"output directory already exists: {destination}; use --force")
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    destination.replace(backup)
    try:
        staging.replace(destination)
    except OSError:
        backup.replace(destination)
        raise
    try:
        if backup.is_dir():
            shutil.rmtree(backup)
        else:
            backup.unlink()
    except OSError as error:
        print(
            f"warning: output completed, but backup cleanup failed: {backup}: {error}",
            file=sys.stderr,
        )


def run_pipeline(args: argparse.Namespace) -> Path:
    progress_mode = "quiet" if args.quiet else args.progress
    reporter = ProgressReporter(progress_mode)
    staging: Path | None = None
    try:
        reporter.event("validation", "active", "Resolving paths and settings")
        source = args.input_file.expanduser().resolve()
        output_dir = (
            args.output_dir.expanduser().resolve()
            if args.output_dir is not None
            else default_output_dir(source)
        )
        validate_args(args, source, output_dir)
        algorithms, diarization_threshold = resolved_settings(args)
        settings_summary = {
            "input": str(source),
            "output_directory": str(output_dir),
            "auphonic": {
                "algorithms": algorithms,
                "mp3_bitrate_kbps": args.mp3_bitrate,
                "resume_production": args.resume_auphonic_production,
            },
            "elevenlabs": {
                "model": args.model,
                "language_code": args.language_code or "auto",
                "diarization_threshold": diarization_threshold,
                "max_speakers": args.max_speakers,
                "audio_events": args.audio_events,
                "verbatim": not args.clean_transcript,
                "speaker_library": args.speaker_library,
                "speaker_roles": args.speaker_roles,
            },
        }
        reporter.event("validation", "completed", "Paths and settings are valid")
        if args.dry_run:
            print(json.dumps(settings_summary, indent=2, ensure_ascii=False))
            reporter.event("pipeline", "completed", "Dry run complete")
            return output_dir
        if output_dir.exists() and not args.force:
            raise PipelineError(
                f"output directory already exists: {output_dir}; use --force"
            )

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{output_dir.name}.transcribe-", dir=output_dir.parent
            )
        )
        source_copy = staging / source.name
        adjusted_name = f"{source.stem}-adjusted.mp3"
        adjusted_path = staging / adjusted_name
        raw_json_path = staging / f"{source.stem}-raw.json"

        copy_callback = byte_progress(reporter, "copy", f"Copying {source.name}")
        copy_with_progress(source, source_copy, copy_callback)
        reporter.event(
            "copy",
            "completed",
            f"Copied {source.name}",
            current=source.stat().st_size,
            total=source.stat().st_size,
            unit="bytes",
        )

        auphonic_client = auphonic.AuphonicClient(auphonic.load_api_key())
        details: dict[str, Any] | None = None
        effective_algorithms = algorithms
        effective_mp3_bitrate = args.mp3_bitrate
        if args.resume_auphonic_production is not None:
            production_id = args.resume_auphonic_production
            reporter.event(
                "auphonic_resume",
                "active",
                "Loading existing Auphonic production",
                production_id=production_id,
            )
            details = auphonic_client.details(production_id)
            remote_algorithms = details.get("algorithms")
            if isinstance(remote_algorithms, dict):
                effective_algorithms = remote_algorithms
            reporter.event(
                "auphonic_resume",
                "completed",
                "Existing Auphonic production loaded",
                production_id=production_id,
            )
        else:
            reporter.event(
                "auphonic_submit", "active", "Creating Auphonic production"
            )
            output_file = {
                "format": "mp3",
                "bitrate": str(args.mp3_bitrate),
                "filename": adjusted_name,
            }
            production_id = auphonic_client.create_production(
                source_copy,
                source.stem + "-adjusted",
                algorithms,
                output_file,
            )
            reporter.event(
                "auphonic_submit",
                "completed",
                "Auphonic production created",
                production_id=production_id,
            )

            auphonic_client.upload(
                production_id,
                source_copy,
                progress=byte_progress(
                    reporter, "auphonic_upload", "Uploading source to Auphonic"
                ),
            )
            reporter.event(
                "auphonic_upload",
                "completed",
                "Source uploaded to Auphonic",
                current=source_copy.stat().st_size,
                total=source_copy.stat().st_size,
                unit="bytes",
            )
            reporter.event(
                "auphonic_processing",
                "active",
                "Starting Auphonic processing",
                production_id=production_id,
            )
            auphonic_client.start(production_id)

        def report_auphonic_status(
            production: dict[str, Any], status_changed: bool
        ) -> None:
            remote_status = production.get("status")
            status_message = str(
                production.get("status_string") or f"status {remote_status}"
            )
            reporter.event(
                "auphonic_processing",
                "active",
                status_message,
                production_id=production_id,
                remote_status=remote_status,
                remote_status_changed=status_changed,
            )

        if details is None or details.get("status") != auphonic.TERMINAL_STATUS_DONE:
            details = auphonic.wait_for_completion(
                auphonic_client,
                production_id,
                args.poll_interval,
                args.wait_timeout,
                status_callback=report_auphonic_status,
            )
        for remote_output in details.get("output_files", []):
            if not isinstance(remote_output, dict) or remote_output.get("format") != "mp3":
                continue
            remote_bitrate = remote_output.get("bitrate")
            if isinstance(remote_bitrate, int):
                effective_mp3_bitrate = remote_bitrate
            elif isinstance(remote_bitrate, str) and remote_bitrate.isdecimal():
                effective_mp3_bitrate = int(remote_bitrate)
            break
        reporter.event(
            "auphonic_processing",
            "completed",
            "Auphonic processing complete",
            production_id=production_id,
        )
        download_url = auphonic._result_download_url(details, "mp3")
        auphonic_client.download(
            download_url,
            adjusted_path,
            progress=byte_progress(
                reporter, "auphonic_download", "Downloading adjusted MP3"
            ),
        )
        if not adjusted_path.is_file() or adjusted_path.stat().st_size == 0:
            raise PipelineError("Auphonic returned an empty adjusted MP3")
        reporter.event(
            "auphonic_download",
            "completed",
            "Adjusted MP3 downloaded",
            current=adjusted_path.stat().st_size,
            total=adjusted_path.stat().st_size,
            unit="bytes",
        )

        elevenlabs_client = elevenlabs.ElevenLabsClient(elevenlabs.load_api_key())
        elevenlabs_upload_completed = False
        transcription_stop = threading.Event()
        transcription_heartbeat: threading.Thread | None = None
        elevenlabs_upload_progress = byte_progress(
            reporter,
            "elevenlabs_upload",
            "Uploading adjusted MP3 to ElevenLabs",
        )

        def heartbeat_while_transcribing() -> None:
            while not transcription_stop.wait(10.0):
                reporter.event(
                    "elevenlabs_transcription",
                    "active",
                    "Waiting for diarized transcription",
                )

        def report_elevenlabs_upload(current: int, total: int | None) -> None:
            nonlocal elevenlabs_upload_completed, transcription_heartbeat
            elevenlabs_upload_progress(current, total)
            if (
                total is not None
                and current >= total
                and not elevenlabs_upload_completed
            ):
                reporter.event(
                    "elevenlabs_upload",
                    "completed",
                    "Adjusted MP3 uploaded to ElevenLabs",
                    current=current,
                    total=total,
                    unit="bytes",
                )
                reporter.event(
                    "elevenlabs_transcription",
                    "active",
                    "Waiting for diarized transcription",
                )
                transcription_heartbeat = threading.Thread(
                    target=heartbeat_while_transcribing,
                    name="transcribe-progress",
                    daemon=True,
                )
                transcription_heartbeat.start()
                elevenlabs_upload_completed = True

        try:
            transcript = elevenlabs_client.transcribe(
                adjusted_path,
                args.max_speakers,
                diarization_threshold,
                model_id=args.model,
                language_code=args.language_code,
                tag_audio_events=args.audio_events,
                no_verbatim=args.clean_transcript,
                use_speaker_library=args.speaker_library,
                detect_speaker_roles=args.speaker_roles,
                progress=report_elevenlabs_upload,
            )
        finally:
            transcription_stop.set()
            if transcription_heartbeat is not None:
                transcription_heartbeat.join()
        if not elevenlabs_upload_completed:
            reporter.event(
                "elevenlabs_upload",
                "completed",
                "Adjusted MP3 uploaded to ElevenLabs",
            )
        reporter.event(
            "elevenlabs_transcription",
            "completed",
            "Diarized transcription received",
            word_count=sum(
                1
                for word in transcript.get("words", [])
                if isinstance(word, dict) and word.get("type") == "word"
            ),
            speaker_count=len(transcript_speakers(transcript)),
        )

        reporter.event("artifacts", "active", "Writing JSON, sidecar, speaker table, and Markdown")
        write_package_files(
            version=VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source=source_copy,
            adjusted=adjusted_path,
            raw_json=raw_json_path,
            transcript=transcript,
            settings={
                "production_id": production_id,
                "algorithms": effective_algorithms,
                "mp3_bitrate": effective_mp3_bitrate,
                "model_id": args.model,
                "language_code": args.language_code,
                "diarization_threshold": diarization_threshold,
                "max_speakers": args.max_speakers,
                "audio_events": args.audio_events,
                "clean_transcript": args.clean_transcript,
                "speaker_library": args.speaker_library,
                "speaker_roles": args.speaker_roles,
            },
        )
        reporter.event("artifacts", "completed", "JSON, sidecar, speaker table, and Markdown written")
        reporter.event("publish", "active", "Publishing completed package")
        replace_directory(staging, output_dir, args.force)
        staging = None
        reporter.event("publish", "completed", "Package published")
        reporter.event(
            "pipeline", "completed", "Transcription package complete", output=str(output_dir)
        )
        print(output_dir, flush=True)
        return output_dir
    except BaseException as error:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        message = "Interrupted" if isinstance(error, KeyboardInterrupt) else str(error)
        reporter.event("pipeline", "failed", message or type(error).__name__)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="transcribe",
        description=__doc__,
        epilog="Run 'transcribe speakers PACKAGE [ID=NAME ...]' to show or edit a package's speaker table, and 'transcribe duplicates PATH ...' to find the same audio in other formats.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input_file", type=Path)
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--progress",
        choices=("auto", "tty", "plain", "json"),
        default="auto",
        help="progress format; auto uses a live TTY line or plain redirected logs",
    )
    output_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress routine progress; keep the result path and errors",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="exact output directory (default: INPUT_DIR/INPUT_STEM)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing output directory")
    parser.add_argument("--dry-run", action="store_true", help="print resolved settings without API calls or writes")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    auphonic_group = parser.add_argument_group("Auphonic preprocessing")
    auphonic_group.add_argument(
        "--resume-auphonic-production",
        metavar="UUID",
        help="reuse an existing Auphonic production and continue from download",
    )
    auphonic_group.add_argument(
        "--auphonic-config",
        type=Path,
        help=f"algorithm JSON (default: {DEFAULT_AUPHONIC_CONFIG})",
    )
    auphonic_group.add_argument(
        "--auphonic-set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override an Auphonic algorithm value; repeat as needed",
    )
    auphonic_group.add_argument(
        "--speech-leveler-strength",
        type=int,
        metavar="N",
        help="convenience override for levelerstrength_speech",
    )
    auphonic_group.add_argument(
        "--mp3-bitrate",
        type=int,
        default=DEFAULT_MP3_BITRATE,
        metavar="KBPS",
        help="Auphonic MP3 output bitrate",
    )

    stt_group = parser.add_argument_group("ElevenLabs transcription")
    stt_group.add_argument("--model", default="scribe_v2")
    stt_group.add_argument(
        "--language-code",
        metavar="CODE",
        help="ISO-639 language code; omit for automatic detection",
    )
    speaker_count = stt_group.add_mutually_exclusive_group()
    speaker_count.add_argument(
        "--diarization-threshold",
        type=float,
        metavar="N",
        help=f"automatic speaker split/merge threshold (default: {DEFAULT_DIARIZATION_THRESHOLD})",
    )
    speaker_count.add_argument(
        "--max-speakers",
        type=int,
        choices=range(1, 33),
        metavar="N",
        help="maximum speaker count instead of threshold-based automatic detection",
    )
    stt_group.add_argument("--audio-events", action="store_true", help="include detected non-speech event tags")
    stt_group.add_argument("--clean-transcript", action="store_true", help="remove fillers, false starts, and disfluencies")
    stt_group.add_argument("--speaker-library", action="store_true", help="match against the ElevenLabs workspace speaker library")
    stt_group.add_argument("--speaker-roles", action="store_true", help="label two-party calls as agent/customer (10%% surcharge)")

    parser.add_argument("--poll-interval", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("--wait-timeout", type=float, default=7200.0, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ["speakers"]:
        # A recording literally named "speakers" is still reachable as ./speakers.
        from . import speakers_cli

        return speakers_cli.main(arguments[1:])
    if arguments[:1] == ["duplicates"]:
        from . import fingerprint_cli

        return fingerprint_cli.main(arguments[1:])
    try:
        run_pipeline(parse_args(arguments))
    except (PipelineError, auphonic.AuphonicError, elevenlabs.TranscriptionError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
