# Agentic Harness

Run ready backlog items through development agents. A work item access agent chooses ready work using the project's provider and dependencies. Development agents update their own items; a separate merge agent checks and integrates finished work.

The scope is [the approved functional specification](docs/functional-spec.md). The runtime supports its fourteen use cases: dispatch, bounded retries, waiting, pause/resume, user-action outcomes, parallel backlog and epic delivery, defect recording, integration, and interrupted-work recovery.

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
access_timeout: 120
```

Use a model available to your Codex account. The bundled adapter uses the official `openai-codex` Python SDK and its matching local app-server runtime. It reuses Codex authentication and the configured sandbox; permission escalation is denied for these unattended invocations. Optional arguments are `--model` and `--effort`. Role instructions are supplied through the SDK's `developer_instructions` parameter and assignments through its turn input. Agents use project instructions and provider/delivery skills for backlog and integration conventions. There is no context-file or shell-wrapper requirement.

```sh
uv run agentic-harness --config config.yaml
uv run agentic-harness --config config.yaml --epic release-folder
```

The human user can type `pause` or `resume` followed by Enter. Pausing stops new dispatch and lets current attempts finish. Ctrl-C exits and reaps spawned processes. Backlog processing keeps polling; epic processing ends when the access agent confirms all epic items are delivered, then drains existing outside work and the final merge check.

All harness activities and agent output appear on the console, in `activities.jsonl`, and as OTLP JSON log entries in `otel.jsonl`. Per-invocation folders retain the request, result, and raw output. No collector or dashboard is required.

## Agent interface and verification

[Operator and agent protocol](docs/operator-guide.md) describes custom commands and role boundaries. `AgentAdapter.run` accepts ordinary Python instructions, request data, output schema, working directory, and an activity callback; it returns a result dictionary. Only `codex.py` imports the Codex SDK. Another backend implements that contract and calls `adapter.execute`; select its entry point in the role command configuration without changing the scheduler. [Verification](docs/verification.md) maps each use case to its tests.

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Integration tests build a separate disposable Git project and exercise actual subprocesses, filesystem work items, worktrees, and merges. They do not run paid models or modify this repository's backlog.

Manual item selection, human-answer management, dashboards, review gates owned by the harness, and direct provider transactions are absent. Earlier plans and evidence are retained only in [the historical archive](docs/archive/previous-workflow/README.md).
