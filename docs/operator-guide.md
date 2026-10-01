# Operator Guide

## Configuration And Installation

Use an explicit absolute repository, candidate workspace, methodology installation, and evidence root. The application never imports arbitrary configured adapters. Configuration reload happens before each harness-issued CLI invocation. The complete snapshot and resolved dependency digests govern that invocation. Accepted item scopes, checks and candidate storage remain frozen; incompatible edits fence the next invocation. The effective Codex home is recorded in the session binding and applied to both capability validation and launch; changing it cannot silently resume another authentication context. Changes to the originating CLI, authentication context, or profile cannot silently replace a retained native session.

Build with `uv build`. Install the wheel using `uv tool install /absolute/path/to/agentic_harness-0.1.0a2-py3-none-any.whl`, or use `uv sync` and `uv run` from the source checkout. `agentic-harness --version` identifies the package. Codex authentication stays in its own CLI context; authenticate there with `codex login`. A named alternate context uses `agent_clis.NAME.adapter_options.codex_home`; the harness stores its reference and digests, never credentials.

The fixture generator is a concrete configuration example:

```sh
uv run python scripts/acceptance_fixtures.py \
  --root /absolute/path/isolated-acceptance \
  --methodology-root /absolute/path/dev-methodology
```

It creates separate SOLO and MULTITASK provider repositories, each with three dummy items and unchanged authoritative tests. It refuses to replace an existing fixture. Creating fixtures makes no model call. Run their generated config through `validate`, then `run --until-terminal` or `app`.

A setup example is [dev-methodology-observation.yaml](examples/dev-methodology-observation.yaml). It requires the project setup owner to create the named independent workspace and storage directories. Its example scope/checks are for schema validation only and must be replaced with the selected item’s accepted requirements before dispatch.

## Workflow And Authority

The dev-methodology integration selects `provider_interaction: agent`. It is under verification; historical release acceptance covers the earlier direct fixture route. `validate` checks configuration without a model call. `refresh-provider` performs a bounded, read-only Coordinator observation when the provider source changes and otherwise returns the cached observation. Neither command dispatches a Work Item. Dashboard and status use the cache and report stale observations rather than launching agents.

The configured Coordinator and Orchestrator must be able to read the applicable provider-management skills from the methodology installation. They may load them by reference; literal profile duplication is unnecessary. Management invocations execute in the authoritative repository; implementation invocations stay in the separate candidate workspace. A writable management operation additionally requires the configured role's `workspace-write` permission and the recorded actor, revision, transition, prompt, and exact provider paths. Agents use their configured claim helper. The harness does not acquire claims. A separate executor never becomes the canonical owner. Management usage is retained separately and checked against `administrative_review_limits`.

The default is `provider_interaction: agent`. Explicit `provider_interaction: direct` preserves the earlier fixture route. When replaying an older fixture configuration, add that explicit selection; its retained evidence is not migrated to the agent route. The byte-oriented `recover-provider` command below belongs to that route; it must not be used to adopt or replace an existing desktop task. Agent-mediated operation recovery reuses the recorded operation and invocation evidence. A desktop task ID alone is not a CLI resume identity.

The Coordinator requests admission against the current provider revision. Ready becomes Starting before a canonical Orchestrator starts. That Orchestrator accepts ownership while read-only; the provider records Running for its portable session before writable source work. The agent may use native specialists and arrange a native independent review.

A completion request must identify the exact candidate. The harness independently reads native producer and reviewer records, verifies fresh context and distinct identity, verifies the candidate and passing checks, merges it into the configured primary branch, runs integrated checks, and closes and archives the provider record in a separate exact-path Git commit. Neither a successful launch nor a report of done permits closure.

Provider and delivery effects have durable requested records. If a commit completed but its receipt was lost, reconciliation proves its unique parent, paths and bytes and rebuilds the receipt. A merge is recovered by its exact parents. Unproven mutations stay unresolved. For a request that is proven uncommitted, `recover-provider OPERATION_ID` explicitly finishes the recorded bytes under the provider transaction lock. It requires the original main HEAD, exact original/intended checkout and index bytes, the recorded actor gate, and no unrelated changes. A committed operation recovers its receipt without another commit. The application does not reset a checkout or repeat an unknown paid launch.

## Modes And Capacity

SOLO permits one executing Work Item. MULTITASK requires the provider's explicit concurrent setting. Each item uses an independent clone under `candidate_root`. Admission conservatively blocks overlapping declared paths, and actual candidate diffs and current-main compatibility are checked again during integration.

`max_active_invocations` controls harness-issued invocations. Durable slots retain uncertain executions after process-lock loss. A lowered cap includes existing higher-numbered slots. `native_max_threads` is a Codex-native request governing native child work; those children remain internal to the CLI. The harness verifies independent review and reconciles their generated tokens with native telemetry before permitting subsequent advancement. It does not select another CLI or reload a profile for each internal child.

