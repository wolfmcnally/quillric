# Human-in-the-Loop Speaker Resolution Workbench

Status: Proposal

Research retrieved: 2026-08-29

## Decision summary

Build a cluster-first, evidence-backed speaker resolution workbench for
pre-segmented transcripts. The system should treat existing timestamps,
segments, and anonymous speaker labels as a starting hypothesis rather than
ground truth. AI agents propose small, reviewable changes; human reviewers
identify known voices, correct clustering and boundaries, and approve any
propagation. Every decision remains traceable to the original audio and can be
reversed.

The primary unit of work is an assertion such as:

- `speaker_3` is the same voice as `speaker_7`;
- segment 187 belongs to `speaker_4`, not `speaker_2`;
- the speaker changes at 00:12:14.280; or
- `speaker_2` is the person identified as Jane Smith.

The product should not behave like a conventional text editor with speaker
names added. It should behave like an audio-evidence review system whose
approved decisions produce a readable transcript.

## Problem

Automated diarization ordinarily answers “who spoke when” by segmenting speech
and clustering similar voices under recording-local labels such as
`speaker_0`. It does not necessarily determine the real-world identity of the
speaker. Pre-segmented transcripts can therefore contain at least four
independent kinds of error:

| Task | Example |
| --- | --- |
| Boundary correction | A speaker changes midway through a segment. |
| Cluster correction | `speaker_2` contains utterances from two people. |
| Cluster consolidation | `speaker_2` and `speaker_5` are the same person. |
| Identity resolution | `speaker_2` is assigned to Jane Smith. |

These tasks must remain separate in the interface, data model, confidence
scores, audit trail, and evaluation. A pure cluster may still have an unknown
identity, while a correctly identified person may have contaminated segments
in their cluster.

NIST describes speaker diarization as determining who spoke when. The
literature further distinguishes its segmentation and clustering operations
from speaker identification and speaker-attributed speech-to-text. Generic
speaker labels are therefore file-local identifiers, not durable person
records. See [NIST Rich Transcription Evaluation][nist-rt],
[Desplanques et al.][desplanques], and [pyannote terminology][pyannote-models].

## Goals

The workbench should:

1. Make known-voice identification fast for a human who knows some or all of
   the participants.
2. Find cluster impurity, fragmentation, boundary errors, overlaps, and
   incidental voices without forcing every voice into the main roster.
3. Ask the smallest, highest-value questions that reduce the most consequential
   uncertainty.
4. Expose the original audio and transcript context supporting every automated
   recommendation.
5. Make bulk changes previewable, reversible, and auditable.
6. Preserve raw provider output and export a resolved transcript as a derived,
   versioned artifact.
7. Support multiple reviewers and adjudication without losing conflicting
   judgments.

## Non-goals

The first version should not:

- silently identify people from voice embeddings;
- assume that the diarization provider’s segments or labels are correct;
- require every incidental voice to receive a real-world name;
- rewrite raw words, timestamps, or provider JSON in place;
- infer a cross-recording identity merely because two local labels look or
  sound similar; or
- use a single confidence number for clustering, boundaries, transcription,
  and identity.

## Core review workflow

### 1. Ingest immutable evidence

Import the original audio, raw word timestamps, supplied segments, anonymous
speaker labels, confidence data, and provider response. Assign stable internal
IDs to words and source segments. Corrections are stored as a separate layer of
assertions and events.

### 2. Construct a speaker inventory

Create one card for every anonymous speaker cluster, plus explicit Unknown,
Incidental Voice, and Non-Speech categories. Each card should show:

- anonymous cluster label;
- segment and word counts;
- total speaking duration and percentage of recording;
- distribution across the recording timeline;
- three to five clean representative clips from different recording regions;
- two or more anomalous or low-confidence clips;
- transcript context and timestamps for every clip;
- proposed identity, alternatives, and review status; and
- cluster-purity and identity confidence as separate values.

