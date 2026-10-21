"""Explicit configuration loading and the transcription leaf's bounded schema.

Credentials, provider endpoints and filesystem actions are deliberately outside this schema.
Parents may reuse ``validate_config`` with Conric's subtree validation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conric import ConfigError, SchemaError, Value, load

from .auphonic import DEFAULT_ALGORITHMS


class ConfigurationError(RuntimeError):
    """A configuration error with value-free diagnostics."""


@dataclass(frozen=True)
class Settings:
    """Immutable validated overrides; omitted fields retain the command's defaults."""

    overrides: tuple[tuple[str, str | bool | int | float | None], ...] = ()
    algorithms: tuple[tuple[str, str | bool | int | float], ...] | None = None


def _mapping(value: Value, allowed: set[str], path: tuple[str, ...]) -> dict[str, Value]:
    if not isinstance(value, dict):
        raise SchemaError("expected_mapping", path=path)
    if set(value) - allowed:
        # Do not echo an unknown key: even a key can contain sensitive input.
        raise SchemaError("unknown_field", path=path)
    return value


def validate_config(value: Value) -> Settings:
    """Validate this leaf's subtree without reading credentials or invoking providers."""
    root = _mapping(value, {"transcription", "cleanup", "second_pass", "uneven_threshold_db"}, ())
    overrides: dict[str, Any] = {}
    algorithms = None

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

    if "uneven_threshold_db" in root:
        overrides["uneven_threshold"] = scalar(root["uneven_threshold_db"], float,
                                                ("uneven_threshold_db",), minimum=0)
    for section, provider in (("transcription", "elevenlabs"), ("cleanup", "auphonic")):
        if section not in root:
            continue
        allowed = ({"provider", "model", "language", "diarization", "audio_events",
                    "clean_transcript", "speaker_library", "speaker_roles"}
                   if section == "transcription" else {"provider", "mode", "algorithms", "bitrate_kbps"})
        document = _mapping(root[section], allowed, (section,))
        if "provider" in document and document["provider"] != provider:
            raise SchemaError("unsupported_provider", path=(section, "provider"))
        if section == "cleanup":
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
        for key, destination in (("model", "model"), ("language", "language_code")):
            if key in document:
                item = document[key]
                overrides[destination] = (None if key == "language" and item is None
                                          else scalar(item, str, (section, key)))
        for key in ("audio_events", "clean_transcript", "speaker_library", "speaker_roles"):
            if key in document:
                overrides[key] = scalar(document[key], bool, (section, key))
        if "diarization" in document:
            fields = _mapping(document["diarization"], {"max_speakers", "threshold"},
                              (section, "diarization"))
            if len(fields) > 1:
                raise SchemaError("conflicting_diarization", path=(section, "diarization"))
            if "max_speakers" in fields:
                overrides["max_speakers"] = scalar(fields["max_speakers"], int,
                    (section, "diarization", "max_speakers"), minimum=1, maximum=32)
            if "threshold" in fields:
                overrides["diarization_threshold"] = scalar(fields["threshold"], float,
                    (section, "diarization", "threshold"), minimum=0.1, maximum=0.4)
    if "second_pass" in root:
        fields = _mapping(root["second_pass"], {"mode"}, ("second_pass",))
        if "mode" in fields:
            overrides["second_pass"] = mode(fields["mode"], ("second_pass", "mode"))
    if overrides.get("speaker_roles") and overrides.get("max_speakers") not in {None, 2}:
        raise SchemaError("incompatible_speaker_roles", path=("transcription", "diarization"))
    return Settings(tuple(overrides.items()), algorithms)


def load_settings(paths: list[Path]) -> Settings:
    """Load only supplied files; Conric confines includes to the first file's parent."""
    try:
        return load(paths).validate(validate_config)
    except ConfigError as error:
        raise ConfigurationError(str(error)) from None
