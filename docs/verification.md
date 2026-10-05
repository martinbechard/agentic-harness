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
| UC-007: user action | Development agent records question/status; reports outcome; no automatic answer selection | `test_nondelivery_checkpoint_is_published_without_product_delivery[user_action-user-action-required]` |
| UC-008: concurrent backlog | Capacity-limited assignments with active exclusions and distinct worktrees | Scheduler concurrency and invalid-batch tests; real parallel subprocesses |
| UC-009: epic dependencies | Epic selection and provider-owned readiness/completion | `test_parallel_delivery_dependencies_defects_and_merge`: B depends on merged A |
| UC-010: spare capacity | Second access query outside epic after epic items receive priority | `test_concurrency_epic_priority_and_outside_capacity`; real outside-item delivery |
| UC-011: discovered defects | Delivery instructions delegate recording and blocking fixes | Separate-project blocking/unrelated defect creation, blocking fix, and publication by merge |
| UC-012: integration | Serialized merge-agent invocations on completion and interval | `test_merge_is_serial_and_completion_trigger_is_retained`; combined-test failure rejects merge; delayed-commit race regression |
| UC-013: interrupted recovery | Inspect local item, reuse worktree/branch, instruct assessment before continuing | `test_real_process_recovery_and_retry[interrupted]`; stopped-parent/live-child cleanup |
| UC-014: restart if not resumable | Replacement instructions preserve unrelated work while restarting | `test_real_process_recovery_and_retry[restart]` |
| UC-015: explicit human decision | Configured provider agent, exact observation binding, stable decision identity, shared access lock, validated result | Synthetic decision CLI tests: allow/cancel/answers, stale refusal, worktree handoff, duplicate clicks, lost acknowledgement, old-runner refusal |

## Principles and exclusions

- **P-001:** All provider operations go through the access-agent command. Normal status changes occur in the development-agent command. The Codex adapter supports an independently configured model and reasoning effort per role.
- **P-002:** CLI prompts address the human user explicitly. Pause/resume is exercised through actual stdin; user-action questions are stored by the development test agent.
- **P-003:** `test_every_activity_has_matching_console_log_and_otel_record` checks identical content and timestamps across console, JSONL activity log, and OTLP JSON log export. Actual process output streaming and incomplete UTF-8 are covered separately.
- **P-004:** Item-specific access commands and development commands run in the item's worktree. Separate-project tests verify committed waiting checkpoints publish lifecycle-only state without merging unfinished product files. Delivered product records still publish through Git merge. Merge instructions explicitly require reconciliation of conflicting attempt information.
- **P-005:** Tests cover transient retries, nontransient holding, timeout exhaustion, and recovery. Two automatic retries are shared by delivery and interruption recovery within the foreground run.
- **P-006:** Integration fixtures initialize a separate temporary Git project outside this checkout, with status folders, work item creation/update, dependencies, defect recording, independent worktrees, combined tests, and delivery merges. No production backlog is used.
- Manual item selection, automatic selection of human answers, direct provider transactions, dashboards, harness-owned review/proof gates, and retired transports have no implementation or public command. The wheel contains only `__init__`, `__main__`, `adapter`, `cli`, `codex`, `decision`, `engine`, `process`, and `provider_lock`.

## Verification results

Verified on 2026-10-03 with Python 3.12:

- 168 automated tests passed; one opt-in live test skipped in the default suite, including real agent subprocesses, worktree recovery, process-group cleanup, parallel delivery, dependency order, combined-result testing, CLI controls, and structured adapter results.
- Coverage: 100% of 678 production statements and 206 branches. The existing 100% gate remains enabled.
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

The automated suite verifies agent-owned decisions through deterministic test agents and runs the real Codex Python SDK against a controlled app server. Earlier [live Codex verification](live-codex-verification.md) passed filesystem delivery, tested integration, and interrupted-agent recovery with the former CLI adapter; those runs do not establish SDK end-to-end delivery. No live GitHub-provider run was performed. OTEL output is a local OTLP JSON log export, not network collector delivery.

## Deployment regressions

### Mechanical thread cleanup (2026-10-04)

`tests/test_cleanup.py` covers strict completion-age eligibility, recent activity,
active/failed/interrupted/empty/undated turns, pagination before mutation,
rechecking resumed threads, per-thread failures, disabled cleanup, default timing,
configured age, cancellation, and one-shot outcomes. `tests/test_cli.py` checks
configuration validation, disabling cleanup without starting agents, and matching
DEBUG filtering/severity in console, JSONL, and OTEL output.

