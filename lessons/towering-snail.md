---
slug: towering-snail
title: pytest 9.2.0 breaks xdist_group serialization under pytest-xdist 3.8.0
status: candidate
scope: methodology
proposed_surface: policy
filed: 2026-11-20
source: teach
occurrences:
  - date: 2026-11-20
    ref: "Starter 1.9.0 retrofit TAUGHT FROM TEMPLATE entry"
---

A recipient whose lock resolved pytest 9.2.0 failed `tests/test_test_governance.py::test_parallel_pytest_keeps_coverage_isolation_and_failure_truth`: two cases marked with one `xdist_group` ran on different workers. The same fixture passes under pytest 9.0.3, 9.1.0 and 9.1.1 and fails under 9.2.0, all with pytest-xdist 3.8.0, outside any repository configuration. Quillric constrains `pytest>=8.0,<9.2` in its dev group. The template's language profile and dependency floor should name a tested pytest ceiling, or the proof's failure text should point at the scheduler version, because a fresh lock silently picks the newest pytest and the shared-resource guarantee is then lost.
