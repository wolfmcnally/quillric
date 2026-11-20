# Quillric

A recording on its own is hard to search, cite or attribute. Quillric turns one audio or video file into a self-contained transcription package: a copy of the source, the provider's complete response, a machine-readable description with the source recording's identity, a speaker table in which names can be assigned later, and a diarized Markdown transcript rendered from them.

Transcription uses ElevenLabs Scribe v2, optionally after leveling at Auphonic; level measurement and duplicate detection run locally with ffmpeg. An explicit configuration can select other transcription, diarization and leveling providers. The repository README is the complete reference for options, package contents, privacy and provider handling.

The repository adopts development practices from [Starter](https://github.com/wolfmcnally/agentic-coding-starter); this manual describes Quillric's product.
