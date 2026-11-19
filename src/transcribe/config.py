"""Explicit configuration loading and the reusable transcription leaf schema."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from conric import ConfigError, SchemaError, SecretRef, Value, load

from .auphonic import DEFAULT_ALGORITHMS
from .providers import Command, ENV_NAME, WORK_ENV


class ConfigurationError(RuntimeError):
    """A configuration error with value-free diagnostics."""


@dataclass(frozen=True)
class Vendor:
    api_key: SecretRef | None = None
    api_base: str | None = None
    ca_file: Path | None = None
    allow_insecure_local_http: bool = False


@dataclass(frozen=True)
class Settings:
    """Immutable validated overrides; omitted fields retain the command's defaults."""

    overrides: tuple[tuple[str, Any], ...] = ()
    algorithms: tuple[tuple[str, str | bool | int | float], ...] | None = None
    transcription: Literal["elevenlabs", "command"] = "elevenlabs"
    diarization: Literal["vendor", "command", "off"] = "vendor"
    leveling: Literal["auphonic", "ffmpeg", "off"] = "auphonic"
    transcription_command: Command | None = None
    diarization_command: Command | None = None
    second_provider: Literal["elevenlabs", "command"] | None = None
    second_model: str | None = None
    second_command: Command | None = None
    elevenlabs: Vendor = Vendor()
    auphonic: Vendor = Vendor()
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    executable_path: str | None = None
    local_only: bool = False
    allow_brokered: bool = False
    duplicates: tuple[tuple[str, Any], ...] = ()
    speakers: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _mapping(value: Value, allowed: set[str], path: tuple[str, ...]) -> dict[str, Value]:
    if not isinstance(value, dict):
        raise SchemaError("expected_mapping", path=path)
    if set(value) - allowed:
        # Do not echo an unknown key: even a key can contain sensitive input.
        raise SchemaError("unknown_field", path=path)
    return value


