# Reconcile Documentation Semantic Checks With Authorized Source Revisions

Status: Ready

Priority: Low

Type: Defect

Provider: file

Work Item ID: reconcile-documentation-semantic-checks-with-authorized-source-revisions

Completion: main-branch

## Summary

Reconcile three existing documentation baseline failures against authorized source changes without accepting unexplained semantic drift or weakening unrelated checks.

## Context

Fourth coordinated campaign preflight at main 022a0f8699dec789257261efc059d5355a62ffdb ran all 33 documentation design-system checks plus two focused bundle checks. Three failed before the selected report and ownership-readability items changed source. The five original checks directly covering the ownership page pass in the same sandbox with no skips. These unrelated discrepancies are a nonblocking follow-up, not evidence that the full suite passed.

## Source Evidence

- analytics/coordinated-fourth-pair/baseline-checks.json preserves the original 35-check command and failing result.
- analytics/coordinated-fourth-pair/baseline-failure-details.json binds exact failure identities and diagnostics.
- Failures: DocumentationDesignSystemTests.test_agentic_configuration_uses_shared_shell_and_preserves_semantics; test_lifecycle_uses_versioned_shell_and_real_navigation; test_readme_states_bounded_coordination_and_portability_evidence.
- The first two compare semantic digests; the third expects a literal model-profile list absent from README. The source revision and authorization must be audited, not assumed correct because tests are stale.

## Requirements

- Compare each discrepancy against its last accepted source, relevant subsequent delivery evidence and governing source contract.
- Preserve historical migration baselines. Introduce exact authorized revision bindings where prose changes were approved; correct source if the difference was not authorized or loses required meaning.
- Keep adversarial semantic/accessibility coverage. Never solve this by suppressing failed tests, dropping requirements or broadly replacing digests without evidence.
- Keep scope separate from active ownership-readability work. Reconcile latest main and finished ownership changes before taking its test-file scope.

## Acceptance Criteria

All three original failing checks pass for independently reviewed reasons, complete documentation suite runs without skipped required checks, and prior failures remain recorded truthfully.

## Dependencies

None.

## Coordination Notes

Do not concurrently modify scripts/test_documentation_design_system.py while its ownership-readability owner is active.

## Verification

Run the original three checks and full scripts.test_documentation_design_system suite in the configured subscription-worker sandbox/runtime/PATH. Obtain fresh source and content review of any semantic baseline change. Record exact before/after evidence.

## Open Questions

Determine whether each discrepancy reflects an authorized prose revision or unintended source drift from authoritative source history.


## Related Inherited Generated Baseline Finding

- Same-sandbox `scripts/build-support-checklist.py --check` fails on both unchanged baseline and report candidate. Independent generator-output comparison produces identical SHA-256 aabddfd4f8b0f476e4f221d7d1532ddbdbf493202847d8dea97f9df1b29a17d7 for both trees. This is existing freshness debt, not a report-source regression.
- Evidence: analytics/coordinated-fourth-pair/support-checklist-freshness-diagnosis.json and support-checklist-output-comparison.json. Retain these failures; the report candidate does not silently pass this check.
- As part of reconciling these documentation baselines, audit the stale design/agent-skill-test-coverage-checklist.md through its owning scripts/build-support-checklist.py. Regenerate only after validating underlying source evidence and include freshness validation in the follow-up. No hand-editing generated counts or coverage claims.