The live one-shot pass against `/Users/martinbechard/dev/dev-methodology` archived
513 chats. An independent indexed listing confirmed all 513 as archived and 24
chats remaining unarchived. One additional archive was refused by Codex because
the thread had an active writer; it remained unarchived and produced an ERROR
event and a nonzero command exit. No agent turns or backlog delivery were started.
Local logs and readback evidence are in `.agent-ops/thread-cleanup-verification/`.
The already-running installed harness was not restarted or upgraded by this test.

The final full suite passed: 242 tests passed and one opt-in test skipped.
Coverage reached 100% of 969 statements and 304 branches. One existing subprocess
timeout test exceeded its wait budget during the instrumented full run; its
isolated instrumented rerun passed. Ruff lint, formatting, and diff whitespace
checks passed.

### Desktop archive notifications (2026-10-04)

The earlier archive readback proved persistence, not removal from the visible
sidebar. `desktop.py` now sends the same version-2 `thread-archived` IPC broadcast
used by the installed desktop, following an initialization handshake. Each cleanup
pass enumerates existing project archives and replays notifications, including
previous runs. The local socket must belong to the current user in a private
directory; notification work is bounded to five seconds. This is an isolated
internal desktop interface and can require changes after desktop upgrades.

The full suite passed with 250 tests and one opt-in skip. Focused tests after adding
archived-list pagination coverage passed; combined coverage is 100% of 1022
statements and 318 branches. Tests exercise a real temporary Unix socket,
initialization, unrelated frames, versioned broadcasts, rejected initialization,
invalid framing, disconnects, missing app, invalid socket, replay, and retry logging.

The live repair enumerated archived dev-methodology root and worktree chats and
sent 975 cache-invalidation notifications. Evidence is
`.agent-ops/thread-cleanup-verification/ui-notifications.json`. Notifications have
no UI-render acknowledgment. Computer Use refused inspection of Codex itself;
visual disappearance was requested from the user and is not yet independently
verified. The supported desktop archive tool also successfully reapplied archive
to one already-archived chat before the bulk notification test.

### Adapter protocol tests

`test_exact_instructions_are_received_by_sdk_server` compares complete instructions, assignment data, and output schema against JSON-RPC messages received by a local server through the real SDK. `test_codex_sdk_transmits_instructions_schema_and_streams_activity` exercises the subprocess entry point and verifies SDK options and streamed output. Failure tests cover failed/interrupted turns, missing and malformed responses, non-object results, and transport disconnection. The cancellation test confirms the SDK app server is in the invocation process group and is reaped when the invocation stops.

`test_another_adapter_uses_same_request_result_and_event_contract` supplies an unrelated backend through the same interface, with no SDK types in the contract. The temporary context-file option and CLI configuration overrides are removed.

`test_live_codex_receives_instructions` is an opt-in authenticated SDK test in a disposable project. A random receipt is supplied only through developer instructions, not through the turn input or schema. Codex returned the exact receipt in a completed structured response on 2026-10-02 using SDK 0.160.0 and gpt-6-luna. This demonstrates receipt and use for that test, not arbitrary policy compliance. Run it with:

```sh
RUN_CODEX_LIVE=1 uv run pytest -q tests/test_adapter.py::test_live_codex_receives_instructions
```

The ordinary suite skips that paid model call. Backlog execution remains stopped.

`test_provider_reconciles_legacy_candidate_and_excludes_unpublished_work` uses a separate Git project and deterministic provider agents. An explicitly interrupted candidate survives provider-owned reconciliation and the tested delivery merge; its commit remains an ancestor of main. Unrelated local files remain uncommitted. A live assignment is not duplicated and an unreviewed committed candidate is not merged. The fixture's explicit interruption record stands in for provider knowledge; this does not prove a model can diagnose arbitrary historical attempts.

Full regression run: 168 passed, one opt-in live test skipped; 100% statement and branch coverage. The live test passed separately. Ruff, diff checks, and package builds passed.


## Monitoring and process events

Heartbeat tests observe actual emitted events during idle, paused, and slow-provider operation with a concurrent stalled merge agent. Active heartbeats identify the assigned invocation. A deliberate event-loop stall produces no heartbeat; cancellation stops further events and records shutdown. Supervisor failure records error shutdown.

Subprocess tests distinguish spontaneous exit without a result, reported success/failure, invalid results, requested termination, and routine group cleanup. They check invocation, role, item, PID, exit code, registry removal, and exactly one exit event. The separate-project timeout test matches each timeout event to its actual development invocation. Console/JSONL/OTEL equality now includes a heartbeat.

Verification: 96 tests passed, one authenticated SDK test skipped in the default suite; 100% of 523 statements and 148 branches. SDK transport did not change, so the previously passing live instruction-receipt test was not repeated. No production backlog was launched or restarted for this change.


## Human decision command

