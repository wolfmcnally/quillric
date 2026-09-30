# Phase 2: Retry requests the provider refuses with HTTP 429

Status: implemented 2026-09-27; acceptance met (101 tests; the retry removed, both new tests fail). Committed during private development; retained as historical implementation evidence.

## Outcome

A consumer converting many recordings at once no longer loses a recording, or pays twice for a completed first pass, when ElevenLabs refuses a request because a rate or concurrency limit was reached. The client sends the refused request again after a bounded exponential backoff, as the provider's error guidance asks; everything else fails exactly as before.

Evidence (2026-09-27): a consumer running sixteen conversions at once met "HTTP 429: Too many concurrent requests" on an account limited to 20 simultaneous requests. ElevenLabs documents that a recording longer than eight minutes is transcribed in up to four parallel pieces (concurrency = min(4, ceil(seconds / 480)) for Scribe v2); a probe reading the documented `current-concurrent-requests` header showed a 32-minute recording alone at 3 and a 64-second one at 1, so the pieces count against the account's limit. The provider's error documentation names two 429 codes (`rate_limit_exceeded`, `concurrent_limit_exceeded`) and asks for exponential backoff; it does not say whether a refused request is billed.

## Work

1. `ElevenLabsClient` retries a request answered with HTTP 429 after 15, 30, 60 and 120 seconds (`RETRY_DELAYS_SECONDS`; the delays and the sleep are injectable), sending the whole request again. It counts the retries it made (`retries`). Any other status, or a fourth refusal, raises `TranscriptionError` as before.
2. The version becomes 1.4.0; the README's library section states the rule and the provider's per-recording concurrency.

## Acceptance

- Two refusals then success return the transcript after the two waits, the same request sent each time; a refusal that outlasts the waits raises the provider's message; a 401 is not retried.
- Removing the retry fails both tests. The whole suite passes.