Representative clips should favor sufficient duration, low overlap, good
signal quality, and closeness to the cluster’s acoustic center. That selection
is only a proxy for the typical voice and can conceal contaminated outliers.
The interface must therefore present central examples and outliers together.

### 3. Generate agent proposals

Agents may use distinct evidence classes:

- acoustic similarity or distance;
- verified reference clips;
- explicit self-introductions;
- forms of address and participant names;
- conversational role and turn-taking;
- recording metadata and a supplied participant roster; and
- disagreement between transcript content, embeddings, and existing labels.

Agents should propose atomic actions, alternatives, counterevidence, and the
original intervals supporting the proposal. They must never commit identity or
bulk attribution changes silently.

### 4. Conduct focused human review

Show one bounded decision at a time. Useful questions include:

- Are these two clips the same person?
- Is this a known reference voice?
- Does this segment belong to the currently named cluster?
- Where does the speaker change?
- Is this an incidental or unknown voice?

The reviewer must be able to Accept, Correct, Reject, Mark Unsure, or Skip and
then advance automatically. Active-learning annotation systems such as
INCEpTION expose the proposed label, confidence, competing candidate, simple
review actions, automatic advancement, and navigable history. Research on
human-assisted diarization likewise supports ordering simple correction
questions to reduce error with limited human effort; one reported result was
up to a 36.5% relative reduction in diarization error. See the
[INCEpTION user guide][inception] and [Prokopalo et al.][active-correction].

### 5. Preview and apply propagation

Before applying a cluster-wide identity or merge, show:

- affected segments, words, and duration;
- representative excerpts;
- known outliers and segment-level overrides;
- conflicts with earlier human decisions; and
- the exact before-and-after mapping.

Apply the decision as one reversible changeset. A segment-level correction
must override, rather than destructively alter, a cluster-level default.

### 6. Review uncertainty and impact

Prioritize work by expected error reduction per reviewer-second, not by model
uncertainty alone. The queue should account for:

- likely mixed clusters;
- likely duplicate clusters;
- ambiguous high-duration mappings;
- overlap and boundary problems;
- identities affecting consequential portions of the transcript;
- short and incidental voices that duration-weighted ranking would hide; and
- disagreements among reviewers or evidence classes.

The system should also sample apparently high-confidence decisions at random.
Uncertainty scoring cannot reveal a systematic error that the model is
confidently repeating.

### 7. Adjudicate and export

Support independent review and a conflict queue. For consequential transcripts,
named identities should be eligible for second-reviewer confirmation or formal
adjudication. Export the resolved transcript, speaker registry, decision log,
and machine-readable mappings while preserving the source evidence unchanged.

## Interface proposal

The primary experience should be desktop-first and keyboard-first:

```text
┌────────────── Player · waveform · loop · speed · timeline ──────────────┐
│ Speaker lanes, current interval, boundaries, overlap, recording minimap  │
├──────── Speaker inventory ───────┬──────── Current evidence ─────────────┤
│ speaker_0  18:42  Confirmed      │ Previous turn                         │
│ speaker_1   6:11  Proposed       │ ▶ Current clip and transcript          │
│ speaker_2   0:37  Needs review   │ Next turn                             │
│ Unknown     0:12                 │ Waveform selection and loop            │
├──────────────────────────────────┼──────── Identity candidates ───────────┤
│ Review queue                     │ Jane Smith                  72%         │
│ History and undo                 │ John Jones                  19%         │
│ Progress and unresolved duration │ Unknown · New person · Role only       │
└──────────────────────────────────┴────────────────────────────────────────┘
```

### Required interactions

- Play, pause, replay, loop, previous clip, and next clip without leaving the
  keyboard.
- A/B comparison between candidate clips or a clip and a verified reference.
- Adjustable playback speed without pitch shifting.
- Configurable pre-roll and post-roll.
- Previous, current, and next transcript turns around the audio selection.
- Waveform zoom, speaker lanes, and a minimap for long recordings.
- Fast candidate selection by number key.
- Cluster rename, merge, split, segment reassignment, and boundary movement.
- Explicit overlap, Unknown, Incidental Voice, Role Only, and New Person
  choices.
