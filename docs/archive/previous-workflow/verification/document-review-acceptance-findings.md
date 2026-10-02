# Documentation acceptance review

Reviewer: Northstar. Date: 2026-09-30.

Verdict: **NEEDS_CORRECTION**. Stage: **PASS_1_MATERIAL_DEFECTS**.

## Candidate and scope

The frozen manifest is `.agent-ops/document-review-candidate.json`, SHA-256 `d57245da2c1c7c93c4ebbdf7bb8a171194a92d7cd3b10f7f34a0ee383c5f84e7`. All 16 file hashes match. HEAD is the accepted source commit `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`; its tree is `1781fab046c4066e52a96d5966d18e726651547b`. All 40 source-file hashes in `docs/verification/release-acceptance.json` match the checkout.

The review covers the implementation plan, README, operator guide, ARC-002, HLD-003, all six module designs, requirements matrix, and four manifest-bound JSON receipts. Accepted source and its retained independent source review are authoritative for existing behavior. This review does not reopen source acceptance or require another native run.

The six previous findings have been corrected at their cited locations. Essential review across the complete scope found the remaining contradictions below. They require bounded prose corrections, not implementation changes.

## Findings

### ACCEPTANCE-1 — The architecture still overstates the adapter boundary and assigns execution to a types-only module

Severity: medium. Category: architecture and cross-module contract.

Exact candidate evidence:

- ARC-002, System Context, line 193: `CLI-specific process, protocol, authentication, and event details stay inside adapters. The rest of the application reasons only in terms of portable capabilities, invocation handles, session handles, events, errors, usage, supported interruption, and observed outcomes.`
- HLD-003, Selectable Agent CLI Runtime, line 114: `The Adapter pattern owns that translation while keeping adaptee details out of application.py.`
- HLD-003, AgentCliAdapter, line 897: `Application code sees only normalized contracts.`
- HLD-003, Scenario-To-Operation Mapping, line 182: `runtime.py resolves each role binding and invokes the selected AgentCliAdapter.`
- HLD-003, Minimal Reconciliation Evidence, line 100: `runtime.py owns session and event evidence.`

Source evidence:

- `src/backlog_harness/runtime.py` contains three data classes and the Protocol. It imports `AgentBinding` from `contracts.py` and performs no binding resolution, adapter invocation, persistence, or event observation.
- `Application._invoke` resolves the binding and adapter, performs preflight, and awaits start or resume in `src/backlog_harness/application.py:159` and `:268`.
- `Application.native_sessions_root` at `application.py:418` interprets Codex home and its `sessions` directory. `recover_invocation` at `:367` reads native Codex records and inspects `task_complete`. `usage_view` imports native child accounting at `:487`. Candidate completion calls `verify_native_review` with native producer/reviewer identities at `:1145`.
- `src/backlog_harness/native_evidence.py:15` reads the Codex session layout and native records. This is a supporting module outside the concrete CLI adapter. MOD-002 and MOD-004 already identify this dependency correctly.

Assessment: the corrected active-call section in HLD-003 is accurate, but these other statements still describe a stronger separation and different execution owner. The current selected Codex implementation uses both the portable launch Protocol and a Codex-specific native-evidence path. A reader cannot infer that implementing the Protocol alone makes all application recovery, review, and usage behavior portable.

Correction: assign binding/invocation orchestration to `application.py`, binding construction to `contracts.py`, contract shapes to `runtime.py`, and durable session/event writes to the adapter and evidence module. State explicitly that native review, child usage, and some recovery remain Codex-specific application dependencies through `native_evidence.py`. Narrow the universal isolation claims to the launch/process boundary actually provided. Preserve the closed Codex-only production route; do not move code or add an abstraction.

Authority: accepted source, EXISTING_IMPLEMENTATION mode, and the architecture/HLD/interface-pattern requirements for accurate ownership and compatibility boundaries. Impact: maintainers otherwise receive conflicting integration seams and an unsupported adapter-portability guarantee.

### ACCEPTANCE-2 — PendingOperation still puts the requested marker on the wrong side of the adapter call

Severity: medium. Category: durable effect ordering.

Exact candidate evidence, HLD-003, PendingOperation, line 944:

> **Requested boundary:** requested.json is durable before the adapter call.

