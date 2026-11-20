---
slug: onyx-coati
title: A stale-index proof must not depend on the recipient having accepted phases
status: candidate
scope: methodology
proposed_surface: test
filed: 2026-11-20
source: teach
occurrences:
  - date: 2026-11-20
    ref: "Starter 1.9.0 retrofit TAUGHT FROM TEMPLATE entry"
---

Starter 1.8.0's `tests/test_execution_dashboard.py::test_stale_dashboard_refuses_until_regenerated_from_tracked_inputs` copies the recipient's own `EXECUTION_LOG.jsonl` and archive. A recipient with no accepted phase has neither a trace nor a phase archive, and the checker's zero-phase branch only parses the index, so the proof fails in every fresh stamp or retrofit even though the checker is correct. Quillric's copy renders one synthetic accepted phase into the mirror when the archive is empty, the way the sibling regeneration proof builds its archive, and an observed-red receipt shows the adapted proof still catches a checker that accepts a stale index. The donor test should build its own phase rather than read the recipient's history.
