<!--
Copyright (c) 2026 Martin.Bechard@DevConsult.ca
Artifact-ID: 66ee38a5-c624-40c7-80b9-267d8cd04896
Created-UTC: 2026-09-30T21:38:35Z
Creating-Agent: Northstar
Runtime: Codex
-->

# Requirements-to-verification matrix

This matrix covers all 10 required outcomes, 11 end-to-end scenarios, and six phases in the [implementation plan](../../IMPLEMENTATION-PLAN.md). It distinguishes real native execution from deterministic fixture checks and installed recovery replay.

## Evidence status

- **Current source:** commit `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`, independently accepted in [final-source-review.json](final-source-review.json). All **78 tests** and Ruff pass. The [release receipt](release-acceptance.json) binds source files and the installed wheel.
- **Real execution:** [six-item-acceptance.json](six-item-acceptance.json) records three Completed items in each mode, native independent reviews, source and integrated checks, question continuation, and a reviewed usage hold. These executions predate the final corrections; their original manifests remain intact.
- **Installed current package:** all 24 packaged implementation files match reviewed source. Both retained batches replay successfully with exactly the original invocation IDs: SOLO 9 and MULTITASK 12, with zero new invocations. Configuration reload, later-item discovery, pause/resume, and exact-revision terminal/dashboard parity pass against the installed package using explicit deterministic fixtures.
- **Native exporter limitation:** [native-exporter-evidence.json](native-exporter-evidence.json) records the tested first-export 503 gap and the accepted fail-closed boundary. It does not establish lossless export or complete flush.
- **Documentation review:** current ARC/HLD and six module designs require their independent artifact verdict. Source acceptance alone does not approve documentation.

## Required outcomes

| ID | Required outcome | Implementation and focused tests | Evidence and boundary |
| --- | --- | --- | --- |
| RO-01 | Terminal-first application with read-only overall dashboard. | [MOD-006](../design/components/MOD-006-views.md); `cli.main`, `terminal`, `snapshot`, `create_dashboard_server`; `test_dashboard_serves_exact_projection_and_rejects_mutation`. | Current installed terminal controls and populated dashboard pass. The dashboard labels persisted run state as recorded evidence, rather than claiming process liveness. |
| RO-02 | Until-terminal execution and optional watch for later items. | [MOD-003](../design/components/MOD-003-provider-coordination.md); `RunController.run`; `test_three_item_execution_obeys_selected_mode`; `test_watch_unchanged_scope_does_not_dispatch_and_pause_stop_work`. | Both installed batches settle with Completed=3. Installed watch fixture sees no idle or paused dispatch, then admits one later Ready item once after resume. That discovery fixture substitutes delivery; real delivery is established separately by the six native items. |
| RO-03 | Provider lifecycle, Coordinator scheduling, Orchestrator delivery. | MOD-003; `validate_transition`, `FileProvider.transition`, `Application._run_item`; `test_valid_admission_and_reject_missing_actor_stale_revision`; `test_candidate_bound_independent_review`; `test_stale_frozen_assignment_blocks_admission_before_invocation`; `test_starting_continuation_requires_content_bound_provider_admission`. | Six native items bind actor, candidate, reviewer, checks, merge, and provider completion. Current regressions prevent stale Ready assignments or unbound Starting continuations from launching acceptance. |
| RO-04 | Configurable role CLI bindings and common adapter interface; Codex first. | [MOD-001](../design/components/MOD-001-configuration.md), [MOD-002](../design/components/MOD-002-execution.md); `AgentBinding`, `AgentCliAdapter`, registry, `CodexAdapter`; `test_duplicate_keys_and_unsupported_adapter`; `test_effective_authentication_context_is_bound_to_session`. | Codex 0.159.2 matches the registry latest and remains authenticated. Other production adapters are outside version 1. Native children remain managed by their CLI. |
| RO-05 | Per-call reload and immutable in-flight configuration. | MOD-001; `load_config`, `ConfigSnapshot`, `Application._invoke`; `test_reload_is_deeply_immutable`; `test_skill_change_invalidates_binding`; `test_accepted_workflow_remains_frozen_and_changed_scope_blocks_invocation`. | Installed fixture changes the model between two harness calls, preserves the first snapshot, applies the new model to the second, and rejects changed-profile resume. It makes zero model calls. |
| RO-06 | CLI-specific OpenTelemetry and attributable standard OTLP JSONL. | [MOD-005](../design/components/MOD-005-telemetry.md); `prepare_telemetry`, receiver, sink; `test_http_attribution_retry_and_gzip`; `test_invalid_conflict_oversize_and_durability_failure`. | Six real deliveries retain native root/child/run telemetry. The 503 probe retains later spans while fencing its missing batch; no complete-flush claim. |
| RO-07 | Trace views joined to provider and invocation authority. | MOD-006; incremental `trace_snapshot`, `capture`; `test_trace_projection_reads_only_appended_complete_lines_and_orders_by_time`; `test_cold_projection_rejects_same_count_content_change`. | Installed dashboard shows 39,220 native spans and zero unresolved observations for the completed concurrent fixture. Historical receipts are labeled `legacy_count_only`; new receipts verify count and content. |
| RO-08 | Warning and item-local hold at initially 2 times original high estimate. | MOD-005; analytics, `usage_view`, `guard`, `review_hold`; `test_unknown_dedup_cumulative_and_crossing`; `test_original_estimate_cannot_be_reset_by_provider_edit`; `test_hold_review_retries_frozen_request_after_allowance_write`. | Real concurrent slug held at 1,264 generated tokens over original ceiling 200, retained its owner, and completed after bounded review raised the ceiling to 5,000. |
| RO-09 | Recovery without duplicate execution; unknown usage never zero. | [MOD-004](../design/components/MOD-004-evidence.md), MOD-005; invocation/provider/answer/delivery recovery; `test_submitted_unobserved_invocation_is_not_relaunched`; `test_existing_reservation_without_retained_session_evidence_never_relaunches`; `test_saved_bad_telemetry_never_advances_on_retry`. | Current installed replays preserve all original IDs. Deterministic crash tests cover unknown submission, missing owner evidence, exact provider recovery, merge/check boundaries, answer continuation, and frozen hold-review recovery. |
| RO-10 | No model calls for unchanged polls, status, validation, or dashboard refresh. | MOD-003, MOD-006; `test_watch_unchanged_scope_does_not_dispatch_and_pause_stop_work`; `test_control_storage_remains_inspectable_when_generation_profile_is_missing`; `test_long_lived_view_observes_invalid_configuration_without_reusing_generation_settings`. | Installed status, terminal controls, dashboard refresh, both batch replays, and idle-watch fixture create no model calls. |

