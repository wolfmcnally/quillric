# Agent instructions

These instructions apply to coding agents working in Quillric. `AGENTS.md` directs agents to this canonical file so Claude Code and Codex use the same guidance.

## Commit and push authority

As of 2026-11-10, the operator authorizes ordinary commits and non-force, fast-forward pushes for authorized work in this repository without asking for separate commit or push approval. Complete and deliver attributable work when its required checks pass; do not park routine delivery merely to request permission already granted.

- Stay within the approved task. Preserve explicit scope-specific publication, deployment, or delivery holds; this authority does not clear them or expand the task.
- Coordinate with an existing active owner before changing shared files. Re-read the complete working-tree status and diff before staging. Stage only explicit task-owned paths, never `git add -A` or `git add .`; preserve unrelated and operator-owned changes, including local `.codex/` configuration.
- Inspect the staged diff and verify the resulting commit contains only the intended changes. Use a factual commit message without agent credit. Keep all hooks and safeguards enabled; never use `--no-verify`.
- Push only to the branch's existing, unambiguous configured upstream, after verifying the remote state and fast-forward relationship. Do not create, select, or alter remotes or upstreams under this authority. Missing or ambiguous upstreams, divergence, rejected pushes, unresolved hook failures, uncertain ownership, unexpected changes, and platform access denials are blockers to report, not restrictions to bypass.
- After a push, verify the actual remote commit and the exact commit's hosted checks. Distinguish pushed, still validating, failed, and fully qualified results. A local commit or successful push is not release publication, deployment, or human acceptance.
- This authority does not cover force-pushes, history rewrites, destructive Git operations, tags, releases, deployment, new credentials, expanded permissions, or unrelated work. Those retain their applicable authorization requirements.

## Validation and data boundaries

Read the current `README.md`, `pyproject.toml`, and `.github/workflows/ci.yml` before implementation. Run the checks the change could break before committing and pushing, and report any blocked or unperformed checks accurately. For runtime changes, use the complete offline suite through `python .github/scripts/test.py` in a supported Python environment with the documented dependencies; skips fail qualification. Package changes also require the build, archive validation, and installed-wheel checks specified by CI. Documentation-only changes require content, link, and diff checks; they do not by themselves require paid transcription or a release.

Never commit credentials, private recordings, transcript packages, or local provider outputs. Tests use synthetic audio and fake or local HTTP clients, without provider credentials or paid calls. Keep the README's privacy boundaries and release qualification requirements intact. Do not load `.env` or send recordings to providers merely to validate an instruction change.
