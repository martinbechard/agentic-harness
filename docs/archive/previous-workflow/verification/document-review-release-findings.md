# Documentation release review

Reviewer: Northstar. Date: 2026-09-30.

Verdict: **NEEDS_CORRECTION**. Stage: **PASS_1_MATERIAL_DEFECTS**.

## Candidate and authority

The frozen candidate manifest is `.agent-ops/document-review-candidate.json`, SHA-256 `ef51b5f7cdaf0895a533f678d6e70adcd03802d3ccdf3d2e7344ddd5fb88f16e`. All 16 candidate file hashes match. All 40 source-file hashes in `docs/verification/release-acceptance.json` match. HEAD remains accepted source commit `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`.

The complete assigned scope was read: implementation plan, README, operator guide, ARC-002, HLD-003, six module designs, requirements matrix, and four JSON receipts. This is an independent documentation review. The accepted source review and retained execution evidence remain authoritative; no source acceptance decision was reopened.

The nine findings in the two preceding reports were checked against the current candidate. The revised ownership prose, requested-marker boundary, startup summaries, admission branches, event persistence order, effect diagrams, historical/current evidence distinctions, and interactive status fields address the previously cited locations. A remaining architecture diagram still contradicts the corrected ownership and dependency account.

## Findings

### RELEASE-1 — The architecture dependency diagram still assigns executable responsibilities to the contract module and mixes dependency direction with data flow

Severity: medium. Category: architecture responsibility and dependency contract.

Target: `docs/architecture/ARC-002-codex-work-item-dispatch-harness.md`, Architectural Layers, lines 285–310; related summary at lines 333–337.

Exact candidate evidence:

```text
Dependencies point inward through explicit interfaces.
```

The immediately following diagram labels its nodes with implementation files and includes these exact edges:

```text
    Application --> Runtime[runtime.py]
    Application --> Coordination[coordination.py]
    Runtime --> Registry[adapters/registry.py]
    Provider --> Projections[projections.py]
    Runtime --> Projections
    Projections --> Terminal
    Projections --> Analytics[analytics.py]
    Analytics --> Dashboard[dashboard.py]
```

Source evidence and assessment:

- `src/backlog_harness/runtime.py:1` contains data classes and the Protocol. It neither resolves the registry nor calls projection code. `Application._invoke` resolves the registry and invokes the adapter at `src/backlog_harness/application.py:267`.
- `src/backlog_harness/coordination.py:124` invokes `Application.run_item`; the application does not call or import `RunController`. The terminal constructs the controller at `src/backlog_harness/terminal.py:26`.
- `src/backlog_harness/projections.py:95` reads the provider and evidence. The provider does not depend on projections. Projection obtains usage through `Application.usage_view` at `projections.py:160`; it does not call `analytics.py` directly.
- `src/backlog_harness/terminal.py:10` imports and calls `snapshot`. Projection does not invoke the terminal.
- `src/backlog_harness/dashboard.py:58` calls the injected collector. `analytics.py` has no dashboard dependency.

These edges cannot consistently mean caller-to-dependency: several point in the opposite direction, and others name calls that do not exist. They also cannot consistently mean data movement: the diagram begins with terminal-to-application control and then routes registry resolution through a types-only module. This is a material architecture contract, not a preference about arrow style. It gives maintainers a different ownership and dependency model from the corrected HLD and source.

Correction: give the diagram one explicit edge meaning and reconcile every edge with that meaning. For a caller/dependency diagram, show application-to-registry, controller-to-application, terminal-to-controller and projection, projection-to-provider/evidence and application usage, and dashboard-to-injected projection collector. Keep `runtime.py` as a contract dependency, not an executing intermediary. If data movement is also useful, distinguish it explicitly in a separate view or with labeled edge types. Reconcile the adjacent Portable agent runtime and Agent Runtime And Adapter Registry summaries so that the conceptual runtime does not silently imply that `runtime.py` performs binding resolution. Retain `contracts.py` binding construction, application orchestration, adapter execution, and the documented Codex-native evidence dependencies. No source change, new abstraction, or additional native execution is required.

Authority: accepted source; EXISTING_IMPLEMENTATION mode; the architecture checklist questions concerning dependency direction, ownership, coherent system frame, and useful structural diagrams; and the generic checklist requirement to avoid material contradictions.

Impact: an implementer following the architecture would place calls in the wrong module or infer dependencies that violate the actual application/control/projection boundaries. The diagram also reintroduces the execution ownership error that the current prose correction was intended to remove.

## Essential correctness evidence

The review considered all essential concerns across the full scope before stopping. This table records the provisional judgments; it is not a completed Pass 2 checklist.