`tests/test_decision.py` invokes the real CLI and process boundary against a separate synthetic provider. That provider, not the harness, owns its records, path/NUL/bytes revisions, history, authoritative workspace and state changes. Tests verify exact approval, explicit cancellation, selected/free-text answers, rejection of stale revisions/questions/candidates/identities, refusal to infer a choice from Allow, and rejection of superseded worktree sources. A current worktree handoff changes only that source, preserving main and candidate files.

Concurrent identical CLI submissions produce one durable decision; the second returns `already_applied`. A provider that persists and loses its acknowledgement produces `unknown`/`persisted:null`; retrying the unchanged submission reads the prior decision without duplicating it. Mismatched provider acknowledgements never become success. Additional tests verify actual cross-process serialization with an ordinary provider status call, cancellation while waiting for a lock, stale/incompatible lease refusal, and second-runner rejection.

The provider-agent instruction requires native conditional-update safeguards and persisted readback. Synthetic evidence verifies the command/contract and the deterministic fixture's behavior; it does not establish that every model/provider implements atomic updates correctly. No real approvals, cancellations, or backlog runs were performed. Dashboard activation requires safe upgrade of the sole runner first.

Current validation: 168 passed, one opt-in live SDK test skipped, 100% of 678 statements and 206 branches; Ruff, formatting and diff checks passed. SDK transport is unchanged.


## Decision recovery and publication

The 2026-10-03 suite covers unchanged requests after unrelated history updates, refusal of changed decision scope, conditional-update races, claim-free native transactions, binary permission grants, historical-resolution reporting, and explicit cancellation after historical approval. Actual subprocess tests observe waiting progress before lock acquisition and bound queue/execution time. A running timeout kills and reaps the provider; replay recovers a prior write with the same decision ID.

`test_published_answer_reaches_preserved_workspace_before_delivery` uses a disposable Git repository and actual access/development subprocesses. It verifies an exact human answer is committed on the authoritative branch while unrelated staged files remain staged, reconciled into a preserved candidate worktree, and read by the development process before delivery. Candidate ancestry and local delivery evidence survive. A conflicting pending question prevents dispatch. A failing commit hook leaves a partial write unconfirmed and undispatchable; recovery with the original request publishes it once and preserves the request bytes and ID. Selection is tested with a new provider client without an in-memory workspace registry.

These deterministic provider fixtures verify the boundary and intended provider behavior. They do not prove that a live model follows every publication/reconciliation instruction. This release makes that agent contract explicit; it does not add provider-specific Git transactions to the harness. No real decisions were replayed and no production runner was restarted during this work.

## Waiting checkpoint publication (2026-10-04)

Selection now explicitly owns provider-mediated lifecycle-only publication of proven
committed waiting checkpoints before returning ready items. This does not add a Git
provider implementation to the harness or authorize unfinished product integration.
Questions, decisions, assignments, candidate evidence and recorded blocker details
must survive publication; ambiguous records remain excluded. A Ready label or newer
partial answer does not establish that a question has been resolved.

The full automated suite passed 254 tests with two authenticated tests skipped;
statement and branch coverage remained 100% (1022 statements, 318 branches).
After tightening the partial-answer boundary, 45 focused process/project tests
passed. Ruff and formatting checks passed. The disposable Git fixture covers
waiting-state publication, stale branches after completion, newer answers,
conflicting revisions, repeat-scan idempotence and unrelated staged-file preservation.

The opt-in `test_live_access_reconciles_waiting_without_product_merge` exercises
the configured Luna/high access agent with real Git worktrees and the same
selection instructions. It checks complete pending-question metadata, candidate
preservation, no product merge, newer conflicting decisions, and a fresh unchanged
second scan. Its first run exposed dispatch of an unresolved partial answer;
the access instruction was corrected rather than accepting that result. Run with:

```sh
RUN_CODEX_LIVE=1 uv run pytest -q tests/test_project.py::test_live_access_reconciles_waiting_without_product_merge
```

The deterministic fixture verifies its provider contract, not arbitrary model
compliance. Live acceptance and deployed-run receipts are retained separately.

The corrected authenticated live test passed on 2026-10-04 in 263.77 seconds.
Two fresh access invocations exercised initial publication and unchanged replay.
Invocation receipts are `14d0e40b68db45c0baa81d8f445d4a2f` and
`5ca3c59075184c88ba1b27cb145948f2` under
`.agent-ops/live-lifecycle-test-v2/test_live_access_reconciles_wa0/state/`.
All four source records, candidate branches, product isolation, staged-file
preservation and repeat-scan assertions passed. This evidence is bounded to
these cases; it does not claim arbitrary provider/model correctness.

### Work Item Provider protocol (2026-10-04, 0.1.0a56)

The optional read-only protocol adds a count check before Ready selection. It is
an exception to P-001's agent-only observation path; selection, reconciliation and
provider transactions remain agent-owned. Configuration chooses `file`, `github`,
or the compatible `agent` fallback. See [the contract](work-item-provider-protocol.md).

