# Agentic Harness

Run ready backlog items through development agents. A work item access agent chooses ready work using the project's provider and dependencies. Development agents update their own items; a separate merge agent checks and integrates finished work. Before selection, the access agent reconciles committed waiting checkpoints into the published backlog without merging unfinished product changes. Newer human decisions and delivered completion take precedence over stale branch records; conflicts remain undispatched and are reported.

The scope is [the approved functional specification](docs/functional-spec.md). The runtime supports its sixteen use cases: dispatch, bounded retries, waiting, pause/resume, user-action outcomes, parallel backlog and epic delivery, defect recording, integration, interrupted-work recovery, and explicit human decisions on pending requests.

## Run

Requires Python 3.12+ and macOS or Linux. Install with `uv sync`.

Create a configuration using absolute executable paths:

```yaml
project: /absolute/path/to/project
state: /absolute/path/to/harness-state
access:
  - /absolute/path/to/agentic-harness/.venv/bin/python
  - -m
  - backlog_harness.codex
  - --model
  - gpt-6-luna
  - --effort
  - high
development:
  - /absolute/path/to/agentic-harness/.venv/bin/python
  - -m
  - backlog_harness.codex
merge:
  - /absolute/path/to/agentic-harness/.venv/bin/python
  - -m
  - backlog_harness.codex
scheduling:
  capacity: 2
  timeout: 3600
  poll_interval: 10
  merge_interval: 60
  merge_timeout: 3600
  heartbeat_interval: 10
  blocked_interval: 900
  unblock_timeout: 3600
access_timeout: 120
decision_timeout: 120
log_level: INFO
thread_cleanup:
  enabled: true
  interval: 60
  completed_age: 300
```

Use a model available to your Codex account. The bundled adapter uses the official `openai-codex` Python SDK and its matching local app-server runtime. It reuses Codex authentication and the configured sandbox; permission escalation is denied for these unattended invocations. Optional arguments are `--model` and `--effort`. Role instructions are supplied through the SDK's `developer_instructions` parameter and assignments through its turn input. Agents use project instructions and provider/delivery skills for backlog and integration conventions. There is no context-file or shell-wrapper requirement.

```sh
uv run agentic-harness --config config.yaml
uv run agentic-harness --config config.yaml --epic release-folder
```

The human user can type `pause` or `resume` followed by Enter. Pausing stops new dispatch and lets current attempts finish. Ctrl-C exits and reaps spawned processes. Backlog processing keeps polling; epic processing ends when the access agent confirms all epic items are delivered, then drains existing outside work and the final merge check.

All harness activities and agent output appear on the console, in `activities.jsonl`, and as OTLP JSON log entries in `otel.jsonl`. Per-invocation folders retain the request, result, and raw output. No collector or dashboard is required.

Mechanical Codex thread cleanup runs at startup and every minute while the
harness runs, including while delivery is paused. It archives unarchived chats
whose working directory matches `project` or a worktree under `project/.worktrees`,
whose latest turn completed,
failed, or was interrupted more than 5 minutes ago, and which have no newer activity.
It re-reads each chat before archiving. Active, empty, and undated turns are kept.
Chats in Codex's built-in Pinned section are kept, including when pinned between
the two reads. The section ID comes from the desktop integration and is checked
in SDK thread metadata; when that metadata is unavailable, cleanup uses the
activity checks alone. With the default polling interval, eligible chats are
normally archived between five and six minutes after their last activity.
Chats in other directories are outside this scope.
Archiving does not delete chats or change work-item status. Failed runs retain their
error evidence in archived chats and harness logs. No agent or model is used.

Backlog and Merge prompts also ask agents to self-archive their own chat when done,
using the runtime archive tool when available. The structured result is still required;
an unavailable or failed archive tool leaves cleanup to the periodic pass.

Set `thread_cleanup.enabled: false` to disable cleanup. `interval` and
`completed_age` are seconds. Set `log_level: DEBUG` for check, skip, and archive-attempt
details; successful archives and pass summaries are INFO, and failures are ERROR.
The selected minimum level applies to the console and both log files.
Configuration changes take effect on the next harness start.

Run one cleanup pass without starting delivery or merge agents:

```sh
uv run agentic-harness --config config.yaml --cleanup-once
```

The command respects the off switch and exits nonzero if a cleanup operation fails.

After each pass, cleanup sends the desktop's `thread-archived` notification for
the project's persisted archives, including archives from earlier runs. This
invalidates stale sidebar entries without resuming chats or starting agents.
The desktop integration uses its internal, versioned local IPC interface
(`$CODEX_HOME/ipc/ipc.sock`, default `~/.codex/ipc/ipc.sock`), not a public SDK API.
It checks same-user ownership and bounds notification work to five seconds.
An unavailable or incompatible app produces `thread_archive_ui_sync_failed`;
persisted archives remain valid, and the next cleanup pass retries notifications.
`thread_archive_ui_notifications_sent` records dispatched notifications at DEBUG;
it is not an acknowledgment that the sidebar finished rendering. App updates can
require this isolated integration to be updated.

## Skip empty selection calls