- Immediate undo, redo, and visible decision history.
- Autosave after every atomic decision.

Waveform-aligned annotations and separate speaker lanes are established audio
annotation patterns. See the [ELAN introduction][elan] and
[Unitlab audio annotation overview][unitlab].

### Suggested keyboard map

| Key | Action |
| --- | --- |
| Space | Play or pause |
| `L` | Replay or loop current evidence |
| `J` / `K` | Previous or next evidence clip |
| `[` / `]` | Adjust pre-roll or post-roll |
| `1`–`9` | Choose an identity candidate |
| `U` | Mark unknown |
| `I` | Mark incidental voice |
| `M` | Propose merge or same-person relation |
| `S` | Split or reassign current segment |
| `R` | Reject the proposal |
| `?` | Mark unsure or request adjudication |
| Command/Control-Z | Undo |

Keyboard shortcuts must supplement, not replace, labeled controls.

## Agent behavior contract

Agents are recommenders and reviewers, not silent mutators. A proposal should
have a structure equivalent to:

```json
{
  "proposal_id": "proposal_123",
  "scope": "cluster",
  "target_ids": ["speaker_2"],
  "action": "assign_identity",
  "candidate_identity": "Jane Smith",
  "confidence": 0.72,
  "alternatives": [
    {"identity": "John Jones", "confidence": 0.19}
  ],
  "evidence": [
    {
      "type": "verified_reference_voice",
      "audio_start": 84.2,
      "audio_end": 91.8
    },
    {
      "type": "self_introduction",
      "audio_start": 114.0,
      "audio_end": 118.3,
      "text": "This is Jane Smith..."
    }
  ],
  "counterevidence": [],
  "model": "provider/model/version",
  "status": "proposed"
}
```

Logical agent roles may include:

- an acoustic agent proposing same-voice or different-voice relationships;
- a discourse agent finding introductions, names, and role evidence;
- a purity agent finding clips inconsistent with their assigned cluster;
- a review scheduler selecting the next high-value question; and
- a quality agent challenging accepted decisions and selecting random audits.

Agreement among agents is not independent corroboration when the agents depend
on the same embedding, transcript clue, or upstream diarization label. The UI
should group evidence by underlying source rather than count agent votes.

## Evidence and epistemic safeguards

Every automated signal scores a proxy for the desired fact:

| Signal | Proxy | Important false positives or inversions |
| --- | --- | --- |
| Acoustic similarity | Same real-world person | Similar voices, short clips, shared microphones, channel effects, noise, or voice changes can create false matches. |
| Representative clip | Cluster purity | A clean central clip can hide a minority of wrongly clustered segments. |
| Spoken name | Speaker identity | A person may be addressing, quoting, or introducing somebody else. |
| Conversational role | Person identity | Multiple participants may occupy the same role. |
| Model confidence | Correctness | It measures model certainty, not truth, and can be confidently miscalibrated. |
| Speaking duration | Importance | It suppresses brief speakers and incidental but consequential voices. |

Some failures invert the conclusion rather than merely add noise. For example,
a speaker saying “Judge Smith” may be confidently labeled as Judge Smith when
they are actually addressing the judge. No agent explanation may substitute for
the original timestamped audio and transcript context.

One clip is insufficient to establish both identity and cluster purity. A human
identity confirmation should ordinarily include multiple clips from different
parts of the recording, while a purity review should deliberately include
outliers.

Maintain separate confidence or review state for:

- cluster purity;
- segment membership;
- boundary accuracy;
- transcript accuracy; and
- real-world identity.

The system must permit “unknown” as a correct result rather than forcing the
nearest roster entry.

## Data model

Use immutable evidence objects and versioned assertions:

