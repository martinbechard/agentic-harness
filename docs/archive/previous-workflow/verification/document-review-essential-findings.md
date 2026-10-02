# Independent documentation review: frozen release candidate

## Result

NEEDS_CORRECTION. Stage: PASS_1_MATERIAL_DEFECTS. Reviewer: Northstar. Review date: 2026-09-30.

The essential correctness gate found material contradictions in delivery ownership, adapter outcome timing, and skill delivery. Correct the documentation against the accepted source. No source change, new native run, or repeated test run is required by these findings.

This is a provisional essential-stage findings artifact, not a completed checklist or acceptance. The review-structured-artifact procedure requires stopping on a material essential defect before exhaustive checklist and sentence work. Page verification, complete supplemental evidence, the sentence manifest, and mechanical acceptance validation therefore remain unperformed. Historical reviews remain historical and are not approvals of this candidate.

## Scope and exact binding

The frozen candidate manifest is `.agent-ops/document-review-candidate.json`, SHA-256 `d0cda56b8e840d9544811d730275ba22246b88cb0dd253cb0a594fa0baedbb8e`. Its 16 file hashes were independently compared with current bytes and all matched. All source-file hashes in `docs/verification/release-acceptance.json` also matched current bytes. Source commit: `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`.

The applicable guidance is review-structured-artifact, review-architecture, review-high-level-design, review-module-design, verify-documentation-page, ste-technical-writing, and terminology-standard-review. Generic and architecture/HLD/module checklists were loaded. Module level-two headings match the supplied module template. The review evidence path differs from the default beside-target location under the explicit assignment.

Terminology load returned ABSENT in configured snapshot `d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658`, with no sources. This is not evidence about unlisted host scopes and is not a completed terminology review.

## Findings

### FIND-1: Architecture assigns provider closure and delivery to the wrong execution owner

- Severity: high.
- Target: `docs/architecture/ARC-002-codex-work-item-dispatch-harness.md:430`, Dispatch And Delivery diagram.
- Evidence type: exact quotation.
- Candidate evidence: `O->>O: complete verification and Commit delivery` and `O->>P: record provider closure through selected skill`.
- Source evidence: `src/backlog_harness/application.py:1094` instructs the Orchestrator not to claim completion or modify the authoritative provider. At lines 1154–1176, the application runs source checks, validates the candidate, calls `integrate`, and invokes `self.provider.transition(..., "Completed", ...)` with canonical actor evidence. `docs/design/components/MOD-003-provider-coordination.md`, Public Contracts and effect phases, correctly separates canonical actor authorization from harness execution.
- Assessment: The architecture diagram gives the agent a direct provider mutation path and assigns final delivery to it. That contradicts both the actual enforcement boundary and the child module contract. Actor authority and execution ownership are distinct.
- Correction: Show the Orchestrator returning the candidate and completion request; show the application verifying native review, running source checks, validating the candidate, invoking integration and integrated checks, then requesting provider completion with canonical authority and the delivery receipt. Keep agent-owned candidate production and native specialist organization intact.
- Authority: Accepted executable source and the user's requirement that protected transitions go through harness evidence gates.
- Impact: A downstream implementation or operator could move completion outside the validated application boundary or misunderstand which component must recover a partial delivery.

### FIND-2: Execution diagram makes telemetry validity part of the adapter outcome

- Severity: medium.
- Target: `docs/design/components/MOD-002-execution.md:204`, Processing Diagram.
- Evidence type: exact quotation.
- Candidate evidence: `alt successful terminal event and complete telemetry`; `else runtime or telemetry uncertainty`; `C-->>A: failed or unresolved evidence`.
- Source evidence: `src/backlog_harness/adapters/codex/adapter.py:275` classifies `returned` from process status, native session, successful turn, and absence of runtime errors. It persists that classification and returns at line 300 without reading the telemetry report. `src/backlog_harness/application.py:279` waits for drain after the handle returns, then saves the report and result. `validate_invocation_result` at lines 301–324 separately rejects missing, rejected, or content-mismatched telemetry.
- Assessment: A process can have a durable `returned` outcome while telemetry prevents adoption or advancement. The diagram changes both the owner and timing of the telemetry gate and implies that telemetry rewrites the adapter outcome. The module's prose and MOD-005 correctly place this gate in the application.
- Correction: Show adapter outcome classification and return first, then application drain/report persistence and telemetry validation. Use a separate application branch for a returned process whose telemetry fences advancement; preserve the adapter outcome.
- Authority: Accepted adapter/application source and the shared requirement to preserve exact submission, completion, and failure phases.
- Impact: Recovery code or operators could incorrectly treat a successfully returned CLI call as unresolved and misunderstand the evidence required to adopt the retained result.

### FIND-3: Architecture says skills are not copied into prompts, but the adapter injects them on every call

