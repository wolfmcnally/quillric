"""Local media preparation and the ffmpeg leveling adapter."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .providers import ProviderError, exact, strict_json, work_environment


VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mpeg", ".mpg", ".ts"})
FILTER = "dynaudnorm"
LOCAL_INPUT = ("-protocol_whitelist", "file,pipe")


def executable(name: str, path: Path | None, search: str | None, *, explicit: bool) -> str:
    if path is not None:
        found = str(path)
    elif search is not None or not explicit:
        found = shutil.which(name, path=search)
    else:
        found = None
    if not found or not Path(found).is_absolute() or not os.access(found, os.X_OK):
        raise ProviderError(f"{name}: configure an absolute executable or search path")
    return found


def _run(argv: list[str], env: dict[str, str] | None, *, read: bool = False) -> bytes:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if read else subprocess.DEVNULL,
                                check=True, timeout=7200, env=env)
    except (OSError, subprocess.SubprocessError):
        raise ProviderError("local media processing failed") from None
    return result.stdout


def video_audio(source: Path, output: Path, probe: str, ffmpeg: Callable[[], str],
                env: dict[str, str] | None) -> Path:
    raw = _run([probe, "-v", "error", *LOCAL_INPUT, "-show_entries",
                "stream=codec_type,channels:stream_disposition=attached_pic", "-of", "json",
                str(source)], env, read=True)
    try:
        streams = json.loads(raw)["streams"]
        has_video = any(s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
                        for s in streams)
        audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    except (ValueError, TypeError, KeyError):
        raise ProviderError("ffprobe: invalid stream information") from None
    if audio is None:
        raise ProviderError("input has no audio stream")
    if not has_video:
        return source
    channels = audio.get("channels")
    # FLAC supports up to eight channels. Preserve supported layouts and downmix wider tracks.
    layout = ["-ac", "2"] if type(channels) is int and channels > 8 else []
    _run([ffmpeg(), "-v", "error", "-nostdin", "-n", *LOCAL_INPUT, "-i", str(source),
          "-map", "0:a:0", *layout, "-c:a", "flac", str(output)], env)
    return output


@dataclass(frozen=True)
class FFmpegLeveling:
    executable: str
    env: dict[str, str] | None

    def level(self, audio: Path, output: Path, bitrate: int) -> None:
        _run([self.executable, "-v", "error", "-nostdin", "-n", *LOCAL_INPUT, "-i", str(audio),
              "-map", "0:a:0", "-af", FILTER, "-c:a", "libmp3lame",
              "-b:a", f"{bitrate}k", "-f", "mp3", str(output)], self.env)
        if not output.is_file() or output.stat().st_size == 0:
            raise ProviderError("ffmpeg leveling produced no audio")


def doctor(ffmpeg: str, ffprobe: str, env: dict[str, str] | None) -> dict[str, str]:
    versions = {}
    for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        output = _run([path, "-version"], env, read=True).decode("utf-8", errors="replace")
        if not output.startswith(f"{name} version "):
            raise ProviderError(f"{name}: wrong executable")
        versions[name] = output
        versions[name + "_license"] = _run([path, "-L"], env, read=True).decode("utf-8", errors="replace")
    for argument, feature in (("-filters", FILTER), ("-encoders", "libmp3lame")):
        if feature.encode() not in _run([ffmpeg, "-hide_banner", argument], env, read=True):
            raise ProviderError(f"ffmpeg: required feature {feature} is absent")
    return versions


def leveling_helper(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="transcribe leveling-helper")
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args(argv)
    response = {"schema": "quillric-leveling-response/1"}
    try:
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise ValueError
        document = strict_json(payload)
        if not exact(document, {"schema", "body"}) or document["schema"] != "quillric-leveling-request/1":
            raise ValueError
        body = document["body"]
        if not exact(body, {"audio_path", "output_path", "bitrate_kbps"}):
            raise ValueError
        audio, output, bitrate = (body[key] for key in ("audio_path", "output_path", "bitrate_kbps"))
        if (not isinstance(audio, str) or not isinstance(output, str) or "\0" in audio + output
                or not Path(audio).is_absolute() or not Path(output).is_absolute()
                or type(bitrate) is not int or not 32 <= bitrate <= 320):
            raise ValueError
        tool = executable("ffmpeg", args.ffmpeg, None, explicit=True)
        FFmpegLeveling(tool, work_environment(Path.cwd(), None)).level(Path(audio), Path(output), bitrate)
    except (ValueError, RecursionError):
        response.update(status="error", category="invalid")
    except (ProviderError, OSError):
        response.update(status="error", category="unavailable")
    else:
        response.update(status="ok", body={"algorithm": FILTER, "bitrate_kbps": bitrate})
    print(json.dumps(response))
    return 0
