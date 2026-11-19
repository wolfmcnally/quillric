"""Typed interfaces and bounded execution for configured local or brokered helpers."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict


STDOUT_LIMIT = 16 * 1024 * 1024
ERRORS = frozenset({"throttled", "refused", "unavailable", "invalid", "limit"})
WORK_ENV = frozenset({"HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PATH"})
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ProviderError(RuntimeError):
    """A stage failure with a bounded, safe-to-display diagnostic."""


class Word(TypedDict):
    text: str
    start: float | None
    end: float | None
    type: Literal["word", "spacing", "audio_event"]
    speaker_id: str | None
    logprob: float | None


class Transcript(TypedDict):
    words: list[Word]
    language_code: str
    audio_duration_secs: float


class TranscriptionProvider(Protocol):
    def transcribe(self, audio: Path, model: str, language: str | None) -> Transcript: ...


class DiarizationProvider(Protocol):
    def diarize(self, audio: Path, transcript: dict[str, Any], *,
                max_speakers: int | None, threshold: float | None) -> dict[str, str | None]: ...


class LevelingProvider(Protocol):
    def level(self, audio: Path, output: Path, bitrate: int) -> None: ...


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    egress: Literal["local", "brokered"] = "brokered"
    timeout_seconds: float = 900
    max_concurrency: int = 1
    pass_env: tuple[str, ...] = ()
    deterministic: bool = True

    def validate(self) -> None:
        if (type(self.argv) is not tuple or not self.argv
                or any(type(arg) is not str or "\0" in arg for arg in self.argv)
                or not Path(self.argv[0]).is_absolute()):
            raise ProviderError("command requires an absolute executable and valid argv")
        if (self.egress not in ("local", "brokered") or type(self.deterministic) is not bool
                or type(self.timeout_seconds) not in (int, float)
                or not 0.001 <= self.timeout_seconds <= 86400
                or type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 1024):
            raise ProviderError("command egress, determinism or limits are invalid")
        if (type(self.pass_env) is not tuple or any(type(name) is not str
                or not ENV_NAME.fullmatch(name) or name in WORK_ENV for name in self.pass_env)
                or len(set(self.pass_env)) != len(self.pass_env)):
            raise ProviderError("command environment allowlist is invalid")


_condition = threading.Condition()
_running: dict[tuple[str, ...], tuple[int, int]] = {}


@contextmanager
def _slot(command: Command, deadline: float):
    with _condition:
        while True:
            count, limit = _running.get(command.argv, (0, command.max_concurrency))
            if limit != command.max_concurrency:
                raise ProviderError("command: conflicting concurrency limit for active executable")
            if count < limit:
                _running[command.argv] = count + 1, limit
                break
            if not _condition.wait(max(0, deadline - time.monotonic())):
                raise ProviderError("command: timeout")
    try:
        yield
    finally:
        with _condition:
            count, limit = _running[command.argv]
            if count == 1:
                del _running[command.argv]
            else:
                _running[command.argv] = count - 1, limit
            _condition.notify_all()


def work_environment(work: Path, search_path: str | None) -> dict[str, str]:
    env = {"HOME": str(work), "TMPDIR": str(work), "XDG_CACHE_HOME": str(work / "cache"),
           "XDG_CONFIG_HOME": str(work / "config"), "XDG_DATA_HOME": str(work / "data")}
    if search_path is not None:
        env["PATH"] = search_path
    return env


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ProviderError("command: timeout")
    return left


def _exchange(command: Command, payload: bytes, work: Path, env: dict[str, str]) -> bytes:
    deadline = time.monotonic() + command.timeout_seconds
    with _slot(command, deadline):
        _remaining(deadline)
        try:
            child = subprocess.Popen(command.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, cwd=work,
                                     start_new_session=True)
        except (OSError, ValueError):
            raise ProviderError("command: unavailable") from None
        try:
            assert child.stdin is not None and child.stdout is not None
            with child.stdin, child.stdout, selectors.DefaultSelector() as io:
                for stream in (child.stdin, child.stdout):
                    os.set_blocking(stream.fileno(), False)
                io.register(child.stdin, selectors.EVENT_WRITE)
                io.register(child.stdout, selectors.EVENT_READ)
                pending = memoryview(payload)
                output = bytearray()
                while io.get_map():
                    ready = io.select(_remaining(deadline))
                    for key, event in ready:
                        if event == selectors.EVENT_WRITE:
                            try:
                                pending = pending[os.write(key.fd, pending[:65536]):]
                            except BrokenPipeError:
                                raise ProviderError("command: incomplete request") from None
                            if not pending:
                                io.unregister(key.fd)
                                child.stdin.close()
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                io.unregister(key.fd)
                            elif len(output) + len(chunk) > STDOUT_LIMIT:
                                raise ProviderError("command: output limit")
                            else:
                                output.extend(chunk)
                if child.wait(timeout=_remaining(deadline)) != 0:
                    raise ProviderError("command: unsuccessful exit")
                return bytes(output)
        except subprocess.TimeoutExpired:
            raise ProviderError("command: timeout") from None
        except OSError:
            raise ProviderError("command: I/O failure") from None
        finally:
            child.poll()
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin can refuse killpg for a group containing only zombies.
                if child.returncode is None:
                    child.kill()
            finally:
                child.wait()


def strict_json(data: bytes) -> Any:
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject(_constant):
        raise ValueError("non-finite JSON constant")

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject)


def exact(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and value.keys() == keys


def _number(value: Any) -> bool:
    return type(value) is float and math.isfinite(value) or type(value) is int and abs(value) < 2**53


def _label(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(value)
                             and value.isprintable() and len(value) <= 256
                             and not value.startswith("second:"))


def _request(command: Command, stage: str, body: dict, work: Path, env: dict[str, str]) -> Any:
    request = json.dumps({"schema": f"quillric-{stage}-request/1", "body": body},
                         allow_nan=False).encode("utf-8")
    inherited = {name: value for name in command.pass_env if (value := os.environ.get(name)) is not None}
    response = _exchange(command, request, work, {**inherited, **env})
    try:
        document = strict_json(response)
        if not isinstance(document, dict) or document.get("schema") != f"quillric-{stage}-response/1":
            raise ValueError
        if exact(document, {"schema", "status", "body"}) and document["status"] == "ok":
            return document["body"]
        if (exact(document, {"schema", "status", "category"}) and document["status"] == "error"
                and isinstance(document["category"], str) and document["category"] in ERRORS):
            raise ProviderError(f"{stage}: {document['category']}")
        raise ValueError
    except (ValueError, RecursionError):
        raise ProviderError(f"{stage}: invalid response") from None


@dataclass(frozen=True)
class CommandTranscription:
    command: Command
    work: Path
    env: dict[str, str]

    def transcribe(self, audio: Path, model: str, language: str | None) -> Transcript:
        body = _request(self.command, "transcription", {
            "audio_path": str(audio), "model": model, "language_code": language,
        }, self.work, self.env)
        if (not exact(body, {"words", "language_code", "audio_duration_secs"})
                or not isinstance(body["words"], list) or not isinstance(body["language_code"], str)
                or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", body["language_code"])
                or not _number(body["audio_duration_secs"]) or body["audio_duration_secs"] < 0):
            raise ProviderError("transcription: invalid transcript")
        previous = 0
        for word in body["words"]:
            if not exact(word, {"text", "type", "start", "end", "speaker_id", "logprob"}):
                raise ProviderError("transcription: invalid word")
            text, kind, start, end, label, logprob = (word[key] for key in
                ("text", "type", "start", "end", "speaker_id", "logprob"))
            if (not isinstance(text, str) or not text or any(0xD800 <= ord(c) <= 0xDFFF for c in text)
                    or kind not in ("word", "spacing") or not _label(label)
                    or (logprob is not None and (not _number(logprob) or logprob > 0))):
                raise ProviderError("transcription: invalid word")
            if kind == "spacing" and start is None and end is None and text.isspace():
                continue
            if (not _number(start) or not _number(end) or
                    not previous <= start <= end <= body["audio_duration_secs"] or
                    (kind == "word" and not text.strip()) or (kind == "spacing" and not text.isspace())):
                raise ProviderError("transcription: invalid word timing")
            previous = start
        return body


def word_ordinals(transcript: dict[str, Any]) -> dict[str, dict]:
    words = {}
    for index, word in enumerate(transcript["words"]):
        if not isinstance(word, dict) or word.get("type") not in (None, "word"):
            continue
        start, end = word.get("start"), word.get("end")
        if not _number(start) or not _number(end) or not 0 <= start <= end:
            raise ProviderError("diarization: expected timed words")
        words[str(index)] = {"ordinal": index, "start": start, "end": end}
    return words


@dataclass(frozen=True)
class CommandDiarization:
    command: Command
    work: Path
    env: dict[str, str]

    def diarize(self, audio: Path, transcript: dict[str, Any], *,
                max_speakers: int | None, threshold: float | None) -> dict[str, str | None]:
        words = word_ordinals(transcript)
        body = _request(self.command, "diarization", {
            "audio_path": str(audio), "words": list(words.values()),
            "max_speakers": max_speakers, "threshold": threshold,
        }, self.work, self.env)
        if (not exact(body, {"assignments"}) or not isinstance(body["assignments"], dict)
                or body["assignments"].keys() != words.keys()
                or not all(_label(value) for value in body["assignments"].values())):
            raise ProviderError("diarization: invalid assignments")
        return body["assignments"]
