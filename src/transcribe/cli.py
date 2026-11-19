"""Process audio with Auphonic, then create a diarized ElevenLabs transcript."""

from __future__ import annotations

import argparse
import errno
import fcntl
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

from . import auphonic, elevenlabs, levels, media
from .config import ConfigurationError, Settings, Vendor, load_settings
from .providers import (
    Command, CommandDiarization, CommandTranscription, DiarizationProvider, LevelingProvider,
    ProviderError, TranscriptionProvider, work_environment,
)
from .second_pass import SCHEMA as SECOND_PASS_SCHEMA, compare as compare_passes
from .package import project_diarization, write_package_files
from .render import (  # noqa: F401  (re-exported for callers of this module)
    render_frontmatter,
    render_transcript_markdown,
    transcript_speakers,
    webvtt_timestamp,
    yaml_scalar,
)


VERSION = "0.1.0"
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


def _absolute(path: Path, explicit: bool) -> Path:
    if explicit and str(path).startswith("~"):
        raise PipelineError("home-relative paths are unavailable in explicit configuration")
    return (path if explicit else path.expanduser()).resolve()


def _vendor_key(key: str | None, vendor: Vendor, *, explicit: bool,
                standalone: Callable[[], str], provider: str) -> str:
    if key is not None:
        return key
    if vendor.api_key is not None:
        if value := os.environ.get(vendor.api_key.name):
            return value
        raise PipelineError(f"{provider}: configured secret is unavailable")
    if explicit:
        raise PipelineError(f"{provider}: explicit configuration requires a secret reference or caller credentials")
    return standalone()


def _vendor_options(vendor: Vendor, *, explicit: bool, eleven: bool = False) -> dict:
    options = {"explicit": True, "ca_file": vendor.ca_file} if explicit else {}
    if vendor.api_base is not None:
        options["api_base"] = vendor.api_base
    if eleven and vendor.allow_insecure_local_http:
        options["allow_insecure_local_http"] = True
    return options


