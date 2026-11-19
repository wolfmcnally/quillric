"""Explicit-mode TLS and media type defaults."""

import ssl
from pathlib import Path


_AUDIO_TYPES = {
    ".aac": "audio/aac", ".flac": "audio/flac", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
    ".mp4": "video/mp4", ".oga": "audio/ogg", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".webm": "video/webm",
}


def media_type(name: str) -> str:
    return _AUDIO_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def tls_context(ca_file: Path | None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_file is None:
        context.load_default_certs()
    else:
        context.load_verify_locations(cafile=str(ca_file))
    return context
