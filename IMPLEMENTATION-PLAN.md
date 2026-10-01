# Agentic Harness Implementation Plan

Authorized by Martin on 2026-09-29. See docs/verification/progress.md for current completion status and evidence.

## Workflow Enforcement And Agent Autonomy

The harness enforces the selected workflow's required transitions and evidence. Agents organize the work within those boundaries and may use their CLI's native delegation capabilities. A custom agent-to-harness delegation transport is not a prerequisite for the first version.

- **Agent responsibility:** choose specialists, delegate, investigate, implement, correct findings, and arrange reviews using available capabilities.
- **Harness responsibility:** enforce admission, required workflow gates, independent-review evidence, usage limits, holds, and completion criteria. A successful launch or an agent saying done is not sufficient evidence.
- **Adapter responsibility:** configure and launch harness-invoked agents, observe results and telemetry, and expose supported controls. Native child calls follow the CLI's own configuration; the harness does not claim it can select a different CLI or reload configuration for each internal child.
- **Workflow definition:** for each required transition, specify the source and target stage, authorized actor, required evidence and candidate identity, and the outcome when evidence is absent or invalid. Reuse the selected methodology's rules instead of inventing a universal fixed delivery sequence.
- **Enforcement point:** validate before authorizing the transition through the existing provider operation boundary. Agent tools must not allow a protected transition to bypass that check. Reuse existing provider management operations where available; inventory alone is insufficient. Do not introduce a new provider framework without a demonstrated gap.
- **Review gate:** verify reviewer independence, the exact candidate reviewed, verdict, and required verification/delivery evidence. Native delegation is permitted; self-attestation alone does not establish independence or acceptance. Missing proof blocks that transition, not all unrelated work.
- **Capacity and usage:** distinguish harness invocation limits from native child concurrency. Configure CLI-native limits where supported and account for child usage without double counting. Report unsupported controls and incomplete evidence honestly; retain the existing usage guard and safe interruption boundaries.

The minimum complete slice runs one item through its required workflow, lets its agent arrange specialist work, and demonstrates that valid evidence permits the next transition while missing or invalid evidence prevents it. Dynamic delegation infrastructure and cross-CLI child routing are optional later capabilities, justified only by an actual workflow need.

## Project Boundary

Build the application in this sibling project, with its own eventual Git repository, dependencies, command-line interface, dashboard, tests, and release cycle. The dev-methodology project remains the source of reusable skills and agent definitions.

Consume an explicitly configured methodology installation or checkout. Do not require a hard-coded sibling path or duplicate the methodology sources.

The existing designs currently place implementation under dev-methodology/harness. Update that boundary during project setup and transfer the application designs with their creation provenance preserved. Copies of the current designs and their historical review evidence are included here. Source references in the main designs link back to the methodology checkout where needed. Historical review records retain their original paths and hashes. The local designs are:

- [Architecture](docs/architecture/ARC-002-codex-work-item-dispatch-harness.md)
- [High-level design](docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md)

Existing code under dev-methodology/adapters/codex/backlog-harness is a selective reuse source. Assess each component against the intended design; do not copy its workflow assumptions wholesale or introduce an operational-data migration phase.

## Required Outcomes

- A terminal-first agentic application with a read-only dashboard showing the overall process.
- Execution until all in-scope backlog items are terminal, with optional deterministic polling for later items.
- Provider-owned lifecycle and assignments, Coordinator-owned scheduling decisions, and Orchestrator-owned delivery.
- Configurable CLI bindings for every agent role behind a common adapter interface, with Codex as the first implementation.
- Applicable configuration reloaded immediately before each CLI invocation; in-flight settings remain immutable.
- CLI-specific OpenTelemetry preparation and standard OTLP JSONL trace files attributable to Work Items and invocations.
- Dashboard trace views derived from standard telemetry, joined with authoritative provider and invocation records.
- Usage warnings and an item-local hold pending review at the configured threshold, initially twice the immutable original high generated-implementation-token estimate.
- Recovery without duplicate dispatch; unknown telemetry is never treated as zero usage.
- No model calls for unchanged polling, status inspection, configuration validation, or dashboard refresh.

## Delivery Phases

| Phase | Deliverables | Completion evidence |
| --- | --- | --- |
| 1. Establish project boundary | Initialize the project repository; establish configuration, packaging, documentation, and test directories. Transfer application designs and update ownership and paths. Identify reusable code. | Project installs and exposes a minimal CLI; architecture and HLD agree on ownership and paths. |
| 2. Create module designs and test plans | Define interfaces, records, state transitions, failures, and acceptance cases for the module groups below. Resolve remaining Codex telemetry questions. | Reviewed contracts and a requirements-to-test matrix with no unresolved interface contradictions. |
| 3. Build the first complete execution path | Configuration reload, provider item, configured Codex adapter, invocation evidence, OTEL files, terminal, and dashboard projection. | One bounded real Work Item runs through the complete path with correct attribution. |
| 4. Implement sustained coordination | Coordinator decisions, Orchestrator sessions, delegated roles, capacity, answers, pause/resume, until-terminal execution, and optional watch mode. | Multiple items progress correctly; unchanged polling makes no model calls. |
| 5. Implement recovery and usage controls | Durable reconciliation, uncertain launch handling, exact-session resume, telemetry failure handling, and the original-estimate usage guard. | Fault scenarios recover without duplicate execution; a held item does not unnecessarily stop independent work. |
| 6. Complete acceptance and delivery | Independent review, populated terminal/dashboard checks, installation instructions, operator guide, and release packaging. | End-to-end acceptance passes against the installed application; material limitations are explicit. |