- Severity: medium.
- Target: `docs/architecture/ARC-002-codex-work-item-dispatch-harness.md:441` and `:568`.
- Evidence type: exact quotation.
- Candidate evidence: `They do not reload unchanged queue history or copy skill bodies.` Also: `Prompts carry references and deltas instead of repeated backlog or skill text.`
- Source evidence: `src/backlog_harness/adapters/codex/adapter.py:155–169` reads every configured `SKILL.md`, concatenates the full contents, and adds them to `effective_prompt` before every new or resumed invocation. At line 264 it writes that prompt to the process. MOD-002 Processing Rules correctly names the effective role-plus-skill prompt.
- Assessment: This is an unsupported current-behavior and token-efficiency claim. The current adapter explicitly uses full skill injection. It is not a by-reference or cached-skill implementation.
- Correction: Distinguish the concise workflow request from the full effective prompt. State that the adapter injects configured role and skill bodies on each harness-issued call. If avoiding that repetition remains desired, label it as deferred intent with separate authority rather than implemented behavior. Do not change accepted source merely to satisfy stale prose.
- Authority: Accepted source and the assignment's source-faithful capability requirement.
- Impact: Maintainers would misjudge context delivery, token cost, and the meaning of skill-byte binding digests.

## Essential evidence and limits

| Essential concern | Evidence and assessment |
| --- | --- |
| Problem fit and non-goals | Module Current Understanding, Parent Context, and Invariants retain the file/main route, explicit coordination none, native delegation, and no custom delegation transport. No material fit defect identified in those inspected sections. |
| Ownership and interfaces | FIND-1 and FIND-2 are material contradictions between architecture/module diagrams and accepted executable ownership. |
| Runtime and configuration | Module contracts distinguish immutable invocation snapshots, compatible exact-session resume, and read-only control loading. No new launch was performed. |
| Failure and recovery | Module evidence preserves requested-effect uncertainty and content-bound telemetry. FIND-2 must be corrected so diagram timing agrees with those contracts. |
| Evidence truthfulness | The release receipt separately records 78 tests/lint, source-matched installation, deterministic installed fixtures, and zero-new-invocation replay. Six native deliveries retain earlier source identity. The 503 probe retains its missing 512-span batch, no observed retry, and no lossless-flush claim. These were not rerun or promoted to current-source native delivery proof. |
| Prompt delivery | FIND-3 contradicts source and the execution module's own description. |
| Complete review | Not performed after the essential defect. Remaining candidate sections are not accepted by omission. |

## Corrections and next owner

The documentation writer should make the three bounded corrections, inspect the parent HLD for equivalent duplicated claims, and refresh the frozen document manifest. A fresh independent reviewer must then recheck essentials and complete current generic, applicable supplemental, page, and sentence evidence before GOOD. The release/source/native/six-item receipts should remain unchanged historical observations.

## Candidate SHA-256 values

| Path | SHA-256 |
| --- | --- |
| `IMPLEMENTATION-PLAN.md` | `66186a2fae0c862aa6e5398f6ebe76acccf4ec4543c5f64bffa018420e47e398` |
| `README.md` | `ddad26970f0ecee800d91a1b9a17892a9172e5797d25465e11c2fb3ecbedf6c8` |
| `docs/architecture/ARC-002-codex-work-item-dispatch-harness.md` | `af54324694ce749b0d5f8fe9d5c7e1768a2c70446643c4b091f5041bded12a71` |
| `docs/design/components/MOD-001-configuration.md` | `90580c316fa0bd59befefdbda070bd04badfc9c8446a5f8517160e4c8b03dcf0` |
| `docs/design/components/MOD-002-execution.md` | `63099e8ef3e2b38f493f06ceb2df67d11414582ce30423aff85c656ed2b27efb` |
| `docs/design/components/MOD-003-provider-coordination.md` | `b53c4b4ae4d0b78beaf21fe483e724e8ad1120aad9a10f9129a06fee5b9756a8` |
| `docs/design/components/MOD-004-evidence.md` | `d3d56814c30277f5e052946d081d6d8c6d0b213f2d2f6d3195019f16b0b28ffb` |
| `docs/design/components/MOD-005-telemetry.md` | `25bfbef56fb576bddee1a27287f1e47d525deb273bff9fdfdd8f912a9c13d58e` |
| `docs/design/components/MOD-006-views.md` | `eade73ec993ce7707c56ac962c789dc6fd933b4b3553e82f4876f22e1bf5407a` |
| `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md` | `65d410fe39b371088544f54dd00c92752479c00004fa8aab116b2f64e433708e` |
| `docs/operator-guide.md` | `fbfa43a87686c5a12a4b9530ae5480d33a68e305ac87b08436de948fa42effef` |
| `docs/verification/final-source-review.json` | `844f28fed47bd6b2ff44cbbd583640393c5bc35021b446e0e75b96e723949c37` |
| `docs/verification/native-exporter-evidence.json` | `1a5170e0157ab09a6af32b3a47d0eb1268ae7ca2d7dd96d7851764d927ad53db` |
| `docs/verification/release-acceptance.json` | `5ee9c86e9c9cc83e4612121f12f953b449e088ee4d06c46dbba43b8d27b988e1` |
| `docs/verification/requirements-matrix.md` | `854bdf5726b6d55295b8ea40bf2bee103a1c0c90dfa185d065478e8a806ac656` |
| `docs/verification/six-item-acceptance.json` | `ac37e54171799e130a72278de9e6f9b456273d3b3672240b95a3bd62426146f9` |