| Concern | Evidence and judgment |
| --- | --- |
| Problem fit and feasibility | Plan, README, operator guide, ARC and HLD preserve native agent organization and harness verification before protected transitions. The selected implementation needs no custom delegation transport or additional provider framework. |
| Scope and non-goals | Codex 0.159.2, file provider, main-branch delivery, explicit resource coordination `none`, SOLO and approved MULTITASK are consistently bounded. Other production adapters and claim-helper routes require separate work. |
| Authority and candidate gates | MOD-003 and HLD distinguish Coordinator admission, canonical Orchestrator ownership, native independent review, exact candidate identity, source checks, integration checks, and provider completion. Runtime evidence and projections do not become lifecycle authority. |
| Admission scenarios | ARC and HLD diagrams now contain explicit successful-admission or validated-continuation fragments. Assess, invalid admission and unchanged observation do not fall through to Orchestrator invocation. |
| Runtime ownership and portability | Corrected HLD and ARC System Context explicitly retain Codex-native recovery, review and child-accounting dependencies outside the adapter. The remaining concrete dependency-diagram contradiction is RELEASE-1. |
| Configuration and startup | MOD-001 and HLD distinguish complete generation validation, control-only inspection, construction without automatic reconciliation, explicit reconcile/run paths, and per-invocation version/login preflight. In-flight bindings remain immutable. |
| Submission and events | HLD PendingOperation and MOD-002/MOD-004 distinguish intent before adapter entry from requested evidence inside the adapter immediately before subprocess creation. The event sequence persists validated session identity before event construction and append. |
| Identity and authentication | Source and designs bind originating adapter, executable, profile and resolved Codex-home path. They do not promise stable account-principal identity within an unchanged home. Exact native resume and fresh candidate-bound reviewer evidence remain distinct gates. |
| Capacity and independent work | The HLD separates harness invocation slots, retained uncertain occupancy, provider reservation, and native child concurrency. MULTITASK still needs explicit approval, isolated candidates and compatible scopes. |
| Questions and recovery | Question classification is read-only and exact-session-bound. Later source work separately requires Running and continuation evidence. MOD-004 distinguishes known-not-submitted, uncertain invocation, committed provider proof, prepared recovery, and delivery re-entry without blind repetition. |
| Telemetry and usage | MOD-005 and the exporter receipt preserve the rejected 512-span gap, exact export fingerprints, content-bound receipts, conservative accounting and item-local holds. The 1,200 later spans and matching 11 output tokens do not prove lossless flush. No new probe is required. |
| Operator projections | MOD-006 distinguishes the full projection from exactly 11 interactive status fields and separate session inspection. Provider coherence, current control configuration, unknown/stale observations and historical count-only labels remain explicit. |
| Evidence provenance | The four receipts and requirements matrix separate six historical native deliveries from current-source tests, source review, matching installation, controls and zero-invocation replay. Current replay preserves the original SOLO 9 and MULTITASK 12 invocation IDs. Historical review artifacts do not approve this candidate. |
| Module structure | All six modules contain the current template's 28 level-two headings in order and leading ACCEPTED/READY decisions. Those decisions expressly describe maintenance/readiness and do not claim independent artifact approval. |
| Diagrams | All 32 candidate Mermaid blocks were inspected for essential meaning. The admission, ordered provider/delivery effects, usage-to-hold path and event persistence repairs are present. RELEASE-1 concerns the architecture dependency view. This is not a rendering or syntax-validation receipt. |

## Review inputs and limits

The loaded review set includes the core communication, STE, terminology, structured-artifact and page-verification skills, plus architecture, HLD, module, interface-pattern and documentation-routing guidance. The selected canonical checklist hashes are:

| Checklist | SHA-256 |
| --- | --- |
| Structured artifact | `510b081be79b1d24373e4164470fa06c9e971f34f4a237fcb25ab9a73fb4b681` |
| Architecture | `96648df3aa495598065accf0549dbb3d36e6eab40789f724cad15d01aed40ed0` |
| HLD | `5c5d719b0caf802f7f9ab07668218944c89b09bd391b7ab4f2d5fc27bc908a26` |
| Module | `127f881322f10b3b5688a4761df0e7f2dc1784a395d81d6c53cd54a5ce4bd812` |
| Interface patterns | `b599c265400305f0c9f8bd643b7834c0bc01d5923a1aac1d4d1137b97083f412` |

The module template digest is `9d0779b1edd96665ec27196cf572582eb8c97fd57c3e1262fca4705a9d868fc9`.

Terminology loading returned only `reference_not_found`: TERMINOLOGY STANDARDS LOADED, ABSENT, within configured snapshot `d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658`. No wider absence claim is made. No ancestor or local AGENTS.md was present; the supplied Northstar identity applies. The memory quick search found no task-specific entry and supplied no review judgment.

The Markdown link tool checked exactly the 12 Markdown candidates. It reported 18 `path_outside_root` findings for transferred sibling-methodology references and no other findings. No fallback bypassed those structured rejections. Their link verification remains unresolved. This does not invalidate the local source evidence supporting RELEASE-1 or establish that the sibling files are absent.

Under [review-structured-artifact](/Users/martinbechard/.agents/skills/review-structured-artifact/SKILL.md), “For a Pass 1 material defect, write only provisional findings at an authorized findings path.” This report therefore stops before exhaustive checklist, page/sentence manifest, and mechanical acceptance evidence. It does not return GOOD or PAGE_VERIFICATION_EVIDENCE_READY. The stop is caused by a concrete architecture contradiction, not a sentence-length preference or the accepted native-exporter limitation.

Only this new report was created. Candidate documents, source, tests, earlier reviews and receipts remain unchanged. No tests, native executions, paid calls or commits were performed.

Next owner action: correct the bounded architecture view and its adjacent ownership summaries, freeze the resulting candidate, and obtain a fresh independent review. That review must complete the applicable checklist, page, sentence and mechanical evidence before accepting the documentation. Existing source acceptance remains intact.