- **Recording:** source media, metadata, checksums, and provider provenance.
- **Word:** stable ID, text, time interval, original label, and source data.
- **Source segment:** original grouping of words and original speaker label.
- **Derived segment:** editable grouping and timing assertions.
- **Cluster:** anonymous recording-local voice grouping.
- **Identity:** person, role, alias, or explicitly unknown entity.
- **Reference voice:** optional, consented identity sample with provenance and
  retention policy.
- **Assertion:** a versioned cluster-, segment-, or interval-to-identity mapping.
- **Proposal:** an agent-recommended assertion with alternatives and evidence.
- **Review:** a reviewer’s decision, confidence, notes, and adjudication state.
- **Change event:** actor, timestamp, before and after state, reason, and source
  model version.

Overlapping speech requires many-to-many interval attribution. Identity renames
must not rewrite word records. Cross-recording matching must be an explicit
operation against a scoped identity registry.

Concurrent review should use optimistic locking. Conflicting changes remain
visible and enter adjudication rather than allowing last-writer-wins data loss.

## Review states

Use a richer lifecycle than Done or Not Done:

- Unreviewed
- Agent Proposed
- Human Confirmed
- Human Corrected
- Unknown
- Disputed
- Needs Second Reviewer
- Adjudicated

Track both cluster-purity state and identity-resolution state. A cluster may be
Human Confirmed as pure while remaining Unknown in identity.

## Evaluation and stopping criteria

### With reference truth

Report:

- missed-speech, false-alarm, and speaker-confusion components of diarization
  error;
- diarization error rate (DER) and Jaccard error rate (JER);
- speaker-attributed word error such as cpWER or tcpWER;
- identity accuracy separately from cluster accuracy; and
- macro or speaker-balanced performance so errors on short speakers are not
  washed out by long speakers.

DER combines missed speech, false alarms, and speaker confusion, while cpWER
evaluates recognition together with speaker attribution. Duration-weighted DER
can underrepresent errors affecting short speakers. See [Kalda et al.][kalda],
[dscore][dscore], and [Balanced Evaluation of Speaker Diarization][balanced].

### Without reference truth

Track:

- human-confirmed percentage by both words and duration;
- sampled cluster-purity rate;
- unresolved and disputed segments, words, and duration;
- unknown and incidental voice counts;
- reviewer agreement and adjudication rate;
- agent acceptance and correction rates by evidence type;
- calibration of confidence bands;
- review seconds per confirmed audio minute; and
- error rate from random audits of high-confidence decisions.

Do not declare completion from duration coverage alone. A reasonable stopping
rule should require:

1. Multiple human-reviewed samples for every cluster above an agreed materiality
   threshold.
2. Explicit accounting for all remaining unknown and incidental voices.
3. Unresolved and disputed material below agreed word, duration, and segment
   thresholds.
4. A passing random audit of high-confidence decisions.
5. Required second reviews or adjudications completed.

## Accessibility

All media and annotation operations must be keyboard-operable. Do not rely on
speaker color alone; pair color with text labels and distinguishable patterns.
Provide a transcript-list alternative to waveform navigation, visible focus,
screen-reader labels, and reflow for narrow windows. Avoid autoplay and preserve
independent volume and playback controls. W3C guidance calls for accessible
media controls together with synchronized text and transcript access. See
[W3C accessible media planning][w3c-media].

## Privacy and security

Voice references and embeddings should be handled as sensitive identity data:

- make voice enrollment optional and based on explicit authority or consent;
- keep identity registries project-scoped by default;
- separate names and case metadata from acoustic embeddings;
- define retention, deletion, and export behavior;
- encrypt stored reference material and restrict it by role;
- never perform implicit cross-project matching;
- record when a reference voice contributed to a decision; and
- allow a reviewer to identify a voice without creating a reusable voiceprint.

These are product safeguards, not a determination of the legal status of voice
data in any jurisdiction. Applicable biometric, privacy, evidentiary, and
records-retention requirements require separate review for the deployment
context.

## MVP scope

The first release should include:

