"""Audio to diarized transcription packages.

Library use::

    import transcribe

    package = transcribe.transcribe_file("hearing.mp3")     # runs the paid pipeline
    package = transcribe.Package.load("hearing")            # an existing package
    package.source_sha256                                   # identity of the recording
    package.assign({"speaker_0": "Jane Smith", "speaker_3": "Jane Smith"})
    package.people()                                        # {"Jane Smith": ["speaker_0", "speaker_3"]}
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

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
    "new_speaker_table",
    "people",
    "render_markdown",
    "sha256_file",
    "transcribe_file",
]


def transcribe_file(source: str | Path, *options: str) -> Package:
    """Run the full pipeline on SOURCE and return the published package.

    OPTIONS are the command's own flags, for example ``"--output-dir", "/packages/hearing"`` or
    ``"--max-speakers", "4"``. Progress is suppressed; failures raise.
    """
    from . import cli

    args = cli.parse_args([str(source), "--quiet", *options])
    with contextlib.redirect_stdout(io.StringIO()):  # the command prints the package path
        directory = cli.run_pipeline(args)
    return Package.load(directory)