## Usage And Holds

The admitted item's original high generated-token estimate is frozen. The default ceiling is twice that estimate. Root cumulative counts, resumed calls, per-request OTLP usage, and observed native child counts are reconciled without adding output and reasoning tokens twice. Missing, partial or conflicting data stays unknown.

At a safe boundary, an affected item is held before further generation when usage is unknown or crosses the ceiling. Its canonical owner and session are retained. A bounded Coordinator review can release the hold only with trustworthy usage below an authorized cumulative ceiling. A higher ceiling needs an operator approval reference. An allowance never bypasses unknown usage. `review-hold ITEM_ID CEILING APPROVAL_REFERENCE` is the interactive command; the noninteractive command accepts `--ceiling` and `--reference`.

Coordinator and administrative call budgets are verified from observed CLI turns and generated output before their decisions can authorize advancement. A single opaque native CLI call may overshoot a requested budget; the harness gates subsequent effects and records that fact. Native internal tool cycles are managed by the CLI.

## Questions And Run Controls

`item show ITEM_ID` displays the exact provider record and usage. `item answer ITEM_ID QUESTION_ID` presents the current exact question, captures the provider revision, and prompts for text. The answer is durably persisted before the canonical session classifies it. Only `approve` permits continuation; defer, decline and ambiguous retain the question. An answer to a stale revision is rejected. After a crash, `resume-answer ITEM_ID` resumes the unique persisted answer operation with its original revision and canonical session. Scheduling fences the item while that operation is incomplete; an approval cannot be lost between its provider commit and continuation record.

Pause closes admission while accepted work continues. Resume reconciles before reopening the retained run intent. Stop closes admission, cancels owned requests through their adapter, and reports proven stopped processes separately from unresolved workflow evidence. Quit cannot substitute for stop. Restart preserves pending identities and keeps admission closed until an explicit run or resume request.

Watch polls deterministically and calls no model for unchanged scope. Status, validation, dashboard refresh, and evidence reconciliation make no model calls. Until-terminal success requires every in-scope item to be Completed and no unresolved effect. A terminal scope containing Failed, Abandoned or other nondelivery outcomes reports settlement without success.

## Telemetry And Inspection

The loopback OTLP/HTTP JSON receiver lives inside the foreground application, independently of the dashboard. It attributes standard native spans to the provider, item and invocation using a private invocation token. It preserves native trace relationships, deduplicates retries, rejects conflicting correlation, and fsyncs before acknowledging storage. A transient storage failure remains an unresolved export until that exact batch is durably received; an overlapping partial batch cannot clear it. New receipts bind the actual span contents, so an unchanged count cannot hide changed evidence.

The dashboard and terminal use the same projection of provider records, invocation evidence, usage and traces. Missing observations are unknown; stale runtime data does not prove a process stopped. Raw invocation and native telemetry evidence stays under the configured operational root. Keep that directory when recovering a run. Status, dashboard, terminal controls and reconciliation resolve current provider/evidence storage even when a generation profile is missing or invalid. They report the configuration error. Every new CLI invocation still requires complete current validation; control inspection never authorizes generation from stale configuration. An invalid generation edit pauses admission without cancelling a running invocation.

If an item is blocked, inspect its provider record, `scheduling-blocks.json`, invocation outcomes, native session evidence and provider/delivery requested records. Correct the actual missing evidence or authorization. Do not delete an uncertain operation directory to force another launch.

## Tested Native Export Failure Boundary

On Codex 0.159.2, no retry was observed in the tested first-export 503 scenario. The first 512 spans were lost while 1200 later spans and all 11 measured output tokens arrived. Matching usage and later spans did not restore the rejected batch. The generation gate rejected the result. Shutdown establishes observed drain/process exit with stable retained spans; it does not prove complete flush while an export is missing. See [native exporter evidence](verification/native-exporter-evidence.json).

The receiver does not promise lossless export or introduce a separate durable spool. If the original validated batch is available, re-ingest those exact bytes through the existing authenticated receiver/correlation boundary and verify persistence before clearing its incident. If it was not retained, keep the gap and affected transition held. The operator must use the selected provider management workflow for an explicit nondelivery disposition; a larger allowance, a fabricated span or another paid invocation cannot substitute for the missing evidence. Inspection, other safe items and operator controls remain available.

The dashboard caches complete trace records by file identity and byte offset, reads appended records once, exposes a partial tail as uncertainty, and orders the displayed recent spans by native timestamp. Counts and item details use the same captured provider revisions; a changed provider snapshot is rejected for refresh. Display configuration reload does not replace an executing invocation’s snapshot. Legacy receipts from the earlier completed acceptance remain historical display evidence; they cannot authorize new advancement without the current content binding.