- audio and raw transcript JSON import;
- immutable source preservation;
- speaker inventory with representative and outlier clips;
- synchronized transcript, waveform, speaker lanes, and context playback;
- known-speaker roster and local identity records;
- cluster-wide identity assignment with impact preview;
- segment override, merge, split, reassignment, boundary, and overlap tools;
- Unknown and Incidental Voice handling;
- keyboard-driven review queue;
- evidence-backed agent proposals;
- autosave, undo, version history, and audit export;
- reviewer progress and unresolved-material reporting; and
- resolved JSON, Markdown, RTTM, and caption export.

Optional cross-recording voiceprints, learned question scheduling,
multi-reviewer adjudication workflows, and organization-wide identity
registries should be later capabilities built on the same assertion model.

## Acceptance criteria

An MVP is ready for evaluation when a reviewer can:

1. Import one existing diarized transcript and its source audio without altering
   either source artifact.
2. Inspect representative and anomalous clips for every anonymous cluster.
3. Identify a cluster, preview the blast radius, apply the mapping, and undo it.
4. Correct a single segment without losing the cluster-level default.
5. Merge fragmented clusters and split a contaminated cluster.
6. Preserve an incidental voice as unknown without assigning a person.
7. Review an agent proposal together with its original audio evidence,
   alternatives, and counterevidence.
8. Complete the principal workflow without a pointing device.
9. Recover every decision, actor, timestamp, and model version from the audit
   trail.
10. Export resolved artifacts while reproducing the raw source bytes exactly.

## Research basis

The following sources informed this proposal. Retrieval dates indicate when the
linked material was consulted, not that every underlying result was newly
published on that date.

- NIST, [Rich Transcription Evaluation][nist-rt]. Retrieved 2026-08-29.
- Desplanques et al., [Unsupervised Speaker Diarization of Meetings Using
  Segment-Level Gaussian Mixture Models][desplanques], Interspeech 2015.
  Retrieved 2026-08-29.
- pyannoteAI, [Models and terminology][pyannote-models]. Retrieved 2026-08-29.
- Prokopalo et al., [Active Correction for Speaker Diarization with Human in the
  Loop][active-correction], IberSPEECH 2021. Retrieved 2026-08-29.
- INCEpTION, [Active learning user guide][inception], release 40.3.
  Retrieved 2026-08-29.
- ELAN, [Introduction and annotation interface][elan]. Retrieved 2026-08-29.
- Unitlab, [Audio annotation overview][unitlab]. Retrieved 2026-08-29.
- Kalda et al., [Evaluation of Speaker Diarization and Speaker-Attributed
  ASR][kalda], Odyssey 2024. Retrieved 2026-08-29.
- Ryan et al., [dscore diarization scoring tools][dscore]. Retrieved 2026-08-29.
- Plaquet and Bredin, [A Metric for Global Evaluation of Speaker Diarization
  Systems][balanced]. Retrieved 2026-08-29.
- W3C Web Accessibility Initiative, [Planning Audio and Video Media][w3c-media].
  Retrieved 2026-08-29.

[nist-rt]: https://www.nist.gov/itl/iad/mltg/rich-transcription-evaluation
[desplanques]: https://www.isca-archive.org/interspeech_2015/desplanques15_interspeech.html
[pyannote-models]: https://www.pyannote.ai/md/models
[active-correction]: https://www.isca-archive.org/iberspeech_2021/prokopalo21_iberspeech.html
[inception]: https://inception-project.github.io/releases/40.3/docs/user-guide.html
[elan]: https://www.ling.upenn.edu/~wlabov/L560/ELAN_introduction.pdf
[unitlab]: https://unitlab.ai/en/audio-annotation
[kalda]: https://www.isca-archive.org/odyssey_2024/kalda24_odyssey.pdf
[dscore]: https://github.com/nryant/dscore
[balanced]: https://arxiv.org/abs/2211.04304
[w3c-media]: https://www.w3.org/WAI/media/av/planning/