The phases express dependencies, not a requirement to finish every detailed design before writing any implementation. Establish shared contracts first. Then design, implement, and verify each cohesive slice before proceeding. Bring the first real end-to-end run forward so unsupported CLI or telemetry behavior is discovered early.

## Module-Level Designs And Unit-Test Plans

Create one design per cohesive responsibility, rather than one document per Python file. Each design specifies ownership, public interfaces, data structures, normal flow, failure behavior, and its unit-test plan before implementation begins.

| Design | Responsibilities | Focused test coverage |
| --- | --- | --- |
| Configuration and adapter contracts | Role bindings, configuration reload, immutable snapshots, adapter registry, capabilities | Invalid or changing configuration; unsupported adapters; edits between calls; session-binding preservation |
| CLI execution and Codex adapter | Preparation, launch, resume, results, interruption, child-role routing | Subprocess failures; uncertain submission; exact-session resume; independent reviewer context |
| Provider and coordination | Provider authority, admission, dependencies, capacity, Coordinator decision boundaries | Lifecycle transitions; dependency blocking; concurrency limits; empty queues and new-item discovery |
| Runtime evidence and recovery | Invocation records, durable writes, reconciliation, restart | Crashes around submission boundaries; partial writes; repeated observations; duplicate prevention |
| OpenTelemetry and usage | Exporter preparation, OTLP ingestion, JSONL storage, correlation, accounting, guard | Concurrent attribution; retries; missing spans; storage failures; cumulative/delta accounting; threshold crossing |
| Terminal and dashboard projections | Commands, questions, progress, shared snapshots, trace views | Command behavior; coherent counts and details; incremental updates; freshness; actionable errors |

Maintain a requirements-to-test matrix linking each required outcome to its design, implementation responsibility, unit or integration cases, end-to-end scenario, and actual verification evidence. Plan entries are not proof that a test passed.

## Test Strategy

### Unit Tests

Use deterministic tests without model calls for configuration, scheduling, records, correlation, accounting, and projections. Check behavior and failure boundaries rather than mirroring implementation details.

### Integration Tests

Exercise actual subprocess, HTTP, and filesystem boundaries with a controllable fake agent CLI. Reproduce failed launches, delayed output, uncertain outcomes, repeated exports, malformed payloads, interrupted writes, and crashes.

Use adapter conformance cases against Codex and the fake CLI to expose accidental runtime coupling. The fake CLI is a test fixture, not evidence that another production CLI is supported.

### End-To-End Tests

Exercise the installed terminal application, a real provider fixture, populated dashboard, and selected real Codex invocations. Keep paid runs bounded and run them at meaningful integration milestones. Deterministic test runs cover repeatable fault scenarios; real CLI runs establish native capability and complete integration.

| Scenario | Required evidence |
| --- | --- |
| Workflow gates | Valid evidence permits the selected transition; missing review, wrong candidate, stale evidence, or unauthorized actor prevents it. Native delegation does not require a custom harness transport. |
| Work Item delivery | An item completes through delivery and independent review, with provider-backed terminal outcome. |
| Concurrent work | Multiple items respect capacity and have isolated, correctly attributed telemetry. |
| Configuration reload | An edit affects the next invocation without changing an in-flight invocation or silently replacing session identity. |
| Questions and answers | An answer resumes the correct item and session. |
| Restart and recovery | Existing execution is reconciled without duplicate launch. |
| Usage guard | The configured 2× original-estimate threshold causes a warning and item-local hold pending review; independent safe work continues. |
| Missing telemetry | Missing or conflicting data stays explicitly unknown and fences the affected next generation boundary when usage cannot be established. |
| Run modes | Until-terminal settles correctly; watch discovers later items without idle model calls. |
| Consistent views | Terminal and dashboard show equal facts for the same projection revision. |
| Telemetry durability | Retry deduplication, resumed invocations, run-level spans, interrupted final lines, exporter failures, and incremental reads behave as designed. |

## Codex Telemetry Implementation Gate

Before declaring the Codex adapter ready, verify the selected CLI version's effective telemetry configuration, native span fields, usage coverage, retry behavior, and shutdown flush behavior.

The proposed starting point is native OTLP/HTTP JSON export to a receiver inside the foreground application. The receiver durably writes standard OTLP JSONL with trusted invocation and Work Item correlation. It remains available when the dashboard is disabled and is not a separate daemon.

Do not assume Codex writes JSONL trace files directly or accepts external parent-trace context. Preserve native trace relationships and distinguish application-added correlation attributes from native telemetry.

## Completion And Progress Rules

- Run the smallest relevant checks after each change; broaden verification when a real dependency or failure justifies it.
- Keep paid capability checks bounded and reuse valid capability evidence instead of repeatedly probing.
- Review source changes independently and validate the complete installed workflow before declaring the application ready.
- Record completed phases only with supporting evidence. Passing isolated tests does not establish end-to-end completion.
- Martin explicitly authorized execution of this plan on 2026-09-29. Completion still requires the evidence above.
