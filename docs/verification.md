# Replacement harness verification

The scope is `functional-spec.md`. Historical acceptance records in `archive/previous-workflow` do not apply to this implementation.

## Use case audit

| Requirement | Implementation | Executed evidence |
| --- | --- | --- |
| UC-001: next ready item | Access-agent selection; development-agent status ownership; explicit outcome | `test_success_and_user_action_are_updated_by_development_agent`; separate-project delivery and merge |
| UC-002: timeout | Independent process timeout, group kill/reap, provider failure record, retry/hold | `test_timeout_kills_before_retry_and_limits_to_two_retries`; `test_actual_timeout_processes_are_reaped_before_hold` |
| UC-003: exception/crash | Failed outcomes and stopped-process detection; failure classification | `test_exception_nontransient_holds_and_transient_retries`; `test_stopped_process_without_running_item_uses_failure_policy`; real transient failure/retry |
| UC-004: no ready item | Polling interval with idle capacity | `test_no_ready_items_wait_and_pause_during_provider_query`; separate-project dependency wait |
| UC-005: pause | Blocks new/retry dispatch, lets active attempts finish | `test_interrupted_recovery_preserves_workspace_and_waits_for_resume`; foreground CLI test |
| UC-006: resume | Wakes polling and dispatch | `test_foreground_cli_pause_resume_and_epic_exit` |
| UC-007: user action | Development agent records question/status; reports outcome; no answer handler | `test_nondelivery_status_stays_in_worktree_without_main_mutation[user_action-user-action-required]` |
| UC-008: concurrent backlog | Capacity-limited assignments with active exclusions and distinct worktrees | Scheduler concurrency and invalid-batch tests; real parallel subprocesses |
| UC-009: epic dependencies | Epic selection and provider-owned readiness/completion | `test_parallel_delivery_dependencies_defects_and_merge`: B depends on merged A |
| UC-010: spare capacity | Second access query outside epic after epic items receive priority | `test_concurrency_epic_priority_and_outside_capacity`; real outside-item delivery |
| UC-011: discovered defects | Delivery instructions delegate recording and blocking fixes | Separate-project blocking/unrelated defect creation, blocking fix, and publication by merge |
| UC-012: integration | Serialized merge-agent invocations on completion and interval | `test_merge_is_serial_and_completion_trigger_is_retained`; combined-test failure rejects merge; delayed-commit race regression |
| UC-013: interrupted recovery | Inspect local item, reuse worktree/branch, instruct assessment before continuing | `test_real_process_recovery_and_retry[interrupted]`; stopped-parent/live-child cleanup |
| UC-014: restart if not resumable | Replacement instructions preserve unrelated work while restarting | `test_real_process_recovery_and_retry[restart]` |

## Principles and exclusions

- **P-001:** All provider operations go through the access-agent command. Normal status changes occur in the development-agent command. The Codex adapter supports an independently configured model and reasoning effort per role.
- **P-002:** CLI prompts address the human user explicitly. Pause/resume is exercised through actual stdin; user-action questions are stored by the development test agent.
- **P-003:** `test_every_activity_has_matching_console_log_and_otel_record` checks identical content and timestamps across console, JSONL activity log, and OTLP JSON log export. Actual process output streaming and incomplete UTF-8 are covered separately.
- **P-004:** Item-specific access commands and development commands run in the item's worktree. Separate-project tests verify main remains unchanged for unmerged failure/user-action updates and receives delivered records through Git merge. Merge instructions explicitly require reconciliation of conflicting attempt information.
- **P-005:** Tests cover transient retries, nontransient holding, timeout exhaustion, and recovery. Two automatic retries are shared by delivery and interruption recovery within the foreground run.
- **P-006:** Integration fixtures initialize a separate temporary Git project outside this checkout, with status folders, work item creation/update, dependencies, defect recording, independent worktrees, combined tests, and delivery merges. No production backlog is used.
- Manual item selection, human-answer management, direct provider transactions, dashboards, harness-owned review/proof gates, and retired transports have no implementation or public command. The wheel contains only `__init__`, `__main__`, `cli`, `codex`, `engine`, and `process`.

## Verification results

Verified on 2026-10-02 with Python 3.12:

- 73 tests passed, including real agent subprocesses, worktree recovery, process-group cleanup, parallel delivery, dependency order, combined-result testing, CLI controls, and structured adapter results.
- Coverage: 100% of 446 production statements and 130 branches. The existing 100% gate remains enabled.
- Ruff lint and format checks passed.
- Source distribution and wheel built successfully. Wheel contents were inspected for retired modules.

Commands:

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
mkdir -p .agent-ops/verification
export COVERAGE_FILE="$PWD/.agent-ops/verification/coverage"
uv run coverage erase
uv run coverage run -m pytest -q
uv run coverage combine
uv run coverage report
uv build --offline --out-dir .agent-ops/verification/dist
```

Agent-owned decisions are verified through deterministic test agents. The Codex bridge is verified against a controlled executable and the locally installed CLI's command interface; these checks do not claim a paid-model run or a live GitHub-provider run. OTEL output is a local OTLP JSON log export, not network collector delivery.
