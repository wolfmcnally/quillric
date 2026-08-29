#!/usr/bin/env python3
"""Re-render transcript Markdown from saved ElevenLabs JSON without an API call."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from elevenlabs_transcribe import render_markdown


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_files", nargs="+", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        for json_path in args.json_files:
            document = json.loads(json_path.read_text(encoding="utf-8"))
            markdown_path = json_path.with_suffix(".md")
            markdown_path.write_text(
                render_markdown(Path(json_path.stem), document),
                encoding="utf-8",
            )
            print(f"Rendered: {markdown_path}")
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
