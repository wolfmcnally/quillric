# Phase 3 — Configuration-selected transcription providers

The owner authorized the changes needed for configuration-only operation and qualification in a hardware-isolated, networkless Linux arm64 environment. The implementation uses Quillric’s normal CLI and library entry points and preserves the existing cloud providers and transcription evidence. All validation uses synthetic input, local tools and fake providers.

Select transcription, diarization and leveling independently. Validate provider selection, egress and capability constraints before source content is read. Use strict versioned JSON and bounded subprocesses for configured helpers. Record deterministic repetitions that cannot provide an independent second pass, preserve derived diarization alongside immutable raw responses, and perform video demux and optional leveling with local media tools.

Acceptance requires the complete offline suite without skips, archive and clean-wheel checks, and current networkless Linux aarch64 glibc evidence for the final code with pinned runtime binaries. Candidate quality remains pending until the documented comparative experiment is run. A paired vendor leveling comparison requires already available, authorized, synthetic paired outputs.
