"""Show a package's speaker table, or assign names to its speaker identities."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .package import Package, PackageError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="transcribe speakers", description=__doc__)
    parser.add_argument("package", type=Path, help="transcription package directory")
    parser.add_argument(
        "assignments",
        nargs="*",
        metavar="ID=NAME",
        help="give NAME to speaker identity ID; the same NAME may be given to several identities",
    )
    parser.add_argument("--note", action="append", default=[], metavar="ID=TEXT", help="set a note")
    parser.add_argument("--clear", action="append", default=[], metavar="ID", help="remove a name")
    return parser.parse_args(argv)


def _pairs(values: list[str], what: str) -> dict[str, str | None]:
    pairs: dict[str, str | None] = {}
    for value in values:
        identity, separator, text = value.partition("=")
        if not separator or not identity.strip():
            raise PackageError(f"{what} must look like ID=TEXT: {value!r}")
        pairs[identity.strip()] = text
    return pairs


def print_table(package: Package) -> None:
    for row in package.speakers["speakers"]:
        print(f"{row['id']}\t{row.get('name') or ''}\t{row['turns']} turns\t{row['words']} words")
    for name, identities in package.people().items():
        if len(identities) > 1:
            print(f"# {name}: {', '.join(identities)}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        package = Package.load(args.package)
        names = _pairs(args.assignments, "an assignment")
        names.update({identity: None for identity in args.clear})
        notes = _pairs(args.note, "a note")
        if names or notes:
            package.assign(names, notes)
        print_table(package)
    except (PackageError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
