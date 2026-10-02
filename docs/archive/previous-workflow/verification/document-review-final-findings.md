# Frozen documentation review

Reviewer: Northstar. Date: 2026-09-30.

Verdict: **NEEDS_CORRECTION**. Stage: **PASS_1_MATERIAL_DEFECTS**.

## Scope and identity

The candidate is `.agent-ops/document-review-candidate.json`, SHA-256 `cda0fd4d7168a8491cad3e21c7d2bd18aeca6ba83ad0312d47e1303cdaeb5e1d`. All 16 files match the manifest. The review read the plan, README, operator guide, ARC-002, HLD-003, six module designs, requirements matrix, and four bound JSON receipts.

The accepted implementation is commit `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`, tree `1781fab046c4066e52a96d5966d18e726651547b`. Every source-file hash in `docs/verification/release-acceptance.json` still matches the checkout. Source acceptance is retained from `docs/verification/final-source-review.json`; this document does not perform or claim another source review.

This is a substantial two-pass review. Essential concerns were inspected across the complete candidate before stopping. The findings below concern documentation. They do not require another paid run, a new delegation framework, or source changes.

## Findings

### FIND-1 — Admission diagrams continue into execution after a no-launch branch

Severity: high. Category: contract and diagram correctness.

Locations: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:447` and `docs/architecture/ARC-002-codex-work-item-dispatch-harness.md:389`.

Exact HLD evidence, lines 464–477:

```text
        else assess or invalid result
            A-->>A: do not reserve or launch Orchestrator
        end
    else existing Starting
        A->>A: validate frozen assignment and retained admission
        A->>P: reconcile exact provider operation and admitted Git blob
    else existing Running
        A->>A: validate retained acceptance and canonical owner
    else unchanged observation
        A->>A: zero model calls
    end
    A->>O: accept Starting or resume exact Running session
    O-->>A: accepted item and exact session
    A->>P: record or verify Running canonical owner
```

The execution messages are outside the conditional fragment. Both `assess or invalid result` and `unchanged observation` therefore rejoin a path that invokes the Orchestrator. The ARC diagram has the same problem: its unchanged-observation branch rejoins unconditional retention of admission and Orchestrator dispatch. It also places `new or assess` before an unconditional Ready-to-Starting request inside the Ready branch.

Source evidence: `src/backlog_harness/application.py:979` requires `operation == "new"`, the exact item, and the exact revision before provider transition. Its acceptance path at line 1008 requires Starting and retained admission. `src/backlog_harness/coordination.py:109` excludes unchanged blocked premises before dispatch.

Correction: Put dispatch and provider acceptance inside an explicit successful-admission or validated-continuation branch. End assess, invalid, and unchanged paths without execution. Keep the no-call path visually separate through the end of each diagram.

Authority: the accepted source, the plan's no-call observation and actor gates, and architecture/HLD diagram consistency requirements. Impact: the primary diagrams currently contradict the paid-execution gate they are meant to explain.

### FIND-2 — The HLD assigns observation and reconciliation calls to an unused adapter path

Severity: medium. Category: cross-module contract.

Locations: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:413` and line 964.

Exact evidence:

> application.py calls `validate_profile`, `start_session` or `resume_session`, `observe_events`, and `reconcile` through `AgentCliAdapter`.

`Application._invoke` in `src/backlog_harness/application.py:268` validates the profile and awaits start or resume. It then consumes `handle.events` directly after the invocation returns. It does not call `observe_events`. `Application.reconcile` at line 839 calls `EvidenceStore.reconcile` and application recovery logic, not `adapter.reconcile`. A search of the complete application module confirms the two claimed adapter call sites are absent.

Correction: Distinguish the Protocol's available operations from the active call path, as the document already does for `request_interrupt`. Describe result consumption from the returned handle and direct retained-evidence reconciliation. Reconcile the per-invocation steps, cross-module section, and related component descriptions with this path.

