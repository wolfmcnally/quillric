# Phase 1: Dependency-ready packages with a speaker table

Status: implemented 2026-09-18; acceptance met (69 tests, offline rebuild, one live fixture run, clean-environment import). Committed during private development; retained as historical implementation evidence.

## Outcome

`transcribe` can be used as a library by a document-conversion tool that builds Markdown mirrors from audio, and every transcription package carries what such a consumer needs: the identity of the source recording, a machine-readable description of the package, and a speaker table in which names can be assigned later.

Full Markdown cleanup of the transcript is out of scope; it belongs to the consumer. The review workbench proposed in `briefs/human-in-the-loop-speaker-resolution.md` is out of scope; the speaker table is the smallest piece of it, and stays compatible with it.

## Work

1. **Importable package.** Move the pipeline into `src/transcribe/` with a `pyproject.toml`, no third-party runtime dependencies. `bin/transcribe` remains a symlinkable launcher that needs no install step. Public entry points: run the pipeline on a file, load an existing package, assign speaker names, render the Markdown.
2. **Package sidecar.** `<stem>-package.json`, schema `transcribe.package.v1`: SHA-256 and byte size of the source recording, SHA-256 of the adjusted audio and of the raw provider JSON, the effective preprocessing and speech-to-text settings, detected language, duration, tool version, and the names of the package's files. The raw provider JSON, with word timings and per-word confidence, stays the immutable evidence and is never rewritten.
3. **Speaker table.** `<stem>-speakers.json`, schema `transcribe.speakers.v1`: one row per speaker identity the provider emitted, each with a nullable `name`, an optional `note`, and descriptive counts (turns, words, speaking seconds, first and last time heard). The table is keyed by speaker identity, so one name may be given to several identities when the provider split one person; a derived view groups identities by name. Transcript turns keep the provider's identity visible when a name is shown.
4. **Markdown is derived.** The Markdown is rendered from the raw JSON, the sidecar, and the speaker table, so it can be re-rendered after names change. It carries `source_sha256` in its frontmatter and a Speakers table ahead of the turns.
5. **Naming sub-command.** `transcribe speakers PACKAGE` prints the table; `transcribe speakers PACKAGE speaker_0="Name" speaker_3="Name"` assigns names; `--clear ID` removes one. Each change re-renders the Markdown. Unknown identities are refused.

## Acceptance

- The existing suite still passes after the move, and new tests cover the sidecar, the hashes, the speaker table, many identities to one name, refusal of unknown identities, and re-rendering.
- A package built offline from an existing raw provider response is complete and re-renderable.
- One live end-to-end run on the repository's fixture produces a package with all five files, and assigning one name to two identities shows in both the table and the turns.
- A consumer can `import transcribe` from a path dependency and call the public entry points.
