# Get Started

Quillric needs Python 3.11 or later on Linux or macOS, Git to fetch its pinned Conric dependency, and `ffmpeg` on the path. Windows is not supported. Install a release wheel in a fresh environment:

```sh
python3 -m venv .venv
```

```sh
.venv/bin/python -m pip install https://github.com/wolfmcnally/quillric/releases/download/v0.1.0/quillric-0.1.0-py3-none-any.whl
```

Then preview the settings for a recording without calling any provider or loading credentials:

```sh
.venv/bin/transcribe recording.mp3 --dry-run
```

Without `--dry-run`, a transcription sends the recording to the configured provider and creates the package beside the input. Read the README's privacy section before transcribing sensitive material.