Configure the optional [Work Item Provider protocol](docs/work-item-provider-protocol.md)
to count eligible Ready work without starting an agent. A zero count skips selection;
a positive count still asks the access agent to choose and validate work. Read
errors defer selection. Without an implementation configured, selection remains
agent-based.

```yaml
work_item_provider:
  type: file
  command: [/absolute/path/to/python, /absolute/path/to/provider-ready-count.py]
  layout: status-field
  paths: [backlog/feature-backlog, backlog/defect-backlog]
```

The file command must use the provider’s eligibility calculation, including
prerequisites; it returns only `{"ready_count": N}`. Choose only existing active
queues. Status-folder layouts use `layout: status-folders`
and `paths: [backlog/ready]`. GitHub label-based queues use `type: github`, an explicit
`repository: owner/repository`, and `ready_label: 'Status: Ready'`. See the protocol
for scope, exclusions, timeout, and format limits. Merge and epic-completion checks
retain their separate behavior.

## Wait for dependencies

`Waiting` means an item can proceed once its valid prerequisites finish. `Blocked`
means a concrete problem needs help. Provider agents record exact dependency IDs
and move dependency-only Ready or legacy Blocked records to Waiting. Missing
references, cycles, and prerequisites needing intervention remain Blocked.

After publishing each Completed item, the merge agent rechecks Waiting dependents.
It promotes an item to Ready only when **all** prerequisites are authoritatively
Completed and other readiness conditions hold. A successful worker or pending
merge does not qualify. Dependency lookup includes other queues, epics and
completed archives. Human decisions and active assignments remain protected.

The existing merge pass also reconciles dependency states at startup and on its
regular interval, even with zero Ready items or no merges. This catches completions
published elsewhere and missed updates after restart. The scheduler wakes after a
merge pass to pick up newly Ready work. Lifecycle transactions and readback remain
agent/provider responsibilities; the harness does not parse dependency graphs.
Workers can report `waiting` or `blocked` after publishing the lifecycle checkpoint;
these outcomes release capacity without retries or a transition to Holding.
Status-folder providers use their Waiting folder (`backlog/waiting` by default);
status-field and hosted providers use their configured lifecycle convention.

## Check blocked items

Every 15 minutes, the harness asks the work item provider for a Blocked count, excluding Waiting items.
A positive count starts one separate unblock invocation using the configured
`access` command. Agent-only providers use an access-agent count; configured file
and GitHub providers count without a model call. Errors are logged and retried on
the next interval; they never become zero counts or trigger recovery.

`scheduling.blocked_interval` (default 900 seconds) is the delay before the first
check and between completed passes. `scheduling.unblock_timeout` (default 3600
seconds) bounds recovery, including waiting for provider access. Checks do not
overlap. Pause suppresses new checks and recovery; an already started pass may
finish. Shutdown cancels and reaps recovery. The selected epic limits recovery;
without an epic it covers the configured active queues. Active and pending retry
assignments are excluded. New delivery admission waits during recovery.

The agent rechecks blockers, resolves what it can through provider conventions,
and records remaining causes and next actions. It preserves human approval gates,
candidates, worktrees and newer decisions. Corrections requiring delivery return
through normal dispatch. A successful scan does not mean every item was unblocked.

File observer commands must accept `action: blocked_count` and return only
`{"blocked_count": N}`. Existing Ready requests stay unchanged. For status-field
layouts, Blocked counts use `paths`; status-folders default to `backlog/blocked`.
Set `work_item_provider.blocked_paths` to override the Blocked scope. GitHub
configuration must add an explicit `blocked_label`, such as `'Status: Blocked'`.
An older Ready-only command or missing label produces `blocked_count_failed` and
leaves normal delivery running. See the [provider protocol](docs/work-item-provider-protocol.md).

## Submit a human decision

```sh
uv run agentic-harness --config config.yaml --decision decision.json
```

The request binds the human decision to the exact observed question, candidate and provider revision. The configured provider agent owns validation and persistence. A live upgraded runner using the same configuration/state directory is required for shared provider serialization; older runners are refused. See [decision request and result schemas](docs/operator-guide.md#human-decision-command). Submission alone never means the item was updated. Queue/start events report progress; `decision_timeout` bounds queue and provider work together (120 seconds by default). Retain the unchanged saved submission when recovering a timeout.

## Agent interface and verification

[Operator and agent protocol](docs/operator-guide.md) describes custom commands and role boundaries. `AgentAdapter.run` accepts ordinary Python instructions, request data, output schema, working directory, and an activity callback; it returns a result dictionary. Only `codex.py` imports the Codex SDK. Another backend implements that contract and calls `adapter.execute`; select its entry point in the role command configuration without changing the scheduler. [Verification](docs/verification.md) maps each use case to its tests.

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Integration tests build a separate disposable Git project and exercise actual subprocesses, filesystem work items, worktrees, and merges. They do not run paid models or modify this repository's backlog.

Manual item selection, automatic choice of human answers, dashboards, review gates owned by the harness, and direct provider transactions are absent. Earlier plans and evidence are retained only in [the historical archive](docs/archive/previous-workflow/README.md).
