# Frozen documentation review v2

Reviewer: Northstar. Date: 2026-09-30.

Verdict: **NEEDS_CORRECTION**.

Stage: PASS_1_MATERIAL_DEFECTS. This is provisional essential-review evidence, not a completed checklist or documentation approval.

## Exact candidate

- Manifest: `.agent-ops/document-review-candidate.json`.
- Manifest SHA-256: `32639d05e1c99825a921ba5d77889552cdbe49913341084eb6d6c10e36247bb0`.
- All 16 manifest file hashes matched the filesystem during this review.
- Source authority: `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`.
- HLD SHA-256: `65d410fe39b371088544f54dd00c92752479c00004fa8aab116b2f64e433708e`.
- ARC SHA-256: `d7058f30f8579cd62c778cd3c3494bdcd14f2eda750c876ff357330630ec60dd`.
- MOD-002 SHA-256: `a46c8d1b4df3e5939dc8dc1f9be373f2fda4b4df68ca051f01217dee9d8061c2`.
- `src/backlog_harness/application.py` SHA-256: `8ca97332ebbaa624b4cb5a73c9549437cb158d9a62f50da4653d1938313f0097`.
- `src/backlog_harness/delivery.py` SHA-256: `23813febd7ee262191298090e6c83fc4314a8182c1ff360930319540dcaecb9e`.
- `src/backlog_harness/adapters/codex/adapter.py` SHA-256: `c4711f9361d92af4b975587c80a0c2a2eeb4747407c84922bc61f7e50b5c4f58`.

## Findings

### FIND-1: HLD delivery diagram assigns executable effects to the wrong owner

- Severity: high.
- Target: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:515`, Delivery And Independent Review; especially lines 531–533.
- Evidence type: exact quotation from the HLD Mermaid source.

```text
        O->>C: perform configured delivery handoff
        C-->>O: readiness and delivery evidence
        O->>P: record provider closeout through selected provider route
```

- Assessment: The diagram assigns delivery handoff and provider closeout to the Orchestrator after the application validates evidence. Current source assigns execution of these effects to the application. `Application._run_item` independently verifies native review, runs source checks, validates the candidate and usage guard, calls `delivery.integrate`, then calls `provider.transition` with the produced canonical authority. `integrate` performs the main-branch merge and integrated checks and writes the READY receipt. There is no intervening Orchestrator-to-Commit runtime handoff. The corrected ARC Data Flow diagram already reflects application-owned execution, so the HLD also contradicts its current parent.
- Authority: Current source takes precedence for existing behavior, as the HLD itself states. `review-structured-artifact` requires correct responsibilities, interfaces, authority boundaries, and diagrams. `review-high-level-design` requires consistent cross-module state and effect ownership.
- Impact: An implementer or incident responder following this diagram would look for an agent delivery handoff that does not occur and could misplace provider-completion or crash-recovery responsibilities.
- Correction: Show the Orchestrator returning the candidate, reviewer identity, and completion request. Show the application executing review/check/candidate/usage gates, calling the implemented delivery module, receiving its READY receipt, and requesting provider completion with canonical authority. Keep agent ownership of implementation and native delegation explicit without assigning application effects to that agent. Align the HLD's nearby Commit ownership statements with this implemented route.

### FIND-2: HLD question diagram permits source work inside the read-only classification invocation

- Severity: high.
- Target: `docs/design/high-level/HLD-003-codex-work-item-dispatch-service.md:552`, Question And Independent Progress; especially lines 568–574.
- Evidence type: exact quotation from the HLD Mermaid source.

```text
    O1->>P: persist authorized answer disposition
    alt approved and provider permits continuation
        O1->>P: perform required selected-skill transition and Running acceptance
        O1->>O1: continue permitted source work in this invocation
```

- Assessment: `Application.answer` resumes the canonical session specifically for read-only classification and prohibits implementation, delegation, and mutation. The application validates the returned question ID, answer digest, and disposition. The application then performs the provider transition and persists a continuation stage. Only a later `run_item` call can resume source work. The HLD prose directly below the diagram correctly describes this separation in steps 5–7, making the diagram internally contradictory as well as source-inaccurate.
- Authority: `src/backlog_harness/application.py:728` through the end of `answer`, especially the read-only prompt, application-owned transition, and continuation receipt. HLD Question And Independent Progress steps 5–7. The review skills require diagrams and prose to agree on owner, phase, and failure timing.
- Impact: The diagram erases a required invocation and authorization boundary and incorrectly depicts source mutation as permissible during classification. It also misidentifies who persists the disposition and Running transition.
- Correction: End the classification invocation with the Orchestrator returning the bound disposition to the application. Show the application validating and persisting the provider result. For approval, show a durable continuation stage followed by a distinct later `run_item` generation gate and exact-session resume. For nonapproval, retain User Action Required without source work.

## Essential evidence and review limits

The essential pass independently checked the native delegation boundary, application versus agent ownership, adapter method signatures, current configuration and immutable-session binding explanations, and the corrected ARC and MOD-002 sequences against source. ARC now shows application-owned integration and provider completion. ARC explicitly records full role and skill-body injection on each invocation; the adapter source supports it. MOD-002 now separates adapter process classification from the later application telemetry acceptance gate.

The two HLD contradictions above remain material regardless of the accepted native exporter limitation. This review does not request additional paid probes, lossless native flush, a new delegation framework, source changes, or test reruns. Historical delivery receipts and documentation-pending receipt fields are not treated as false current-source approval.

The substantial-artifact protocol requires stopping on material essential defects. Therefore complete generic and supplemental checklist judgments, shared page evidence, per-sentence manifests, and validator receipts were not generated. No full-scope approval is implied for the remaining candidate pages. Previous essential-review outputs remain unchanged.

Terminology outcome: TERMINOLOGY STANDARDS LOADED, ABSENT in configured snapshot `d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658`. The reference provider returned only `reference_not_found`; no host-wide absence claim is made.

Next owner action: Correct the HLD delivery and question diagrams and their affected ownership summaries, refresh the frozen candidate manifest, then obtain a fresh essential pass and complete review evidence for that exact candidate.