Source evidence: `Application._invoke` calls `start_session` or `resume_session` at `src/backlog_harness/application.py:277`. Inside that adapter call, command preparation and binding revalidation precede `EvidenceStore.requested(request.evidence_path)` at `src/backlog_harness/adapters/codex/adapter.py:172`; subprocess creation follows at `:176`.

Assessment: adapter entry does not imply that requested evidence exists. Preparation can fail before the marker. The candidate's own Operational Evidence Paths and Cross-Module Contract Reconciliation correctly place the marker immediately before subprocess submission, so this sentence contradicts both source and adjacent design contracts.

Correction: say that the adapter persists `requested.json` immediately before native subprocess submission, after preparation and binding checks. Keep intent-before-adapter-call distinct from requested-before-subprocess-creation.

Authority: accepted source and effect-phase consistency requirements in the HLD/module review skills. Impact: the current sentence misclassifies the crash/preparation boundary used to distinguish known-not-submitted evidence from uncertain submission.

### ACCEPTANCE-3 — Startup summaries contradict the corrected command-specific startup flow

Severity: medium. Category: current operational contract.

Exact candidate evidence:

- ARC-002, Startup And Restart, line 454: `At startup, the application validates the configuration location, registered adapter names, and provider capability. It then reads the complete provider scope and reconciles every known session and pending operation through the configuration snapshot and immutable binding captured for that operation.`
- HLD-003, Assumptions And Resolved Decisions, line 202: `The operator selects a configuration file with --config, and startup resolves only its absolute path for the run.`
- HLD-003, Configuration Boundaries, line 1210: `Startup resolves only the configuration path.`

Source evidence: `src/backlog_harness/cli.py:41` selects full versus control-only loading by command and loads configuration before routing. `Application.__init__` at `src/backlog_harness/application.py:33` resolves and loads configuration and constructs its provider/caches. It does not reconcile. Status and dashboard route to projection; `reconcile` explicitly calls reconciliation; run control performs its reconciliation before admission. The corrected HLD Startup And Capability Validation section at line 397 accurately records these distinctions.

Assessment: the summaries promise incompatible startup behavior. One promises automatic provider/session reconciliation; the other promises only path resolution. Neither accurately describes opening the terminal or reading status with invalid generation settings. This is material to the documented inspection and recovery route.

Correction: reconcile all three summaries with the existing command-specific section and diagram. State that startup loads the selected command's configuration mode and constructs the application; projection and explicit reconcile/run operations perform their respective reads and reconciliation. Keep per-invocation version/login preflight separate. A reference to the existing HLD flow is sufficient; no new mechanism is needed.

Authority: accepted `cli.py`, `Application.__init__`, `RunController`, and the candidate's corrected startup section. Impact: operators and maintainers otherwise cannot determine whether opening the application reconciles an uncertain effect or whether invalid generation configuration prevents inspection.

## Essential correctness evidence