## End-to-end scenarios

| ID | Scenario | Focused test evidence | Installed or native evidence |
| --- | --- | --- | --- |
| E2E-01 | Valid workflow evidence advances; missing, wrong, stale, or unauthorized evidence blocks. | `test_valid_admission_and_reject_missing_actor_stale_revision`; `test_candidate_bound_independent_review`; `test_native_child_freshness_candidate_verdict`; stale-assignment and content-bound-admission cases. | Native fresh reviewers accept exact candidates for all six items. Current missing/invalid cases block before protected advancement. |
| E2E-02 | Delivery and independent review produce provider-backed completion. | Candidate review and `test_delivery_crash_boundaries_reconcile_once`. | Every native item has producer, reviewer, candidate, main commit, both check boundaries, and archived Completed provider receipt. |
| E2E-03 | Capacity and isolated concurrent attribution. | `test_three_item_execution_obeys_selected_mode`; `test_capacity_serializes_actual_async_requests`; HTTP attribution tests. | Real maximum overlap SOLO=1 and MULTITASK=2, with separate item telemetry. |
| E2E-04 | Edit changes next call without mutating current settings or session identity. | Immutable reload, authentication binding, frozen-workflow tests. | Current installed deterministic adapter fixture checks two actual application invocation boundaries and changed-profile resume rejection. |
| E2E-05 | Answer resumes exact item and canonical session. | `test_question_gate_requires_canonical_owner_and_exact_approval`; `test_crash_after_answer_commit_resumes_exact_operation`. | Real concurrent greeting uses exact question `greeting-default`, answer digest, canonical owner, approve disposition, and provider commit. |
| E2E-06 | Restart reconciles without duplicate launch. | `test_submitted_unobserved_invocation_is_not_relaunched`; `test_returned_observations_rebuild_missing_stage`; reservation, provider, and delivery recovery tests. | Current installed SOLO and MULTITASK replay create zero invocation IDs. |
| E2E-07 | Threshold holds one item while safe independent work continues. | Usage crossing, canonical hold, frozen review-retry, and MULTITASK tests. | Real concurrent slug hold and reviewed release retain its owner; all three concurrent items complete. |
| E2E-08 | Missing/conflicting telemetry stays unknown and fences next generation. | Saved bad telemetry, missing receipt file, same-count changed content, exact retry-gap tests. | Real native probe rejects a 512-span first batch, observes no retry, retains later 1,200 spans, and fences advancement despite matching native/OTLP output of 11 tokens. |
| E2E-09 | Until-terminal settles and watch discovers later items without idle calls. | Three-item mode tests and watch/pause/resume/discovery regression. | Both installed retained batches settle; current installed deterministic watch admits one later item only after resume and does not repeat it. |
| E2E-10 | Equal terminal/dashboard facts for the same projection revision. | Read-only projection, provider-revision coherence, current-config-view tests. | Installed actual terminal renderer and HTTP dashboard receive the same captured projection; all 11 terminal status fields match, including exact revision, items, counts, usage, freshness, blockers, and uncertainty. |
| E2E-11 | Retry deduplication, resume/run spans, partial lines, exporter failure, incremental reads. | HTTP/gzip/durability tests; exact/partial retry tests; `test_partial_line_is_not_published_or_overwritten`; incremental trace tests. | Native resumed answer/root/child/run spans and tested exporter behavior are retained. Lossless native shutdown remains an explicit limitation, accepted by design authority with fail-closed advancement. |

