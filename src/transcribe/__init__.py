"""Quillric: audio to diarized transcription packages (import name: transcribe).

Library use::

    import transcribe

    package = transcribe.transcribe_file("hearing.mp3")     # runs the paid pipeline
    package = transcribe.Package.load("hearing")            # an existing package
    package.source_sha256                                   # identity of the recording
    package.assign({"speaker_0": "Jane Smith", "speaker_3": "Jane Smith"})
    package.people()                                        # {"Jane Smith": ["speaker_0", "speaker_3"]}
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .auphonic import AuphonicClient
    from .elevenlabs import ElevenLabsClient

from . import levels, merge, second_pass
from .package import (
    PACKAGE_SCHEMA,
    SPEAKERS_SCHEMA,
    Package,
    PackageError,
    assign_names,
    new_speaker_table,
    people,
    render_markdown,
    sha256_file,
)

__all__ = [
    "PACKAGE_SCHEMA",
    "SPEAKERS_SCHEMA",
    "Package",
    "PackageError",
    "assign_names",
    "levels",
    "merge",
    "new_speaker_table",
    "people",
    "render_markdown",
    "second_pass",
    "sha256_file",
    "transcribe_file",
]


def transcribe_file(
    source: str | Path, *options: str,
    elevenlabs_api_key: str | None = None, auphonic_api_key: str | None = None,
    elevenlabs_client: ElevenLabsClient | None = None,
    auphonic_client: AuphonicClient | None = None,
) -> Package:
    """Run the full pipeline on SOURCE and return the published package.

    OPTIONS are the command's own flags, for example ``"--output-dir", "/packages/hearing"`` or
    ``"--max-speakers", "4"``. Progress is suppressed; failures raise.
    Per-provider keyword keys or clients override environment/.env lookup without
    changing process state. Supplying both a key and client for one provider is refused.
    """
    from . import cli

    args = cli.parse_args([str(source), "--quiet", *options])
    directory = cli.run_pipeline(
        args, announce=False, elevenlabs_api_key=elevenlabs_api_key,
        auphonic_api_key=auphonic_api_key, elevenlabs_client=elevenlabs_client,
        auphonic_client=auphonic_client)
    return Package.load(directory)