Authority: current source is authoritative for the selected EXISTING_IMPLEMENTATION mode. Impact: the HLD currently gives implementers the wrong producer-consumer and recovery integration seams. No Protocol or source modification is requested.

### FIND-3 — Event-processing prose reverses a durable session boundary

Severity: medium. Category: effect ordering.

Location: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:543`.

Exact evidence:

```text
3. Append the event before returning it to the application.
4. On `thread.started`, validate the native UUID, preserve exact resume identity, and persist the portable/native session binding.
```

These are numbered processing steps. In `src/backlog_harness/adapters/codex/adapter.py:219`, the adapter first validates native identity and writes `session.json` for `thread.started`. Only after constructing the normalized event does it call `writer.append(event)` at line 259. The same ordering places selected type-specific fields in the event before append.

Correction: State normalization and session validation/persistence before event construction and append. State separately that the application consumes the returned handle after process completion. Add the required event-processing diagram with the same durable ordering.

Authority: the source and the module/HLD requirements for consistent asynchronous phases and crash boundaries. Impact: the current numbered sequence misstates which evidence can exist after interruption between the two writes.

### FIND-4 — Required effect diagrams leave material sequences only in prose or phase tables

Severity: medium. Category: response adequacy.

The essential diagram inventory found these omissions:

| Location | Unrepresented relationship | Direct correction |
| --- | --- | --- |
| HLD-003, Startup And Capability Validation, line 395 | Startup configuration-path resolution, scope read, reconciliation, initial projection, and validation are given as an ordered sequence without a matching startup diagram. The admission and per-invocation diagrams cover different boundaries. | Add a source-backed startup flow, or explicitly consolidate this sequence into a matching existing diagram. |
| HLD-003, Event Processing, line 543 | The six ordered event and persistence actions have no event-processing diagram. | Add the corrected sequence from FIND-3. |
| MOD-003, External And Asynchronous Effect Phases, line 163, and Processing Rules, line 183 | Provider prepare/commit and candidate validation/merge/integrated-check/READY/provider-completion phases are reduced to a single Running-to-Completed state edge. The context diagram is structural. Neither diagram shows these ordered effects and their separate failure boundaries. | Retain the state diagram and add a compact effect sequence with the application, delivery, and provider owners. |
| MOD-004, External And Asynchronous Effect Phases, line 159 | Prepared-provider versus committed-provider or merge recovery, proof checks, receipt reconstruction, and ambiguity fencing have no corresponding effect/recovery diagram. The present diagrams describe invocation records and outcomes. | Add a bounded recovery flow that distinguishes proof of an existing effect from explicitly authorized completion of a prepared provider effect. |
| MOD-005, Processing Rules, lines 171–172 | Usage reconciliation, ceiling comparison, and provider Holding are absent from the processing diagram, which ends with a telemetry-gap fence. | Extend the diagram or add a usage/hold diagram within this module. Preserve unknown usage versus a measured ceiling crossing. |

Authority: `review-high-level-design` requires diagrams for ordered interactions; `review-module-design` requires them for qualifying Processing Rules and external/asynchronous phases. An existing diagram for another relationship does not express these effects. Impact: material persistence, delivery, and hold boundaries remain hidden in prose despite the required diagram contract. The correction is bounded documentation work, not added runtime orchestration.

### FIND-5 — Release evidence has conflicting current-versus-historical claims

Severity: medium. Category: evidence attribution and readiness.

Locations and exact evidence:

- ARC-002 Verification, line 670: `Wheel --> Workflow[Representative real workflow]`.
- MOD-001 Open Questions, line 45: `Release acceptance still requires independent review and an installed rebuild of the current source.`
- MOD-001 Verification, line 222: `Final verification must rerun the complete suite, lint, independent source review, wheel build, and installed workflow.`
- MOD-005 Implementation Readiness, line 229: `Release readiness still requires independent current-source review, rebuild, and installed acceptance.`
- MOD-006 Open Questions, line 45: `Final installed acceptance must be repeated after the current source is independently reviewed and rebuilt.`

The frozen `release-acceptance.json` already records accepted current source, 78 tests, the matching rebuilt wheel, installed controls, and retained replays. Its native six deliveries belong to earlier source; current installed replay created zero new invocation IDs. The current ARC prose and requirements matrix describe that distinction correctly, but the ARC diagram draws a current-wheel-to-real-workflow proof path and the module statements send readers back to completed gates.

Correction: Draw two evidence inputs: historical native deliveries with their original manifest, and current-source checks/build/installed controls/replays. Join those inputs at the release receipt without depicting new native delivery from the current wheel. Replace stale module gate statements with the precise remaining independent documentation review gate and link the current receipt. Keep historical test counts explicitly historical.

Authority: retained release and source-review receipts, the assignment's accepted release route, and the plan's evidence-backed completion rules. Impact: readers cannot reliably determine which implementation each proof covers or which release work remains. Do not rerun completed checks or paid deliveries to make stale prose true.

### FIND-6 — The module conflates the projection payload with interactive status output

Severity: medium. Category: observable interface contract.

Location: `docs/design/components/MOD-006-views.md:224`.

Exact evidence:

> `status` includes revision, time, provider snapshot, recorded run and admission state, current blockers, counts, items, invocation identities, freshness, trace count, receipt binding, uncertainty, and generation configuration error.

`src/backlog_harness/terminal.py:66` selects 11 fields for the interactive `status` result. Those fields exclude `provider_snapshot` and `invocations`. The full projection includes them; interactive session inspection uses a separate command. The release receipt's terminal/dashboard parity field list also correctly records the smaller 11-field result.

Correction: Describe full projection fields separately from interactive `status`. List or reference the actual status subset, and identify session inspection for invocation/session evidence. If the statement intends noninteractive CLI status, label that operation explicitly instead of making one statement for both commands.

Authority: the exact terminal command and retained parity evidence. Impact: an operator or integration author is promised fields that this specific interface does not return.

## Essential correctness evidence

| Concern | Evidence and assessment |
| --- | --- |
| Problem fit and non-goals | The plan, README, ARC/HLD, and operator guide consistently select a foreground file/main harness, native agent organization, evidence gates, and no custom delegation framework. No new infrastructure is required by this review. |
| Admission and frozen identity | HLD admission prose, MOD-003, MOD-004, source gates, and final source review correctly preserve provider revision/content, original estimate, retained admission, and canonical acceptance. FIND-1 is a diagram contradiction, not an implementation rejection. |
| Delivery and answer ownership | The current HLD delivery and question diagrams correctly assign integration and provider completion to the application, and separate read-only answer classification from later source work. Prior findings about these two HLD diagrams are corrected. |
| Runtime interfaces and ordering | Exact configuration snapshots, CLI preflight, binding checks, permissions, submission records, and process outcomes are documented. FIND-2 and FIND-3 remain in the HLD's active runtime-path explanation. |
| Identity and authentication | ARC now binds the configured profile and resolved Codex-home path, and explicitly disclaims stable account-principal detection within that home. Native child independence remains evidence-based. No principal guarantee is demanded. |
| Telemetry and usage | Source-backed module contracts and receipts distinguish native output from root/child counts, preserve rejected-export fingerprints, require content-bound receipts, and retain item-local holds. The tested missing 512-span batch remains an accepted fail-closed limitation. No new paid probe is required. |
| Recovery | Intent/request/result separation, exact session recovery, retained Starting/Running proof, and no duplicate launch remain consistent in core prose and source evidence. The recovery diagram coverage gap is recorded in FIND-4. |
| Views and controls | Provider authority, current control-only configuration, recorded-state versus liveness, trace-content binding, and legacy count-only display are explicit. FIND-6 narrows one operation-specific response claim. |
| Verification provenance | The frozen receipts distinguish six earlier real deliveries, the current 78-test source, installed matching bytes, and unchanged SOLO/MULTITASK invocation sets of 9/12. FIND-5 identifies inconsistent diagram/status summaries. |
| Module response-adequacy gate | All six module documents have the exact 28 level-two headings in the current module template, in order. Each authored acceptance/readiness decision starts with ACCEPTED/READY. Template SHA-256: `9d0779b1edd96665ec27196cf572582eb8c97fd57c3e1262fca4705a9d868fc9`. |

## Diagram inventory

All 27 candidate Mermaid blocks were read for essential meaning. These locations identify the inspected blocks; this is not a syntax-validation receipt or blanket approval of their containing documents.

| Document | Mermaid opening lines | Essential disposition |
| --- | --- | --- |
| ARC-002 | 156, 295, 389, 510, 529, 665 | Scope, layers, guard, and run-state diagrams were inspected; dispatch and evidence provenance findings are recorded above. |
| HLD-003 | 246, 415, 447, 517, 558, 721, 800, 1020, 1206 | Parent context, invocation, admission, delivery, question, run-state, guard, telemetry, and implementation-order diagrams were inspected; admission and missing effect views are recorded above. |
| MOD-001 | 94, 175 | Configuration context and validation/control branching inspected. |
| MOD-002 | 100, 191 | CLI context and process-outcome versus telemetry-adoption sequence inspected. |
| MOD-003 | 96, 196 | Context and provider states inspected; ordered effect coverage remains incomplete. |
| MOD-004 | 102, 186 | Evidence context and invocation states inspected; provider/delivery recovery coverage remains incomplete. |
| MOD-005 | 96, 176 | Telemetry context and export durability inspected; usage-to-hold coverage remains incomplete. |
| MOD-006 | 97, 179 | Projection context and coherent capture sequence inspected. |

## Review protocol and limits

The required core skills, architecture/HLD/module review skills, terminology review, and applicable interface-pattern guidance were loaded. The selected checklist sources are the generic structured checklist and architecture, HLD, module, and interface-pattern supplements. Their content hashes are:

| Checklist | SHA-256 |
| --- | --- |
| Structured artifact | `510b081be79b1d24373e4164470fa06c9e971f34f4a237fcb25ab9a73fb4b681` |
| Architecture | `96648df3aa495598065accf0549dbb3d36e6eab40789f724cad15d01aed40ed0` |
| HLD | `5c5d719b0caf802f7f9ab07668218944c89b09bd391b7ab4f2d5fc27bc908a26` |
| Module | `127f881322f10b3b5688a4761df0e7f2dc1784a395d81d6c53cd54a5ce4bd812` |
| Interface patterns | `b599c265400305f0c9f8bd643b7834c0bc01d5923a1aac1d4d1137b97083f412` |

Terminology loading returned `reference_not_found` as its only error. Under the terminology skill this means TERMINOLOGY STANDARDS LOADED, ABSENT, for configured snapshot `d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658`. It does not prove absence outside that snapshot. No terminology-conformance verdict beyond that scope is claimed.

No local ancestor AGENTS.md or project-root PROJECT.yaml was present. The supplied identity instruction applies. The HLD's linked methodology AGENTS.md was read as an authority reference; its sibling-repository mutation rules do not authorize changes there. This review changed only this new findings file.

The structured-artifact skill requires stopping before exhaustive checklist and sentence work after a material essential defect. Accordingly, this report is provisional findings with essential evidence. It is not a completed checklist, page-verification result, sentence manifest, or mechanical validation receipt. No GOOD verdict is granted. Source, candidate files, tests, old receipts, and old reviews remain unchanged. No tests, model/native calls, commits, or external publication were performed.

Next owner action: correct the bounded documentation issues above, freeze the resulting candidate, and obtain a fresh independent essential pass followed by complete checklist, page, sentence, and mechanical evidence. Retain current source acceptance and historical native evidence with their existing scope.