def _preflight(args: argparse.Namespace, settings: Settings) -> None:
    first = settings.transcription
    second = first if settings.second_provider is None else settings.second_provider
    if (first not in ("elevenlabs", "command") or second not in ("elevenlabs", "command")
            or settings.diarization not in ("vendor", "command", "off")
            or settings.leveling not in ("auphonic", "ffmpeg", "off")):
        raise PipelineError("unknown configured provider")
    if type(settings.local_only) is not bool or type(settings.allow_brokered) is not bool:
        raise PipelineError("egress policy settings must be booleans")
    if first == "command" and second == "elevenlabs":
        raise PipelineError("ElevenLabs second pass requires an ElevenLabs first pass")
    if (first == "command") != (settings.transcription_command is not None):
        raise PipelineError("transcription provider and command settings conflict")
    if (settings.diarization == "command") != (settings.diarization_command is not None):
        raise PipelineError("diarization provider and command settings conflict")
    if (second != "command" and settings.second_command is not None
            or second == "command" and first != "command" and settings.second_command is None):
        raise PipelineError("second-pass provider and command settings conflict")
    for specification in (settings.transcription_command, settings.diarization_command, settings.second_command):
        if specification is not None:
            if not isinstance(specification, Command):
                raise PipelineError("invalid command settings")
            try:
                specification.validate()
            except ProviderError as error:
                raise PipelineError(str(error)) from None
    active_second = args.second_pass != "off"
    active_leveling = args.leveling != "off" or args.resume_auphonic_production is not None
    if active_leveling and settings.leveling == "off":
        raise PipelineError("leveling mode requests processing with its provider off")
    if settings.leveling != "auphonic" and (args.resume_auphonic_production is not None
            or args.auphonic_config is not None or args.auphonic_set or args.speech_leveler_strength is not None
            or settings.algorithms is not None):
        raise PipelineError("Auphonic options require the Auphonic leveling provider")
    if settings.diarization == "off" and (args.diarization_threshold is not None or args.max_speakers is not None):
        raise PipelineError("speaker tuning requires diarization")
    if settings.diarization != "vendor" and (args.speaker_library or args.speaker_roles):
        raise PipelineError("speaker library and speaker roles require vendor diarization")
    transcribers = {first, second} if active_second else {first}
    if "command" in transcribers:
        if settings.diarization == "vendor":
            raise PipelineError("vendor diarization requires ElevenLabs transcription")
        for name in ("audio_events", "clean_transcript", "speaker_library", "speaker_roles"):
            if getattr(args, name):
                raise PipelineError(f"command transcription does not support {name}")
    commands = [settings.transcription_command, settings.diarization_command]
    if active_second and second == "command":
        commands.append(settings.second_command or settings.transcription_command)
    cloud = "elevenlabs" in transcribers or active_leveling and settings.leveling == "auphonic"
    if settings.elevenlabs.api_base is not None:
        elevenlabs.validate_api_base(settings.elevenlabs.api_base, settings.elevenlabs.allow_insecure_local_http)
    if settings.auphonic.api_base is not None:
        base = auphonic._normalized_request_url(settings.auphonic.api_base)
        if "?" in base or "#" in base:
            raise PipelineError("invalid Auphonic API base")
    if settings.elevenlabs.allow_insecure_local_http:
        if (settings.elevenlabs.api_base is None or
                not settings.elevenlabs.api_base.startswith("http://")):
            raise PipelineError("the loopback fake requires an explicit HTTP API base")
        cloud = active_leveling and settings.leveling == "auphonic"
    brokered = any(command is not None and command.egress == "brokered" for command in commands)
    if settings.local_only and (cloud or brokered):
        raise PipelineError("local-only mode refuses cloud and brokered providers")
    if brokered and not settings.allow_brokered:
        raise PipelineError("brokered providers require allow_brokered")
    models = (args.model,) if settings.second_model is None else (args.model, settings.second_model)
    if any(type(model) is not str or not model.strip() or "\0" in model for model in models):
        raise PipelineError("model must be an exact nonempty identifier")


def resolved_settings(args: argparse.Namespace) -> tuple[dict[str, Any], float | None]:
    config_path = (
        _absolute(args.auphonic_config, args._explicit_config)
        if args.auphonic_config is not None
        else DEFAULT_AUPHONIC_CONFIG
    )
    overrides = list(args.auphonic_set)
    if args.speech_leveler_strength is not None:
        overrides.append(f"levelerstrength_speech={args.speech_leveler_strength}")
    configured_algorithms = getattr(args, "_config_algorithms", None)
    if configured_algorithms is None:
        algorithms = auphonic.resolve_algorithms(config_path, False, overrides)
    else:
        algorithms = {**auphonic.ALL_PROCESSING_OFF, **dict(configured_algorithms)}
        for override in overrides:
            key, value = auphonic._parse_override(override)
            algorithms[key] = value
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


def publish_directory(staging: Path, destination: Path, force: bool) -> None:
    """Serialize publication and keep the final rename on the destination filesystem."""
    parent = os.open(destination.parent, os.O_RDONLY)
    try:
        fcntl.flock(parent, fcntl.LOCK_EX)
        if staging.stat().st_dev == os.fstat(parent).st_dev:
            try:
                replace_directory(staging, destination, force)
                return
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}.publish-", dir=destination.parent) as temp:
            ready = Path(temp) / "package"
            shutil.copytree(staging, ready)
            replace_directory(ready, destination, force)
        shutil.rmtree(staging, ignore_errors=True)
    finally:
        os.close(parent)


