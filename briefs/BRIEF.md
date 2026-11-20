---
title: "Quillric"
date: 2026-11-20
status: implemented
scope: The existing root Python CLI and library that turns one audio or video file into a self-contained, diarized transcription package.
---

# Quillric

*One recording in, one self-contained transcription package out.*

## Thesis

Quillric copies the source recording, measures how uneven its speech level is, optionally levels it, transcribes it, preserves the complete provider response as immutable evidence, records the identity of the source recording, and renders a diarized Markdown transcript with a speaker table whose names can be assigned later. The repository README is the authoritative product reference; the narrative plans under `plan/` record the implemented history without being lifecycle records.

## Catalog

The [root catalog](../AGENTS.md#quillric) is the navigation entry point. The [speaker resolution workbench](human-in-the-loop-speaker-resolution.md) remains a proposal; it does not claim implemented functionality. Future work receives approved phase definitions before it starts.
