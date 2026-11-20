---
id: review-critical-proofs
title: Review the critical-risk direct proofs
order: '1'
parent: null
depends_on: []
informs: []
---

## Goal

Give Quillric's proof estate its first reviewed evidence. The ten applicable critical risks in `tests/proof-estate.yaml` name nine distinct direct proofs, chosen locally at the Starter 1.9.0 retrofit and still pending review with the rest of the 382-proof baseline. The owner approved this phase on 2026-11-20, registered prospectively; the approval named the same nine proofs and counted six product checks, and the correct count is eight product proofs and one governance proof.

## Deliverables

- A recorded disposition for each of these nine proofs, with contract, oracle, nearest overlap and rationale:
  - `tests/test_multioperator.py::test_transition_and_maintenance_refuse_false_state` (custody, authority)
  - `tests/test_security.py::ExplicitCredentialTests::test_concurrent_calls_keep_clients_and_credentials_separate` (concurrency)
  - `tests/test_transcribe_cli.py::PipelineTests::test_force_keeps_existing_package_when_replacement_fails` (atomicity)
  - `tests/test_package.py::PackageTests::test_altered_evidence_is_refused` (corruption)
  - `tests/test_transcribe_cli.py::PipelineTests::test_api_failure_removes_partial_package` (recovery)
  - `tests/test_security.py::JsonApiSecurityTests::test_real_opener_redirect_chain_never_restores_credentials` (security)
  - `tests/test_package.py::PackageTests::test_package_records_source_identity_and_is_loadable` (public contract)
  - `tests/test_config.py::ConfigurationTests::test_invalid_schema_rejects_types_ranges_unknown_fields_and_authority` (schema)
  - `tests/test_transcribe_cli.py::PipelineTests::test_pipeline_creates_complete_default_package_atomically` (core success)
- Observed-red witness receipts for the product proofs among them.
- No product code change.

## Acceptance

`./bin/test-governance validate` is valid with the new dispositions and receipts; the changed-path selection passes; one bare `./bin/check all` qualifies delivery.

## Brief refs

- [Proof-estate design](../../briefs/test-suite-value-governance.md)

## User Demo

None; this phase records test evidence and changes no behavior.
