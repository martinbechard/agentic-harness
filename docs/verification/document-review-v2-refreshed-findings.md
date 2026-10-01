# Refreshed frozen documentation review

Reviewer: Northstar. Date: 2026-09-30.

Verdict: **NEEDS_CORRECTION**. Stage: PASS_1_MATERIAL_DEFECTS.

## Candidate and scope

The current `.agent-ops/document-review-candidate.json` has SHA-256 `8b0ed92c55ed6916f7f2f4b717e8dcfbbcf868c0c0ce59565f65d7a14e9414da`. All 16 bound files matched their recorded hashes. Source authority is `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`.

Pass 1 restarted after the candidate changed. Current HLD sequences, ARC authentication wording, and source execution paths were read again. The preceding findings file is historical evidence for the superseded manifest and supplies no current verdict.

Current critical hashes:

| File | SHA-256 |
| --- | --- |
| `docs/architecture/ARC-002-codex-work-item-dispatch-harness.md` | `9cba91432e307c5f54fe310e363f5b2829a7ac4a249d7c7026ff2a5012fd5ccc` |
| `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md` | `65d410fe39b371088544f54dd00c92752479c00004fa8aab116b2f64e433708e` |
| `src/backlog_harness/application.py` | `8ca97332ebbaa624b4cb5a73c9549437cb158d9a62f50da4653d1938313f0097` |
| `src/backlog_harness/delivery.py` | `23813febd7ee262191298090e6c83fc4314a8182c1ff360930319540dcaecb9e` |
| `src/backlog_harness/contracts.py` | `ab5c4bc76230b8d462ee239a1ae552467c4a7e6b72fa2bfd07544f480646fcc1` |

## Essential findings

### FIND-1 — HLD delivery diagram assigns application effects to the agent

Severity: high. Location: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:515`, especially lines 531–533.

Exact candidate evidence:

```text
        O->>C: perform configured delivery handoff
        C-->>O: readiness and delivery evidence
        O->>P: record provider closeout through selected provider route
```

Assessment: This describes the Orchestrator handing work to a Commit route and recording provider closeout after application validation. The current executable path instead has `Application._run_item` verify native review, run source checks, validate the candidate and usage guard, invoke `delivery.integrate`, and call `provider.transition` with the canonical authority. `delivery.integrate` owns the merge, integrated checks, and READY receipt. The corrected current ARC also assigns these effects to the application. Agent authority over its work does not make it the executor of these effects.

Correction: Show the Orchestrator returning candidate, reviewer identity, and completion request. Show the application performing the gates, invoking the implemented delivery module, receiving the READY receipt, and requesting provider completion. Reconcile nearby Commit-route ownership summaries with this actual route.

Authority and impact: Existing source is the declared authority for current behavior. The architecture and HLD review skills require accurate responsibility, effect ownership, and sequence diagrams. This mismatch would misdirect integration and crash-recovery implementation and diagnosis.

### FIND-2 — HLD question diagram removes the separate source-work invocation

Severity: high. Location: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:552`, especially lines 568–574.

Exact candidate evidence:

```text
    O1->>P: persist authorized answer disposition
    alt approved and provider permits continuation
        O1->>P: perform required selected-skill transition and Running acceptance
        O1->>O1: continue permitted source work in this invocation
```

Assessment: The current `Application.answer` prompt explicitly makes classification read-only and prohibits implementation, delegation, and mutation. The agent returns a question- and answer-bound disposition. The application validates it, performs the provider transition, and records a continuation stage. A later `run_item` invocation resumes source work. Current HLD prose steps 5–7 at lines 584–586 correctly describe this distinction, so the diagram contradicts both its own page and executable source.

Correction: Show a returned disposition ending classification. Assign the resulting provider transition and continuation receipt to the application. Show a distinct later `run_item` gate and exact-session resume before source work. Preserve User Action Required for nonapproval. Also represent question persistence through the actual application/provider boundary.

Authority and impact: The current source, the HLD's own prose, and the review skills require consistent owner and phase contracts. The diagram otherwise permits mutation within a read-only invocation and obscures the durable authorization boundary.

## Authentication clarification

The refreshed ARC accurately narrows the binding to configured authentication profile and resolved `CODEX_HOME` path. `AgentBinding.origin` records those values and no stable account principal. `validate_profile` checks CLI version and successful login status. ARC now explicitly disclaims detection of account or credential changes within the same home. This bounded correction addresses the observed overstatement without requiring source changes.

## Review limits and next action

This substantial review stops on the material essential defects above, as required by `review-structured-artifact`. It does not grant provisional approval to unreviewed concerns. Complete checklist, page, sentence, and mechanical validator evidence is deliberately not produced after a failed essential gate.

No tests, paid calls, source changes, candidate changes, or commits were performed. Native exporter limitations and historical receipts are not new findings. The next owner must correct the HLD diagrams and affected ownership prose, freeze a new candidate, and obtain a fresh essential pass followed by complete evidence before GOOD.