def run_pipeline(
    args: argparse.Namespace, *, announce: bool = True,
    elevenlabs_api_key: str | None = None, auphonic_api_key: str | None = None,
    elevenlabs_client: elevenlabs.ElevenLabsClient | None = None,
    auphonic_client: auphonic.AuphonicClient | None = None,
) -> Path:
    """Run the pipeline and return the package directory. ``announce`` prints the directory (or a dry
    run's settings) to standard output, as the command does; a library caller turns it off rather than
    swapping ``sys.stdout``, which is process-wide and unsafe when recordings convert concurrently."""
    for key, client, provider in ((elevenlabs_api_key, elevenlabs_client, "ElevenLabs"),
                                  (auphonic_api_key, auphonic_client, "Auphonic")):
        if key is not None and (not key or client is not None):
            raise ValueError(f"{provider}: supply a nonempty key or a client, not both")
    progress_mode = "quiet" if args.quiet else args.progress
    reporter = ProgressReporter(progress_mode)
    staging: Path | None = None
    scratch: tempfile.TemporaryDirectory | None = None
    settings = args._settings
    explicit = args._explicit_config
    try:
        _preflight(args, settings)
        reporter.event("validation", "active", "Resolving paths and settings")
        source = _absolute(args.input_file, explicit)
        output_dir = (
            _absolute(args.output_dir, explicit)
            if args.output_dir is not None
            else default_output_dir(source)
        )
        validate_args(args, source, output_dir)
        algorithms, diarization_threshold = resolved_settings(args)
        if settings.diarization == "off":
            diarization_threshold = None
        second_provider = settings.second_provider or settings.transcription
        second_model = settings.second_model or args.model
        second_command = settings.second_command or settings.transcription_command
        command = settings.transcription_command
        first_identity = (settings.transcription, args.model, (command.argv, command.pass_env) if command else None)
        second_identity = (second_provider, second_model,
                           (second_command.argv, second_command.pass_env) if second_command else None)
        same_configuration = first_identity == second_identity
        repeat = (same_configuration and command is not None and second_command is not None
                  and command.deterministic and second_command.deterministic)

        def tool(name: str) -> str:
            return media.executable(name, getattr(settings, name), settings.executable_path, explicit=explicit)

        def measure(path: Path, environment: dict[str, str] | None = None) -> levels.Levels:
            if not explicit:
                return levels.measure(path)
            try:
                executable = tool("ffmpeg")
            except ProviderError as error:
                raise levels.LevelsError(str(error)) from None
            return levels.measure(path, executable=executable, env=environment or {})

        settings_summary = {
            "providers": {"transcription": settings.transcription, "diarization": settings.diarization,
                          "leveling": settings.leveling, "second_transcription": second_provider},
            "input": str(source),
            "output_directory": str(output_dir),
            "leveling": {"mode": args.leveling, "second_pass": args.second_pass, "uneven_threshold_db": args.uneven_threshold},
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
        if args.dry_run and args.second_pass == "on":
            settings_summary["leveling"]["would_transcribe_twice"] = not repeat
        reporter.event("validation", "completed", "Paths and settings are valid")
        if args.dry_run and "auto" in {args.leveling, args.second_pass}:
            try:
                planned = measure(source)  # local and free, so a dry run can say what would happen
            except levels.LevelsError as error:
                settings_summary["leveling"].update(levels=None, measurement_error=str(error))  # the real run would stop here
            else:
                settings_summary["leveling"].update(
                    levels=planned.as_dict(),
                    uneven=planned.uneven(args.uneven_threshold),
                    would_level=args.leveling == "on" or (args.leveling == "auto" and planned.uneven(args.uneven_threshold)),
                    would_transcribe_twice=not repeat and (args.second_pass == "on"
                        or (args.second_pass == "auto" and planned.uneven(args.uneven_threshold))),
                )
        if args.dry_run:
            if announce:
                print(json.dumps(settings_summary, indent=2, ensure_ascii=False))
            reporter.event("pipeline", "completed", "Dry run complete")
            return output_dir
        if output_dir.exists() and not args.force:
            raise PipelineError(
                f"output directory already exists: {output_dir}; use --force"
            )

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        stage_parent = _absolute(args.staging_dir, explicit) if args.staging_dir else output_dir.parent
        work_parent = _absolute(args.work_dir, explicit) if args.work_dir else None
        for parent in (stage_parent, work_parent):
            if parent is not None and parent.is_relative_to(output_dir):
                raise PipelineError("staging and work directories must be outside the output package")
        stage_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.transcribe-", dir=stage_parent))
        if work_parent is not None:
            work_parent.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix=".quillric-work-", dir=work_parent or staging)
        work = Path(scratch.name)
        child_env = work_environment(work, settings.executable_path)
        media_env = child_env if explicit else None
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

        measured: levels.Levels | None = None
        if args.leveling == "auto" or args.second_pass == "auto":
            reporter.event("levels", "active", "Measuring speech levels locally")
            measured = measure(source_copy, media_env)
            reporter.event("levels", "completed", f"Speech level spread {measured.spread_db} dB", spread_db=measured.spread_db)
        uneven = measured is not None and measured.uneven(args.uneven_threshold)
        leveled = args.leveling == "on" or (args.leveling == "auto" and uneven) or args.resume_auphonic_production is not None
        second_pass_wanted = args.second_pass == "on" or (args.second_pass == "auto" and uneven)
        production_id: str | None = None
        effective_algorithms: dict[str, Any] = {}
        effective_mp3_bitrate = args.mp3_bitrate
        stt_input = source_copy
        if explicit and (source.suffix.lower() in media.VIDEO_SUFFIXES or settings.ffprobe is not None
                         or settings.executable_path is not None):
            stt_input = media.video_audio(source_copy, work / "audio.flac", tool("ffprobe"),
                                          lambda: tool("ffmpeg"), media_env)
        if leveled and settings.leveling == "ffmpeg":
            reporter.event("ffmpeg_leveling", "active", "Leveling audio locally")
            leveler: LevelingProvider = media.FFmpegLeveling(tool("ffmpeg"), media_env)
            leveler.level(stt_input, adjusted_path, effective_mp3_bitrate)
            stt_input = adjusted_path
            reporter.event("ffmpeg_leveling", "completed", "Local leveling complete")
        if leveled and settings.leveling == "auphonic":
            auphonic_client = auphonic_client if auphonic_client is not None else auphonic.AuphonicClient(
                _vendor_key(auphonic_api_key, settings.auphonic, explicit=explicit,
                            standalone=auphonic.load_api_key, provider="Auphonic"),
                **_vendor_options(settings.auphonic, explicit=explicit))
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

            stt_input = adjusted_path

        def vendor_transcribe(model: str, progress=None):
            nonlocal elevenlabs_client
            if elevenlabs_client is None:
                elevenlabs_client = elevenlabs.ElevenLabsClient(
                    _vendor_key(elevenlabs_api_key, settings.elevenlabs, explicit=explicit,
                                standalone=elevenlabs.load_api_key, provider="ElevenLabs"),
                    **_vendor_options(settings.elevenlabs, explicit=explicit, eleven=True))
            optional = {} if settings.diarization == "vendor" else {"diarize": False}
            if progress is not None:
                optional["progress"] = progress
            return elevenlabs_client.transcribe(
                stt_input,
                args.max_speakers if settings.diarization == "vendor" else None,
                diarization_threshold if settings.diarization == "vendor" else None,
                model_id=model, language_code=args.language_code, tag_audio_events=args.audio_events,
                no_verbatim=args.clean_transcript, use_speaker_library=args.speaker_library,
                detect_speaker_roles=args.speaker_roles, **optional,
            )

        def local_transcribe(spec, model: str):
            if spec is None:
                raise PipelineError("command transcription requires a helper")
            transcriber: TranscriptionProvider = CommandTranscription(spec, work, child_env)
            return transcriber.transcribe(stt_input, model, args.language_code)

        diarizer: DiarizationProvider | None = None
        if settings.diarization_command is not None:
            diarizer = CommandDiarization(settings.diarization_command, work, child_env)

        def assignments(raw):
            if diarizer is not None:
                return diarizer.diarize(stt_input, raw, max_speakers=args.max_speakers,
                                        threshold=diarization_threshold)
            if settings.diarization == "off":
                return {str(index): None for index, word in enumerate(raw["words"])
                        if isinstance(word, dict) and word.get("type") in (None, "word")}
            return None

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
                    "Audio uploaded to ElevenLabs",
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

        if settings.transcription == "elevenlabs":
            try:
                transcript = vendor_transcribe(args.model, report_elevenlabs_upload)
            finally:
                transcription_stop.set()
                if transcription_heartbeat is not None:
                    transcription_heartbeat.join()
            if not elevenlabs_upload_completed:
                reporter.event("elevenlabs_upload", "completed", "Audio uploaded to ElevenLabs")
            stage = "elevenlabs_transcription"
        else:
            stage = "command_transcription"
            reporter.event(stage, "active", "Running configured transcription command")
            transcript = local_transcribe(command, args.model)
        first_labels = assignments(transcript)
        reporter.event(
            stage,
            "completed",
            "Transcription received",
            word_count=sum(
                1
                for word in transcript.get("words", [])
                if isinstance(word, dict) and word.get("type") == "word"
            ),
            speaker_count=len(transcript_speakers(project_diarization(transcript, first_labels))),
        )

        second_transcript: dict[str, Any] | None = None
        second_labels = None
        comparison = None
        if second_pass_wanted and repeat:
            comparison = {"schema": SECOND_PASS_SCHEMA, "independent": False,
                          "reason": "same_deterministic_provider_and_model"}
            reporter.event("second_pass", "completed", "Identical deterministic second pass skipped")
        elif second_pass_wanted:
            reporter.event("second_pass", "active", "Transcribing a second time to find uncertain words")
            second_transcript = (vendor_transcribe(second_model) if second_provider == "elevenlabs"
                                 else local_transcribe(second_command, second_model))
            second_labels = assignments(second_transcript)
            comparison = compare_passes(transcript, second_transcript)
            comparison.update(provider=second_provider, model=second_model,
                              basis="repeat" if same_configuration else "distinct_configuration")
            reporter.event("second_pass", "completed", "Second transcription received")

        reporter.event("artifacts", "active", "Writing JSON, sidecar, speaker table, and Markdown")
        write_package_files(
            version=VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source=source_copy,
            adjusted=adjusted_path if leveled else None,
            leveling={
                "mode": args.leveling,
                "provider": settings.leveling,
                "algorithm": media.FILTER if leveled and settings.leveling == "ffmpeg" else None,
                "applied": leveled,
                "uneven_threshold_db": args.uneven_threshold,
                "levels": measured.as_dict() if measured is not None else None,
            },
            second_pass=comparison,
            second_transcript=second_transcript,
            diarization=(first_labels, second_labels),
            providers={"transcription": settings.transcription, "diarization": settings.diarization},
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
        scratch.cleanup()
        scratch = None
        publish_directory(staging, output_dir, args.force)
        staging = None
        reporter.event("publish", "completed", "Package published")
        reporter.event(
            "pipeline", "completed", "Transcription package complete", output=str(output_dir)
        )
        if announce:
            print(output_dir, flush=True)
        return output_dir
    except BaseException as error:
        if scratch is not None:
            try:
                scratch.cleanup()
            except OSError:
                pass
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        message = "Interrupted" if isinstance(error, KeyboardInterrupt) else str(error)
        reporter.event("pipeline", "failed", message or type(error).__name__)
        raise


def parse_args(argv: list[str] | None = None, *, settings: Settings | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="transcribe",
        description=__doc__,
        epilog="Run 'transcribe speakers PACKAGE [ID=NAME ...]' to show or edit a package's speaker table, and 'transcribe duplicates PATH ...' to find the same audio in other formats.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input_file", type=Path)
    parser.add_argument("--config", type=Path, action="append", default=[],
                        help="explicit YAML settings file; repeat for ordered overlays")
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
    parser.add_argument("--staging-dir", type=Path, help="directory for in-progress packages")
    parser.add_argument("--work-dir", type=Path, help="directory for per-run scratch and caches")
    parser.add_argument("--force", action="store_true", help="replace an existing output directory")
    parser.add_argument("--dry-run", action="store_true", help="print resolved settings without API calls or writes")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    decision_group = parser.add_argument_group("Leveling and second pass")
    decision_group.add_argument(
        "--leveling",
        choices=["off", "auto", "on"],
        default="off",
        help="level with the configured provider before transcribing: never, only when uneven, or always",
    )
    decision_group.add_argument(
        "--second-pass",
        choices=["off", "auto", "on"],
        default="auto",
        help="transcribe a second time and list where the passes differ: never, only when the recording is uneven, or always",
    )
    decision_group.add_argument(
        "--uneven-threshold",
        type=float,
        default=levels.DEFAULT_UNEVEN_DB,
        metavar="DB",
        help="speech level spread, measured locally, at or above which a recording counts as uneven",
    )
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
    args = parser.parse_args(argv)
    if settings is not None and not isinstance(settings, Settings):
        raise ConfigurationError("settings must be a Quillric Settings object")
    if settings is not None and args.config:
        raise ConfigurationError("pass validated settings or --config, not both")
    args._explicit_config = bool(args.config) or settings is not None
    settings = load_settings(args.config) if args.config else settings
    args._settings = settings or Settings()
    if settings is None:
        return args
    configurable = {"leveling", "second_pass", "uneven_threshold", "mp3_bitrate", "model",
                    "language_code", "max_speakers", "diarization_threshold", "audio_events",
                    "clean_transcript", "speaker_library", "speaker_roles", "output_dir",
                    "staging_dir", "work_dir", "progress", "quiet", "force", "dry_run",
                    "auphonic_config", "auphonic_set", "speech_leveler_strength",
                    "resume_auphonic_production", "poll_interval", "wait_timeout"}
    # Let argparse identify explicitly supplied options (including aliases and --option=value).
    # Suppressing only these defaults avoids mistaking a built-in default for a CLI override.
    defaults = [(action, action.default) for action in parser._actions
                if action.dest in configurable]
    try:
        for action, _ in defaults:
            action.default = argparse.SUPPRESS
        explicit = vars(parser.parse_args(argv))
    finally:
        for action, default in defaults:
            action.default = default
    for destination, value in settings.overrides:
        if destination not in configurable:
            raise ConfigurationError("unknown settings override")
        setattr(args, destination, value)
    if "max_speakers" in explicit:
        args.diarization_threshold = None
    elif "diarization_threshold" in explicit:
        args.max_speakers = None
    if "quiet" in explicit:
        args.progress = "auto"
    elif "progress" in explicit:
        args.quiet = False
    for destination in configurable.intersection(explicit):
        setattr(args, destination, explicit[destination])
    if settings.algorithms is not None and args.auphonic_config is not None:
        raise ConfigurationError("YAML cleanup.algorithms and --auphonic-config are mutually exclusive")
    args._config_algorithms = settings.algorithms
    return args


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
        if arguments[:1] == ["leveling-helper"]:
            return media.leveling_helper(arguments[1:])
        if arguments[:1] == ["doctor"]:
            parser = argparse.ArgumentParser(prog="transcribe doctor")
            parser.add_argument("--config", type=Path, action="append", default=[])
            args = parser.parse_args(arguments[1:])
            settings = load_settings(args.config) if args.config else Settings()
            tools = [media.executable(name, getattr(settings, name), settings.executable_path,
                     explicit=bool(args.config)) for name in ("ffmpeg", "ffprobe")]
            print(json.dumps(media.doctor(*tools, {} if args.config else None), indent=2))
            return 0
        run_pipeline(parse_args(arguments))
    except (ConfigurationError, PipelineError, ProviderError, auphonic.AuphonicError,
            elevenlabs.TranscriptionError, levels.LevelsError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
