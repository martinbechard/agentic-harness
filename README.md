# Agentic Harness

Run ready backlog items through development agents. A work item access agent chooses ready work using the project's provider and dependencies. Development agents update their own items; a separate merge agent checks and integrates finished work.

The scope is [the approved functional specification](docs/functional-spec.md). The runtime supports its fifteen use cases: dispatch, bounded retries, waiting, pause/resume, user-action outcomes, parallel backlog and epic delivery, defect recording, integration, interrupted-work recovery, and explicit human decisions on pending requests.

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
access_timeout: 120
decision_timeout: 120
log_level: INFO
thread_cleanup:
  enabled: true
  interval: 900
  completed_age: 3600
```

Use a model available to your Codex account. The bundled adapter uses the official `openai-codex` Python SDK and its matching local app-server runtime. It reuses Codex authentication and the configured sandbox; permission escalation is denied for these unattended invocations. Optional arguments are `--model` and `--effort`. Role instructions are supplied through the SDK's `developer_instructions` parameter and assignments through its turn input. Agents use project instructions and provider/delivery skills for backlog and integration conventions. There is no context-file or shell-wrapper requirement.

```sh
uv run agentic-harness --config config.yaml
uv run agentic-harness --config config.yaml --epic release-folder
```

The human user can type `pause` or `resume` followed by Enter. Pausing stops new dispatch and lets current attempts finish. Ctrl-C exits and reaps spawned processes. Backlog processing keeps polling; epic processing ends when the access agent confirms all epic items are delivered, then drains existing outside work and the final merge check.

All harness activities and agent output appear on the console, in `activities.jsonl`, and as OTLP JSON log entries in `otel.jsonl`. Per-invocation folders retain the request, result, and raw output. No collector or dashboard is required.

Mechanical Codex thread cleanup runs at startup and every 15 minutes while the
harness runs, including while delivery is paused. It archives unarchived chats
whose working directory exactly matches `project`, whose latest turn completed
more than one hour ago, and which have no newer activity. It re-reads each chat
before archiving. Failed, interrupted, active, empty, and undated turns are kept.
Chats in other directories, including separate worktrees, are outside this scope.
Archiving does not delete chats or change work-item status. No agent or model is used.

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