- Full Python 3.12 suite: **302 passed, 2 opt-in live tests skipped**.
- Branch coverage: **100%**, all 1,145 statements and 386 branches; no exclusions.
- Ruff lint, formatting, and Git whitespace checks passed.
- Source distribution and wheel built for **0.1.0a56**.
- Installed wheel on Python 3.13: **131 passed, 1 opt-in live test skipped** across
  provider, engine, CLI, and adapter tests. Imports were verified under the isolated
  environment's `site-packages`, rather than the editable source checkout.

`tests/test_work_item_provider.py` checks both filesystem layouts, exact header
state, ignored history/index files, epic scope, active exclusions, missing and
unreadable queues, GitHub pagination/PR exclusion/milestones/identity forms, malformed
responses, cancellation, and process reaping. A real configured CLI subprocess
performs repeated empty polls without starting selection, then observes a new Ready
file and starts the access agent. The agent may still return no selected items.

`tests/test_engine.py` checks zero/positive/invalid counts, failure and timeout,
full capacity, active IDs, epic/outside scope, pause during a count, and retained
agent selection. Existing tests cover agent-only fallback, retries, recovery,
decisions, merge behavior, and lifecycle reconciliation.

Release evidence is under `.agent-ops/work-item-provider-release/`; the installed
wheel environment is `.agent-ops/work-item-provider-final/installed`. GitHub API
behavior is verified with controlled responses and the documented CLI contract;
no live GitHub account/repository admission test was performed.

### Eligible Ready count correction (2026-10-04, 0.1.0a57)

The a56 filesystem count above was incorrect for prerequisite-blocked Ready
records. It counted stored status and disagreed with the dashboard. The harness
now invokes the file provider's configured count command and contains no duplicate
Markdown status parser. Dev-methodology's command reuses the dashboard's
`codex_backlog.provider.inventory` eligibility calculation, then applies scope and
active-item exclusions. The command returns only `ready_count`.

- Full Python 3.12 regression suite: **311 passed, 2 opt-in live tests skipped**.
- Coverage: **100%**, all 1,137 statements and 372 branches, no exclusions.
- One additional real-process regression verifies timeout kills a helper process
  after its parent exits while leaving its output pipe open.
- Installed a57 wheel, Python 3.13: **141 passed, 1 opt-in live test skipped** across
  provider, scheduler, CLI and adapter tests, including that additional regression.
- Lint, formatting, whitespace checks, source distribution and wheel build passed.
- Installed harness provider calling the actual dev-methodology observer returned
  **0, 0, 0** on three consecutive reads. All **340** backlog Markdown files kept
  their hashes. This is read-only observation, not a synthetic production backlog.

The CLI regression starts with six stored Ready records whose provider eligibility
is false: repeated polls make no selection call. When the observer reports an
eligible item, selection runs. Provider tests cover exact input scope, malformed
counts, command errors and process cleanup. The provider repository separately tests
real prerequisite and series-order resolution through its shared inventory.

Evidence: `.agent-ops/provider-eligibility-fix/coverage.json`,
`combined-provider-check.json`, and the a57 wheel under `dist/` in that directory.
A file provider command is now required; missing commands fail configuration
validation instead of falling back to the faulty stored-status count.

### Periodic Blocked recovery (2026-10-05, source checkout)

UC-016 adds a provider count every 900 seconds by default, followed by one
serialized access-agent recovery invocation only for a positive count. Tests cover
count validation, file command scope and backwards compatibility, GitHub labels,
pause/completion, timeout, pending exclusions, no overlapping recovery, supervisor
failure, and shutdown. A real subprocess test verifies the count-to-recovery flow
and process-group cleanup on timeout. Human-decision validation also rejects an
old resolution as satisfaction of a new blocked retry.

- Full regression run: **360 passed, 2 opt-in live tests skipped**.
- Additional historical-resolution safeguard test: **1 passed**.
- Combined coverage: **100%**, all 1,202 statements and 394 branches.
- Ruff lint, formatting and whitespace checks passed.

Coverage evidence: `.agent-ops/blocked-recovery-coverage.json`. Tests use isolated
fixtures and subprocesses, not paid agents or a live production backlog. No installed
release or external file observer was upgraded by this change; file observers must
implement the new Blocked request contract before recovery can operate.

The coordinated upgrade packages this change as **0.1.0a58**. The wheel and source
distribution built successfully; `uv lock --locked` passed. An isolated Python
3.12 installation imported the wheel from `site-packages` and passed **210 tests
with 1 opt-in live test skipped** across engine, provider, process, CLI and adapter
tests. Distribution files are in `.agent-ops/blocked-recovery-release/`.
Dev-methodology owns observer deployment and safe runtime activation.
