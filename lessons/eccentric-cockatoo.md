---
slug: eccentric-cockatoo
title: Outer test clocks on commands that answer must exceed the commands' own clocks
status: candidate
scope: methodology
proposed_surface: test
filed: 2026-11-20
source: teach
occurrences:
  - date: 2026-11-20
    ref: "Starter 1.9.0 retrofit: first canonical full gate (CHECK test FAIL, 3 errors)"
---

`tests/test_kickoff_evidence.py` wraps every `kickoff-evidence` call in a 20-second `subprocess.run` timeout and launches `kickoff-config watch` with a 15-second one, although that watch carries its own 30-second first-event and 60-second hard clocks. On a shared host at load average about 20, with another session's parallel suite running, the first canonical full gate of this retrofit failed with three errors from those outer clocks; the same proofs had passed twice on the identical tree at lower load. Quillric gives those four call sites a 120-second clock, as the 1.5.1 generous-clock rule already does for stand-ins that answer, and the file then passed under the same load. The template should apply that rule to these helpers at its source, keeping short clocks only for proofs that a timeout fires.