def validate_config(value: Value) -> Settings:
    """Validate this leaf's subtree without reading credentials or invoking providers."""
    root = _mapping(value, {"transcription", "diarization", "leveling", "cleanup",
                            "second_pass", "uneven_threshold_db", "paths", "run",
                            "local_only", "allow_brokered", "duplicates", "speakers"}, ())
    overrides: dict[str, Any] = {}
    algorithms = None
    providers: dict[str, Any] = {}

    def scalar(item: Value, kind: type, path: tuple[str, ...], *, minimum=None, maximum=None):
        valid = type(item) is kind
        if kind is float:
            valid = type(item) is int or type(item) is float and math.isfinite(item)
        if not valid:
            raise SchemaError("invalid_type", path=path)
        if kind is str and not item.strip():
            raise SchemaError("expected_nonempty_string", path=path)
        if (minimum is not None and item < minimum) or (maximum is not None and item > maximum):
            raise SchemaError("out_of_range", path=path)
        return item

    def mode(item: Value, path: tuple[str, ...]):
        if scalar(item, str, path) not in {"off", "auto", "on"}:
            raise SchemaError("unknown_mode", path=path)
        return item

    def choice(item, choices, path):
        if scalar(item, str, path) not in choices:
            raise SchemaError("unknown_choice", path=path)
        return item

    def absolute(item, path):
        text = scalar(item, str, path)
        if not Path(text).is_absolute() or "\0" in text:
            raise SchemaError("expected_absolute_path", path=path)
        return Path(text)

    def command(item, path):
        fields = _mapping(item, {"argv", "egress", "timeout_seconds", "max_concurrency",
                                 "pass_env", "deterministic"}, path)
        argv = fields.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(type(arg) is not str or "\0" in arg for arg in argv)):
            raise SchemaError("invalid_argv", path=path + ("argv",))
        absolute(argv[0], path + ("argv",))
        names = fields.get("pass_env", [])
        if (not isinstance(names, list) or any(not isinstance(name, str)
                or not ENV_NAME.fullmatch(name) or name in WORK_ENV for name in names)
                or len(set(names)) != len(names)):
            raise SchemaError("invalid_environment", path=path + ("pass_env",))
        return Command(tuple(argv), choice(fields.get("egress", "brokered"), {"local", "brokered"},
                                          path + ("egress",)),
                       scalar(fields.get("timeout_seconds", 900), float, path + ("timeout_seconds",),
                              minimum=0.001, maximum=86400),
                       scalar(fields.get("max_concurrency", 1), int, path + ("max_concurrency",),
                              minimum=1, maximum=1024),
                       tuple(names), scalar(fields.get("deterministic", True), bool,
                                            path + ("deterministic",)))

    def vendor(document, section):
        path = (section,)
        reference = document.get("api_key")
        if "api_key" in document and not isinstance(reference, SecretRef):
            raise SchemaError("expected_secret_ref", path=path + ("api_key",))
        return Vendor(
            reference,
            scalar(document["api_base"], str, path + ("api_base",)) if "api_base" in document else None,
            absolute(document["ca_file"], path + ("ca_file",)) if "ca_file" in document else None,
            scalar(document.get("allow_insecure_local_http", False), bool,
                   path + ("allow_insecure_local_http",)),
        )

    if "uneven_threshold_db" in root:
        overrides["uneven_threshold"] = scalar(root["uneven_threshold_db"], float,
                                                ("uneven_threshold_db",), minimum=0)
    if "cleanup" in root and "leveling" in root:
        raise SchemaError("conflicting_leveling_sections", path=("leveling",))
    level_section = "cleanup" if "cleanup" in root else "leveling"
    for section in ("transcription", level_section):
        if section not in root:
            continue
        vendor_keys = {"api_key", "api_base", "ca_file"}
        allowed = ({"provider", "model", "language", "diarization", "audio_events", "command",
                    "clean_transcript", "no_verbatim", "speaker_library", "speaker_roles",
                    "allow_insecure_local_http"}
                   if section == "transcription" else {"provider", "mode", "algorithms", "bitrate_kbps",
                                                       "poll_interval", "wait_timeout", "resume_production",
                                                       "algorithm_file", "algorithm_overrides", "speech_leveler_strength"})
        document = _mapping(root[section], allowed | vendor_keys, (section,))
        if section != "transcription":
            provider = choice(document.get("provider", "auphonic"), {"auphonic", "ffmpeg", "off"},
                              (section, "provider"))
            providers["leveling"] = provider
            auphonic_fields = vendor_keys | {"algorithms", "poll_interval", "wait_timeout", "resume_production",
                                             "algorithm_file", "algorithm_overrides", "speech_leveler_strength"}
            if provider != "auphonic" and document.keys() & auphonic_fields:
                raise SchemaError("incompatible_provider_options", path=(section,))
            providers["auphonic"] = vendor(document, section)
            for key in ("poll_interval", "wait_timeout"):
                if key in document:
                    overrides[key] = scalar(document[key], float, (section, key), minimum=0.001)
            if "resume_production" in document:
                overrides["resume_auphonic_production"] = scalar(document["resume_production"], str,
                                                               (section, "resume_production"))
            if "algorithm_file" in document:
                overrides["auphonic_config"] = absolute(document["algorithm_file"], (section, "algorithm_file"))
            if "algorithm_overrides" in document:
                items = document["algorithm_overrides"]
                if not isinstance(items, list):
                    raise SchemaError("expected_list", path=(section, "algorithm_overrides"))
                overrides["auphonic_set"] = tuple(scalar(item, str, (section, "algorithm_overrides"))
                                                  for item in items)
            if "speech_leveler_strength" in document:
                overrides["speech_leveler_strength"] = scalar(document["speech_leveler_strength"], int,
                                                             (section, "speech_leveler_strength"))
            if "mode" in document:
                overrides["leveling"] = mode(document["mode"], (section, "mode"))
            if "bitrate_kbps" in document:
                overrides["mp3_bitrate"] = scalar(document["bitrate_kbps"], int,
                                                  (section, "bitrate_kbps"), minimum=32, maximum=320)
            if "algorithms" in document:
                fields = _mapping(document["algorithms"], set(DEFAULT_ALGORITHMS), (section, "algorithms"))
                algorithms = tuple((key, scalar(item, type(DEFAULT_ALGORITHMS[key]),
                                                (section, "algorithms", key)))
                                   for key, item in fields.items())
            continue
        provider = choice(document.get("provider", "elevenlabs"), {"elevenlabs", "command"},
                          (section, "provider"))
        providers["transcription"] = provider
        if (provider == "command") != ("command" in document):
            raise SchemaError("incompatible_command", path=(section, "command"))
        if "command" in document:
            providers["transcription_command"] = command(document["command"], (section, "command"))
        if provider == "command" and document.keys() & (vendor_keys | {"allow_insecure_local_http"}):
            raise SchemaError("incompatible_provider_options", path=(section,))
        providers["elevenlabs"] = vendor(document, section)
        if "clean_transcript" in document and "no_verbatim" in document:
            raise SchemaError("conflicting_verbatim_options", path=(section,))
        if "no_verbatim" in document:
            overrides["clean_transcript"] = scalar(document["no_verbatim"], bool, (section, "no_verbatim"))
        for key, destination in (("model", "model"), ("language", "language_code")):
            if key in document:
                item = document[key]
                overrides[destination] = (None if key == "language" and item is None
                                          else scalar(item, str, (section, key)))
        for key in ("audio_events", "clean_transcript", "speaker_library", "speaker_roles"):
            if key in document:
                overrides[key] = scalar(document[key], bool, (section, key))
    nested = root.get("transcription", {})
    if "diarization" in root and "diarization" in nested:
        raise SchemaError("conflicting_diarization_sections", path=("diarization",))
    path = ("diarization",) if "diarization" in root else ("transcription", "diarization")
    selectors = {"provider", "command"} if len(path) == 1 else set()
    fields = _mapping(root.get("diarization", nested.get("diarization", {})),
                      {"max_speakers", "threshold"} | selectors, path)
    diarization = choice(fields.get("provider", "vendor"), {"vendor", "command", "off"}, path + ("provider",))
    providers["diarization"] = diarization
    if (diarization == "command") != ("command" in fields):
        raise SchemaError("incompatible_command", path=path + ("command",))
    if "command" in fields:
        providers["diarization_command"] = command(fields["command"], path + ("command",))
    if "max_speakers" in fields and "threshold" in fields:
        raise SchemaError("conflicting_diarization", path=path)
    if "max_speakers" in fields:
        overrides["max_speakers"] = scalar(fields["max_speakers"], int, path + ("max_speakers",),
                                           minimum=1, maximum=32)
    if "threshold" in fields:
        overrides["diarization_threshold"] = scalar(fields["threshold"], float, path + ("threshold",),
                                                    minimum=0.1, maximum=0.4)

    fields = _mapping(root.get("second_pass", {}), {"mode", "provider", "model", "command"}, ("second_pass",))
    if "mode" in fields:
        overrides["second_pass"] = mode(fields["mode"], ("second_pass", "mode"))
    if "provider" in fields:
        providers["second_provider"] = choice(fields["provider"], {"elevenlabs", "command"},
                                             ("second_pass", "provider"))
    if "model" in fields:
        providers["second_model"] = scalar(fields["model"], str, ("second_pass", "model"))
    if "command" in fields:
        providers["second_command"] = command(fields["command"], ("second_pass", "command"))
    first = providers.get("transcription", "elevenlabs")
    second = providers.get("second_provider", first)
    if first == "command" and second == "elevenlabs":
        raise SchemaError("elevenlabs_second_pass_requires_elevenlabs_first_pass", path=("second_pass", "provider"))
    if (second != "command" and "second_command" in providers
            or second == "command" and first != "command" and "second_command" not in providers):
        raise SchemaError("incompatible_command", path=("second_pass", "command"))
    if diarization == "vendor" and (first == "command" or
            second == "command" and overrides.get("second_pass", "auto") != "off"):
        raise SchemaError("vendor_diarization_requires_elevenlabs", path=("diarization", "provider"))

    fields = _mapping(root.get("paths", {}), {"output_dir", "staging_dir", "work_dir",
                      "ffmpeg", "ffprobe", "executable_path"}, ("paths",))
    for key in ("output_dir", "staging_dir", "work_dir"):
        if key in fields:
            overrides[key] = absolute(fields[key], ("paths", key))
    for key in ("ffmpeg", "ffprobe"):
        if key in fields:
            providers[key] = absolute(fields[key], ("paths", key))
    if "executable_path" in fields:
        raw = scalar(fields["executable_path"], str, ("paths", "executable_path"))
        for entry in raw.split(":"):
            absolute(entry, ("paths", "executable_path"))
        providers["executable_path"] = raw

    fields = _mapping(root.get("run", {}), {"force", "dry_run", "quiet", "progress"}, ("run",))
    for key in ("force", "dry_run", "quiet"):
        if key in fields:
            overrides[key] = scalar(fields[key], bool, ("run", key))
    if "progress" in fields:
        overrides["progress"] = choice(fields["progress"], {"auto", "tty", "plain", "json"}, ("run", "progress"))
    for key in ("local_only", "allow_brokered"):
        if key in root:
            providers[key] = scalar(root[key], bool, (key,))

    fields = _mapping(root.get("duplicates", {}), {"cache", "workers", "json"}, ("duplicates",))
    duplicates = {}
    if "cache" in fields:
        duplicates["cache"] = absolute(fields["cache"], ("duplicates", "cache"))
    if "workers" in fields:
        duplicates["workers"] = scalar(fields["workers"], int, ("duplicates", "workers"), minimum=1)
    if "json" in fields:
        duplicates["json"] = scalar(fields["json"], bool, ("duplicates", "json"))
    if overrides.get("speaker_roles") and overrides.get("max_speakers") not in {None, 2}:
        raise SchemaError("incompatible_speaker_roles", path=("transcription", "diarization"))
    speaker_fields = _mapping(root.get("speakers", {}), {"assignments", "note", "clear"}, ("speakers",))
    speakers = []
    for key, items in speaker_fields.items():
        if not isinstance(items, list):
            raise SchemaError("expected_list", path=("speakers", key))
        speakers.append((key, tuple(scalar(item, str, ("speakers", key)) for item in items)))
    return Settings(tuple(overrides.items()), algorithms, duplicates=tuple(duplicates.items()),
                    speakers=tuple(speakers), **providers)


def load_settings(paths: list[Path]) -> Settings:
    """Load only supplied files; Conric confines includes to the first file's parent."""
    try:
        return load(paths).validate(validate_config)
    except ConfigError as error:
        raise ConfigurationError(str(error)) from None