## Delivery phases

| Phase | Completion evidence |
| --- | --- |
| 1. Project boundary | Installed package, CLI, independent repository, source/test roots, transferred provenance, reconciled ARC/HLD paths, operator guide. |
| 2. Contracts and designs | Six source-backed module designs with mandatory section order, actual interfaces, gates, effects, and test plans; this matrix. Independent documentation verdict is tracked separately. |
| 3. First complete execution path | Six retained native deliveries establish provider, Codex, telemetry, review, checks, integration, terminal outcome. |
| 4. Sustained coordination | Three items per mode; actual overlap, exact answer, reviewed hold, current installed terminal controls, watch discovery, no-call replay. |
| 5. Recovery and usage | Current 78-test source manifest, independent source ACCEPT, installed recovery replay, unknown/gap fences, immutable estimate and identity. |
| 6. Acceptance and delivery | Current built/installed wheel matches reviewed source, populated dashboard, terminal controls, exact-revision parity, package/operator instructions, release receipt. Documentation review remains its final gate. |

## Native exporter decision

The tested Codex 0.159.2 first-export 503 scenario showed no retry. Later exports persisted 1,200 spans and native/OTLP both reported 11 output tokens. Process exit and an unchanged two-second follow-up were observed. Complete flush is unproved because 512 rejected spans remain missing and original batch bytes were not retained.

Design authority accepts truthful gap preservation and fail-closed advancement. This evidence neither requires another paid probe nor authorizes a replacement invocation or invented spans. If original batch bytes are available in another incident, reingest through the existing correlation/dedup boundary and clear the gap only after durability is verified.

## Release boundary

The executable route is file-provider/main-branch delivery with explicit coordination `none`, SOLO or authorized MULTITASK, independent candidate clones, and Codex 0.159.2. Claim-helper projects and other production CLI adapters remain unsupported and fenced. Real delivery evidence remains bound to its historical source; current corrections are established by deterministic regressions, independent review, installed content matching, and zero-invocation recovery replay. See [release-acceptance.json](release-acceptance.json) for current release status and [progress.md](progress.md) for the final documentation verdict.


## Upgrade Recovery Evidence, 2026-10-01

The dev-methodology adoption follow-up adds an explicit upgrade boundary: an already completed integration review must remain reusable after native-receipt verifier corrections. Version `0.1.0a41` preserves the original integration prompt byte-for-byte. `test_frozen_pre_fix_review_request_replays_without_new_invocation` and its changed-request case cover retained request identity and rejection of changed obligations; the focused review suite passes 33 tests. Marked native reviews require the retained invocation request, exact native session/turn/transcript identity, candidate, verdict, and nonempty supporting evidence.

The exact installed a41 artifact additionally replays the actual pre-fix request with adapter execution forbidden, returns invocation `5c556b5c-a3c1-43c9-8540-13eee3561371`, rejects an altered request, and preserves all 62 prior receipts. Evidence is retained in `.agent-ops/pre-backlog-a41/installed-retained-request.json` and its executable verification script. The public reconciliation command then succeeds without a replacement review. This establishes upgrade recovery; provider closure is a separate protected transition.


`tests/test_system_retained_cache_delivery.py` adds three installed-wheel cache regressions (all passed in 44.92 seconds). An unrecognizable legacy observer with missing provenance incurs one refresh. After that refresh, source-only advancement, model/effort edits, integration reconciliation, Completed delivery, and replay add zero inventory observations. Missing capability metadata in a current cache is recovered from the exact saved invocation without a new call; changed native-tool configuration requires one new observation and then reuses it. These checks do not reconstruct missing historical semantic provenance or prove the actual live scan was substantively necessary.
