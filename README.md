# Quillric

Quillric turns one audio or video file into a self-contained transcription
package. It copies the source, measures how uneven the speech level is, optionally
levels the audio at Auphonic, transcribes it with ElevenLabs Scribe v2 (twice when
the recording is uneven, to find the words the service is unsure of), preserves the
complete API response,
records the identity of the source recording, and renders a diarized Markdown
transcript with a speaker table in which names can be assigned later.

## Install

The distribution is named **`quillric`** (two Ls). The Python import remains
`transcribe`, and the command remains `transcribe`; APIs, package schemas and
output filenames are unchanged. The GitHub repository is
[`wolfmcnally/quillric`](https://github.com/wolfmcnally/quillric).

The core package has no third-party Python dependencies and runs on Python 3.9–3.14
on Linux and macOS. Windows is not supported (the local locking code uses `fcntl`). Normal transcription also needs `ffmpeg` on the path.
The first public release is **0.1.0**. Earlier 1.x versions were private development versions; existing transcript packages keep their recorded versions and schemas. Quillric is distributed through GitHub releases, not PyPI. Install the release wheel in a fresh environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install https://github.com/wolfmcnally/quillric/releases/download/v0.1.0/quillric-0.1.0-py3-none-any.whl
.venv/bin/transcribe --version
```

Install `ffmpeg` separately (`brew install ffmpeg` on macOS, or your Linux package manager). To add local duplicate detection, install NumPy in the same environment (`.venv/bin/python -m pip install 'numpy>=1.24'`). The release includes SHA-256 checksums for its wheel and source archive. A pinned source checkout is an alternative:

```sh
git clone --branch v0.1.0 https://github.com/wolfmcnally/quillric.git
cd quillric
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/transcribe --version
```

Use a dedicated environment. **Do not coinstall distributions `quillric` and
`transcribe`: they can both own the `transcribe` import and executable.** pip does
not detect that file-level conflict. When migrating an existing environment,
uninstall the old `transcribe` distribution first, then install Quillric; a fresh
environment is preferable. Never uninstall either shared-file distribution after
coinstallation and assume the other is intact: rebuild the environment instead.

A simple preflight for an existing target environment refuses ambiguous ownership:

```sh
python -c 'import importlib.metadata as m; import sys; sys.exit("Use a fresh environment: transcribe is already installed") if any(d.metadata["Name"].lower() == "transcribe" for d in m.distributions()) else None'
```

The launcher in `bin/` still runs the checkout directly without installation. For
Wolf's existing directory layout its compatible symlink is:

```sh
ln -s ../DevProjects/quillric/bin/transcribe ~/bin/transcribe
```

Another Python project can depend on distribution `quillric` by path or Git URL
and continue to `import transcribe`; see [Library use](#library-use). Renaming the
distribution does not satisfy a consumer requirement for distribution `transcribe`;
requirements, source mappings and locks must be upgraded together.

For normal transcription, create an ElevenLabs account with Scribe API access and set `ELEVENLABS_API_KEY`. Add an Auphonic account/key (`AUPHONIC_API_KEY`) only if leveling is selected. Use the environment or a private `.env` in your working directory; a source checkout also searches its root `.env`. Installed wheels do not search an arbitrary checkout. Never commit keys. The command does not print them.

Start by reviewing a local dry run. To request exactly one paid transcription without Auphonic, use:

```sh
transcribe recording.mp3 --leveling off --second-pass off --dry-run
transcribe recording.mp3 --leveling off --second-pass off
```

The second command uploads audio and can incur provider charges. The normal default `--second-pass auto` can upload and bill a second transcription for uneven recordings. There is no offline speech recognition backend. See [privacy and package trust](#privacy-and-package-trust) before uploading sensitive recordings.

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
    filename-adjusted.mp3        # only when the audio was leveled
    filename-raw.json
    filename-second-raw.json     # only when a second pass ran
    filename-package.json
    filename-speakers.json
    filename-transcription.md
```

`filename-raw.json` is the provider's complete response, with word timings and
per-word confidence. It is the evidence and is never rewritten.
`filename-package.json` (schema `transcribe.package.v1`) is the machine-readable
description of the package: the SHA-256 and size of the source recording, the
SHA-256 of the adjusted audio and of the raw JSON, the effective settings, the
detected language and duration, and the names of the other files.
`filename-speakers.json` is the [speaker table](#speakers). The Markdown is
derived from those three and is rendered again whenever the table changes.

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

## Leveling and the second pass

Before anything is sent, the recording is measured locally with `ffmpeg`: its speech is cut into 0.4 s windows, silence is dropped, and the **spread** is the distance in decibels between loud speech (95th percentile) and quiet speech (10th). One steady voice measures a few decibels; a room with good and bad microphones measures 26 and up. A recording at or above `--uneven-threshold` (default 25 dB) is *uneven*. The measurement covers the whole file, because a long recording can be even within every five minutes and uneven between its sections.

```sh
transcribe call.m4a                          # no leveling; a second pass only if the recording is uneven
transcribe hearing.mp3 --leveling auto       # level at Auphonic only if uneven
transcribe hearing.mp3 --leveling on         # always level (the behaviour before 1.3)
transcribe hearing.mp3 --second-pass on --uneven-threshold 22
transcribe hearing.mp3 --dry-run             # measures, and says what it would do, without sending anything
```

**Leveling is off by default.** In a paired test of thirteen five-minute excerpts spanning 9 to 35 dB of spread, leveled and unleveled audio produced the same number of words (within 2%), the same words in the quiet stretches, the same speakers and, in eleven of thirteen, identical speaker attribution. Leveled audio scored slightly higher recognition confidence on uneven recordings. What did differ was the wording, by 4 to 7% on uneven audio, but a control showed why: **the same unleveled audio transcribed twice agreed with itself only 95 to 97% on hard recordings** (99.7% on an easy one). Most of the apparent leveling effect was the service varying from run to run. The earlier fixture study that chose the leveling-only preset compared Auphonic presets with one another; it did not show leveled audio transcribing better than unleveled. As of 2026-09-18. Without `--leveling on` or an uneven recording under `--leveling auto`, Auphonic is never contacted and no Auphonic key is needed; the package then has no `-adjusted.mp3`.

**The second pass** follows from that control. For an uneven recording the audio is transcribed a second time with identical settings, and **one transcript is made from both passes, with their disagreements kept in the flow**:

```markdown
[00:04:12.300 --> 00:04:19.850] **speaker_3:** I went through a {POST | police} academy, and then I {— | or} I was assigned
[00:04:20.100 --> 00:04:20.900] **speaker_1 or speaker_3:** Okay.
```

Where the passes heard different words, both readings sit inline as `{first | second}`, first pass first, with `—` where a pass heard nothing. Where they gave the same words to different speakers, that stretch is its own turn labelled with both. Reading the first alternative everywhere gives back the first pass word for word, and the second alternative the second pass; nothing is invented or dropped, and neither pass is treated as right. Casing and punctuation are not disagreements.

The provider's speaker labels are arbitrary per run, so the second pass's labels are first matched to the first pass's by the identically recognised words they share, and failing that by when they spoke. A voice only the second pass told apart keeps its own identity, `second:speaker_N`, and gets a row in the speaker table so it can be named. Once two disputed identities carry the same name the dispute is over, and the turn reads `Jane Smith (speaker_1, speaker_3)`.

`filename-second-raw.json` keeps the second response, and the sidecar's `second_pass` holds the agreement and every word difference with its time and surrounding words, as an index for a reviewer. `Package.merged_turns()` returns the merged turns to a program.

The sidecar records the decision either way: `leveling.mode`, `leveling.applied`, the threshold, and the measured levels.

## Speakers

Diarization labels voices `speaker_0`, `speaker_1`, and so on. Those labels are
local to one recording and say nothing about who is speaking, and a provider
sometimes hears one person as two or more identities. The speaker table
(`filename-speakers.json`, schema `transcribe.speakers.v1`) holds one row per
identity the provider emitted, with a `name` that starts empty, an optional
`note`, and descriptive counts: turns, words, speaking seconds, and the first and
last time heard. Because the table is keyed by identity, giving the same name to
several rows is how one person with several identities is recorded.

```sh
transcribe speakers /transcripts/filename
transcribe speakers /transcripts/filename speaker_0="Jane Smith" speaker_3="Jane Smith"
transcribe speakers /transcripts/filename --note speaker_3="same voice after the recess"
transcribe speakers /transcripts/filename --clear speaker_3
```

With no assignments the sub-command prints the table. (A recording literally
named `speakers` is still transcribed by writing it as `./speakers`.) Each change rewrites the table
and re-renders the Markdown; the raw JSON is untouched, and its hash is checked
first so altered evidence is refused. An identity the table does not hold is
refused, so a typo cannot invent a speaker. In the Markdown the table appears
under `## Speakers`, and a named turn keeps the provider's identity visible:

```markdown
[00:01:23.456 --> 00:01:27.890] **Jane Smith (speaker_3):** Transcript text.
```

Naming is a record of someone's judgment, not voice identification: nothing here
infers who a speaker is.

## Duplicates

The same recording often arrives more than once: as `.m4a` and again as `.mp3`, at another bitrate, trimmed, or quieter. Bytes and durations differ, so a hash cannot see it, and transcribing both pays twice. `transcribe duplicates` finds them locally; nothing leaves the machine.

```sh
transcribe duplicates /recordings                       # directories are searched recursively
transcribe duplicates /recordings --cache ~/.cache/transcribe-fingerprints --workers 8 --json
```

It needs `numpy` (`.venv/bin/python -m pip install ".[fingerprint]"`) and `ffmpeg`. Each file's first audio stream is decoded to 5512 Hz mono, and every 46 ms a 32-bit code is taken from the sign of energy differences between 33 log-spaced bands across time and frequency, the scheme of Haitsma and Kalker's "A Highly Robust Audio Fingerprinting System" (ISMIR 2002). An index of codes proposes a time offset for a pair of files, files of nearly equal length are also compared directly, and the proposal is verified by the bit error rate over every aligned, non-silent frame. Verified at or below 0.25 is the same audio; unrelated audio sits near 0.5.

The report groups files that are the **same recording** (each covers at least 95% of the other), and lists separately a recording that **contains** another (a clip, with the offset where it starts) and a partial **overlap**; those are reported, never grouped, because a clip is not a duplicate. It also gives the count and hours of unique recordings, which is what a transcription budget should be based on. Silence and undecodable files match nothing, and an undecodable file is reported, not skipped quietly.

Measured on an 8.5 minute recording: lossless, 64 kbit AAC, 24 kbit 8 kHz MP3, 24 kbit Opus, and a copy trimmed by 3.4 s and lowered 12 dB all verified at a bit error rate of 0.00 to 0.19 and grouped together; a two-minute excerpt was reported as contained at the right offset; two unrelated recordings measured 0.49. Fingerprinting ran at about 600 times real time on one core. As of 2026-09-18.

```python
from transcribe import fingerprint

prints = [fingerprint.fingerprint_file(path) for path in paths]
matches = fingerprint.find_matches(prints)
groups = fingerprint.duplicate_groups(prints, matches)
```

## Library use

```python
import transcribe

package = transcribe.transcribe_file("hearing.mp3", "--output-dir", "/packages/hearing")
package = transcribe.Package.load("/packages/hearing")   # an existing package

package.source_sha256        # identity of the source recording
package.sidecar              # the transcribe.package.v1 description
package.transcript()         # raw provider response, hash-verified
package.speakers             # the transcribe.speakers.v1 table
package.assign({"speaker_0": "Jane Smith", "speaker_3": "Jane Smith"})
package.people()             # {"Jane Smith": ["speaker_0", "speaker_3"]}
package.render()             # the Markdown, as a string
```

`transcribe_file` takes the command's own flags as extra arguments, runs the paid
pipeline quietly, and raises on failure. Existing calls still load keys from the
environment or `.env`. Library callers can instead supply per-call credentials:

```python
package = transcribe.transcribe_file(
    "hearing.mp3", "--leveling", "off",
    elevenlabs_api_key=credentials.elevenlabs,
)
```

The keyword-only arguments are `elevenlabs_api_key`, `auphonic_api_key`,
`elevenlabs_client`, and `auphonic_client`. Pass a nonempty key or a client for
each provider, never both. An omitted provider falls back to environment/`.env`
lookup only if that provider is needed. Keys are not placed in settings,
artifacts, or progress output, and the call does not change `os.environ`.
Injected clients use the existing `ElevenLabsClient` or `AuphonicClient` methods;
the same transcription client handles both passes. Use a separate client per
concurrent call unless the client implementation supports sharing. Client
injection also supports offline tests. No CLI credential flag was added.

A request ElevenLabs refuses with HTTP 429 (a rate or concurrency limit) is sent again after 15, 30, 60 and 120 seconds, the backoff the provider's error guidance asks for; a refused request did no transcription, so a completed first pass is never repeated because the second was refused. Any other error, or a refusal that outlasts the waits, raises as before. Keep simultaneous requests within the account's limit, remembering that ElevenLabs transcribes a recording longer than eight minutes in up to four parallel pieces, each counted against that limit.

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

The default preprocessing profile is [`src/transcribe/leveling-only.json`](src/transcribe/leveling-only.json), which ships inside the package:

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

The document title and interpretation metadata live only in YAML frontmatter,
including `source_sha256`; the renderer does not add a Markdown title header. The
body has two sections, `## Speakers` and `## Transcript`.

## Privacy and package trust

A normal transcription sends the recording and filename to ElevenLabs. An
uneven recording is sent twice under the default second-pass policy. Auphonic
receives the recording only when leveling is selected or an existing production
is resumed. Level measurement and duplicate detection run locally with ffmpeg.
`--dry-run` makes no provider calls and loads no credentials.

Auphonic API bases and result downloads must use HTTPS, including redirects. API bases and download URLs
containing username/password credentials are refused. The bearer token is sent
only to the configured Auphonic HTTPS origin, including its port; external
storage URLs are fetched without it. A redirect to another origin strips the
token and it is never restored later in that redirect chain. JSON API calls use the same safe redirect handler; uploads use a direct HTTPS connection and do not follow redirects. API bases with URL credentials, query strings or fragments are refused. There is no plaintext localhost exception: inject a fake client for offline tests. Signed download URL query
parameters remain intact.

Packages keep the source recording, raw provider responses, timings, settings,
and assigned speaker names/notes. Fingerprint caches keep paths, durations and
acoustic codes. They have no built-in expiry or encryption. Local failure cleanup
does not delete provider-side recordings or productions; retention and deletion
at each provider remain the user's responsibility.

Review the providers' current policies and your account settings: [ElevenLabs privacy policy](https://elevenlabs.io/privacy-policy) and [Auphonic privacy policy](https://auphonic.com/privacy). Do not assume that deleting a local package deletes uploaded audio, that a production's expiry deletes all retained excerpts, or that a provider account has zero retention. Quillric does not request zero-retention mode, disable provider training settings, or issue deletion requests. Provider terms, regions and account agreements control that handling.

Back up or remove local packages and fingerprint caches according to your own retention policy. Output inherits ordinary filesystem permissions; use a private directory and restrictive umask for sensitive material. Progress and errors can reveal paths, filenames or provider messages. Quillric has no telemetry endpoint. Its configured cloud calls are ElevenLabs transcription and optional Auphonic processing/downloads.

A loaded package must use flat filenames. Absolute paths, traversal, and symlinks
that resolve outside the package are refused for the sidecar, source and every
referenced file. Paths are checked again for later reads and speaker updates;
internal symlinks remain supported. Raw hashes detect changes relative to the
sidecar, not the authenticity of a package whose sidecar can also be edited.
Use a private working directory: these checks do not protect against a hostile
process replacing filesystem entries concurrently between validation and I/O.

## Test

```sh
python3 -m pip install '.[fingerprint]'
python3 -m unittest discover -s tests -v
```

The suite covers configuration resolution, safe credential loading, output-path
safety, API request construction, cross-origin credential stripping, terminal,
plain, JSON, and quiet progress behavior, timestamp rendering, speaker turns,
failure cleanup, a mocked full pipeline, the package sidecar and its hashes, the
speaker table, one name covering several identities, refusal of unknown
identities and of altered evidence, and re-rendering after names change.

CI runs the complete offline suite on Linux/macOS and Python 3.9–3.14 with ffmpeg and NumPy installed, then builds and checks wheel/source archives and performs an installed-wheel smoke test. Synthetic audio and fake/local HTTP clients are used; the suite needs no provider credentials or paid calls. A skip is a CI failure. Release publication requires green CI on the exact tagged commit.

## License

MIT. See [`LICENSE`](LICENSE). NumPy is an optional external dependency; ffmpeg is installed separately and is not bundled. Each retains its own license. The cited fingerprinting research describes the algorithm's provenance; it is not a patent-clearance claim. Research/design documents describe proposals or historical experiments where stated; they do not promise implemented functionality.
