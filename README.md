# transcribe

`transcribe` turns one audio or video file into a self-contained transcription
package. It copies the source, runs an Auphonic preprocessing pass, transcribes
the adjusted MP3 with ElevenLabs Scribe v2, preserves the complete API response,
and renders a diarized Markdown transcript.

## Install

The executable has no third-party Python dependencies and runs on Python 3.9 or
newer. Make it available on the shell path with a symlink:

```sh
ln -s ../DevProjects/transcribe/bin/transcribe ~/bin/transcribe
```

Provide `AUPHONIC_API_KEY` and `ELEVENLABS_API_KEY` in the environment or in a
`.env` file in the current directory or project root. The command does not print
either key.

## Use

```sh
transcribe /recordings/filename.mp3
```

With only an input path, the package is created beside the input:

```text
/recordings/
  filename.mp3
  filename/
    filename.mp3
    filename-adjusted.mp3
    filename-raw.json
    filename-transcription.md
```

`--output-dir` selects the exact package directory:

```sh
transcribe filename.mp3 --output-dir /transcripts/filename
```

An existing package is never changed unless `--force` is supplied. The command
builds the new package in a staging directory and publishes it with one rename,
so an API failure cannot leave a destination that looks complete. It also
refuses a destination that contains the input file, preventing `--force` from
moving or deleting the source.

Use `--dry-run` to inspect the fully resolved settings without loading API keys,
making API calls, or writing files:

```sh
transcribe filename.mp3 --dry-run
```

Run `transcribe --help` for every override. Common experiments include:

```sh
transcribe filename.mp3 \
  --speech-leveler-strength 120 \
  --auphonic-set compressor_speech=soft \
  --diarization-threshold 0.25
```

`--auphonic-config` accepts a JSON object of Auphonic algorithm fields, while
repeatable `--auphonic-set KEY=VALUE` arguments override individual fields.
Values are parsed as JSON when possible. `--max-speakers N` replaces automatic
threshold-based speaker counting. `--language-code eng` pins a known language;
omitting it enables language detection.

If a local run fails after Auphonic has already completed, resume from that
production rather than paying to process the source again:

```sh
transcribe filename.mp3 --resume-auphonic-production PRODUCTION_UUID
```

Resume mode copies the original into a fresh staging package, retrieves the
existing production and its actual algorithm metadata, downloads its adjusted
MP3, and continues with ElevenLabs and artifact publication. Provider-returned
download URLs are percent-encoded before use, including filenames containing
spaces, while existing signed percent escapes are preserved.

## Progress and scripting

Routine progress is written to standard error; the final package path is the
only normal standard-output line. This keeps command substitution and pipelines
clean:

```sh
package_path=$(transcribe filename.mp3)
```

The default `--progress auto` chooses an updating single-line display when
standard error is an interactive terminal and durable newline-delimited text
when it is redirected to a log. Live lines are width-bounded so long filenames
cannot wrap and leave stale rows behind. Measured updates are rate-limited while
always emitting completion, preventing fast local copies from flooding either a
terminal or log. Exact byte percentages are reported while copying, uploading,
and downloading. Auphonic processing and ElevenLabs
recognition are remote, indeterminate-duration stages, so the command reports
provider state changes and periodic heartbeats without fabricating a percentage.

Automation can request versioned NDJSON events on standard error:

```sh
transcribe filename.mp3 --progress json 2>progress.ndjson
```

Each record uses schema `transcribe.progress.v1` and includes a timestamp,
elapsed time, stage, state, and message. Measured transfers also include
`current`, `total`, `unit`, and `percent`. `--progress plain` forces line-oriented
human-readable logs, while `--progress tty` forces the live display.

Quiet mode suppresses routine progress but preserves the result path and real
errors:

```sh
transcribe filename.mp3 --quiet
```

This stdout/stderr separation, TTY detection, line-oriented redirected output,
and truthful indeterminate progress follow the
[Command Line Interface Guidelines](https://clig.dev/#output). Retrieved
2026-08-29.

## Defaults

The default preprocessing profile is [`configs/leveling-only.json`](configs/leveling-only.json):

- adaptive speech leveling enabled at strength 110;
- multi-speaker classifier enabled;
- denoising, filtering, loudness normalization, compression, and every cutter
  disabled;
- no silence trimming;
- native 128 kbps MP3 output from Auphonic.

The default transcription profile uses Scribe v2 with diarization, automatic
language and speaker-count detection, a `0.22` diarization threshold, word-level
timestamps, verbatim speech, and audio-event tags disabled. These settings were
selected from the fixture experiments as the most balanced result across the
quiet and loud voices. The CLI records the effective processing and STT settings
in every Markdown file's YAML frontmatter.

As of 2026-08-29, ElevenLabs documents `0.22` as its usual automatic diarization
threshold, permits `0.1` through `0.4`, and explains that larger values merge
speakers more readily while smaller values split them more readily. It also
documents automatic language prediction when `language_code` is omitted and
word-level timestamps. Retrieved 2026-08-29 from the
[ElevenLabs speech-to-text API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert).

As of 2026-08-29, Auphonic documents native MP3 output with explicit bitrate and
filename fields. Retrieved 2026-08-29 from the
[Auphonic production API reference](https://auphonic.com/help/api/details.html#output-files).

## Markdown timestamp convention

There is no broadly adopted Markdown-specific timed-transcript standard. The
renderer therefore combines ordinary Markdown speaker turns with the W3C WebVTT
cue interval convention:

```markdown
[00:01:23.456 --> 00:01:27.890] **speaker_0:** Transcript text.
```

Intervals use a start time, spaces around the literal `-->` separator, an end
time, fixed hours, and millisecond precision. Speaker changes start new
paragraphs. This keeps the Markdown readable while making its timing syntax
directly recognizable and mechanically parseable. The current WebVTT draft uses
the same interval and voice-label concepts for time-aligned captions. As of
2026-05-20; retrieved 2026-08-29 from the
[W3C WebVTT specification](https://www.w3.org/TR/webvtt1/). The emphasis on
unambiguous speaker attribution also follows the accessibility goals in
Columbia University's
[Oral History Transcription Style Guide](https://incite.columbia.edu/works/oral-history-transcription-style-guide)
(retrieved 2026-08-29).

The document title and interpretation metadata live only in YAML frontmatter;
the renderer does not add a Markdown title header.

## Test

```sh
python3 -m unittest discover -s tests -v
```

The suite covers configuration resolution, safe credential loading, output-path
safety, API request construction, cross-origin credential stripping, terminal,
plain, JSON, and quiet progress behavior, timestamp rendering, speaker turns,
failure cleanup, and a mocked full pipeline.