| Concern | Evidence and judgment |
| --- | --- |
| Problem fit and feasibility | Plan, README, operator guide, ARC/HLD and module responsibilities preserve agent-owned native delegation and harness-owned evidence gates. No custom delegation framework or new paid experiment is required. |
| Selected scope | File provider, main-branch completion, explicit coordination `none`, Codex 0.159.2, SOLO and approved MULTITASK are consistently bounded. Other provider/helper/CLI routes remain unsupported. |
| Admission and continuation | ARC and HLD now enclose execution in an explicit successful-admission or validated-continuation fragment. Assess, invalid admission and unchanged observation do not authorize an Orchestrator call. Source gates retain frozen assignment, admission and canonical acceptance. Previous FIND-1 is corrected. |
| Runtime call path | HLD per-invocation and reconciliation sections now distinguish `handle.events`, direct `EvidenceStore.reconcile`, and unused Protocol methods. Previous FIND-2's cited call-path claim is corrected. ACCEPTANCE-1 identifies remaining conflicting ownership/isolation statements elsewhere. |
| Event durability | The HLD now places native identity validation and `session.json` persistence before event construction and append. The diagram agrees. Previous FIND-3 is corrected. ACCEPTANCE-2 concerns the separate requested-marker boundary. |
| Required effect views | HLD now has command-specific startup and event-processing diagrams. MOD-003 shows provider preparation, commit, candidate integration, checks and completion. MOD-004 separates committed-effect proof, prepared-provider recovery, and safe delivery re-entry. MOD-005 adds measured usage, ceiling and hold review. Previous FIND-4 is corrected. |
| Identity and authorization | Session origin binds configured profile and resolved Codex-home path, not a stable account principal. Candidate review remains native, distinct, fresh and exact-candidate-bound. Provider actor gates and source access after Running are preserved. |
| Questions, capacity and recovery | HLD question flow separates read-only answer classification from later source work. Modules preserve original owner, exact answer, retained reservations, capacity occupancy, provider receipts and unique merge proof. No blind replacement launch is introduced. |
| Telemetry and usage | Module and receipt evidence retain rejected-export fingerprints, exact content binding, unknown usage and item-local holds. The lost 512-span batch remains an accepted fail-closed limitation; matching 11 output tokens and 1,200 later spans do not prove lossless flush. |
| Evidence provenance | ARC verification now separates historical native deliveries from current-source checks, wheel, controls and zero-call replay. Module readiness statements reference completed current gates and retain documentation review as pending. Previous FIND-5 is corrected. |
| Operator projections | MOD-006 separates the full projection from the exact 11 interactive status fields and identifies `session show` inspection. Previous FIND-6 is corrected. |
| Verification claims | Four JSON receipts distinguish accepted source, 78 deterministic tests, current installed controls, 9/12 retained invocation IDs, earlier native deliveries and native exporter limits. These are retained observations, not fresh execution by this reviewer. |
| Module structure | Six modules have the 28 current template headings in order and leading ACCEPTED/READY decisions. Template digest: `9d0779b1edd96665ec27196cf572582eb8c97fd57c3e1262fca4705a9d868fc9`. |

All 32 candidate Mermaid blocks were inspected for essential meaning. This is not a rendering or syntax-validation receipt. The additional diagrams address the previously identified missing relationships; the remaining findings are conflicting prose and ownership claims.

## Review protocol and verification limits

The generic structured checklist and architecture, HLD, module and interface-pattern supplements were loaded. Their SHA-256 digests are, respectively:

- `510b081be79b1d24373e4164470fa06c9e971f34f4a237fcb25ab9a73fb4b681`
- `96648df3aa495598065accf0549dbb3d36e6eab40789f724cad15d01aed40ed0`
- `5c5d719b0caf802f7f9ab07668218944c89b09bd391b7ab4f2d5fc27bc908a26`
- `127f881322f10b3b5688a4761df0e7f2dc1784a395d81d6c53cd54a5ce4bd812`
- `b599c265400305f0c9f8bd643b7834c0bc01d5923a1aac1d4d1137b97083f412`

Terminology loading returned only `reference_not_found`. This means TERMINOLOGY STANDARDS LOADED, ABSENT, for configured snapshot `d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658`. No wider absence or conformance claim is made.

The Markdown link tool reported 18 `path_outside_root` findings for transferred sibling-methodology references in ARC/HLD. It reported no other link findings. The scope patterns also selected historical review companions; those companions were not treated as current approval. The rejected sibling paths were not rechecked through a fallback. Their link verification remains unresolved; it does not erase source-backed judgments or independently demand a rewrite of historical provenance links.

No local ancestor AGENTS.md or root PROJECT.yaml was present. The supplied Northstar identity applies. The sibling-methodology guidance referenced by the HLD does not authorize mutation in that repository. The memory quick search found no task-specific entry; no memory-derived claim is used.

Under [review-structured-artifact](/Users/martinbechard/.agents/skills/review-structured-artifact/SKILL.md), “For a Pass 1 material defect, write only provisional findings at an authorized findings path.” This report therefore stops before complete checklist, sentence-manifest and mechanical-evidence acceptance. It is not PAGE_VERIFICATION_EVIDENCE_READY or GOOD. The substantive findings are source-contract corrections, not STE sentence-length preferences.

Only this new report was created. Candidate documents, implementation, tests, prior reviews and receipts remain untouched. No tests, native/model executions, paid calls or commits were performed. After correcting these bounded contradictions, freeze the resulting documentation and obtain a fresh independent review that can continue through complete evidence if essentials pass.
