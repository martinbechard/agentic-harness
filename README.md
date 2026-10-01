# Agentic Harness

A foreground Python application that lets agents organize delivery with their CLI's native delegation while enforcing provider transitions, independent review, exact candidate identity, checks, delivery, and usage holds.

The first adapter supports Codex CLI 0.159.2. The selected workflow uses a file backlog, main-branch delivery, explicit resource coordination `none`, and either SOLO or MULTITASK. MULTITASK requires `project_setup.concurrent_tasking: true`, independent candidate clones, explicit item scopes, and serialized provider and integration transactions. Projects selecting a resource-claim helper need their existing helper boundary integrated before this route can execute them.

Install Python 3.12 or newer and Codex CLI, then use:

```sh
uv sync
uv run agentic-harness --config /absolute/path/config.yaml validate
uv run agentic-harness --config /absolute/path/config.yaml app
```

The asynchronous terminal supports `run --until-terminal`, `run --watch`, `pause`, `resume`, `stop`, `reconcile`, status, item and session inspection, exact question answers, and usage-hold review. The noninteractive `run` and `run-item` commands use the same application. A read-only dashboard starts with `dashboard --port 8767` (use port 0 to select a free port).

See the [operator guide](docs/operator-guide.md), [implementation plan](IMPLEMENTATION-PLAN.md), and [verification progress](docs/verification/progress.md). The progress record distinguishes tests and live evidence from outstanding acceptance work.
