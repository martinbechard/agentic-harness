# Agentic Harness

A foreground Python application that lets agents organize delivery with their CLI's native delegation while enforcing provider transitions, independent review, exact candidate identity, checks, delivery, and usage holds.

The first adapter was exercised with Codex CLI 0.159.2. Launch readiness checks authentication and the required exec/resume command options rather than requiring that exact patch version. Runtime evidence gates still determine whether an invocation completed correctly. The selected workflow uses a file backlog, main-branch delivery, and either SOLO or MULTITASK. Agents use their configured resource-claim helper. The agent-mediated provider route is selected with `provider_interaction: agent`; it is under verification for dev-methodology and is not covered by the historical six-item release acceptance. The explicitly selected `direct` fixture route retains the earlier file-fixture transactions. MULTITASK requires project authority, independent candidate clones, explicit item scopes, and serialized provider and integration transactions.

Install Python 3.12 or newer and Codex CLI, then use:

```sh
uv sync
uv run agentic-harness --config /absolute/path/config.yaml validate
uv run agentic-harness --config /absolute/path/config.yaml app
```

The asynchronous terminal supports `run --until-terminal`, `run --watch`, `pause`, `resume`, `stop`, `reconcile`, status, item and session inspection, exact question answers, and usage-hold review. The noninteractive `run` and `run-item` commands use the same application. A read-only dashboard starts with `dashboard --port 8767` (use port 0 to select a free port).

Use `answer ITEM --question-id ID --revision REV --text TEXT` to answer an exact outstanding question; the harness resumes the standard workflow. `run --until-terminal` reports `successful`, `settled_with_nondelivery`, or `blocked`; the latter two exit with code 2. Blocked reports preserve unresolved evidence and identify required attention without relaunching uncertain work.

The [pre-backlog acceptance matrix](docs/verification/pre-backlog-acceptance.md) tracks installed fixture scenarios, known gaps, and the current hold on real-backlog execution.

See the [operator guide](docs/operator-guide.md), [implementation plan](IMPLEMENTATION-PLAN.md), and [verification progress](docs/verification/progress.md). The progress record distinguishes tests and live evidence from outstanding acceptance work.

## Testing

Run deterministic tests without launching paid agents:

```sh
uv run pytest -q
```

Measure all production Python modules, including subprocess execution, with branch coverage:

```sh
mkdir -p .agent-ops/coverage
export COVERAGE_FILE="$PWD/.agent-ops/coverage/data"
export COVERAGE_RCFILE="$PWD/pyproject.toml"
uv run coverage erase
uv run coverage run -m pytest -q
uv run coverage combine
uv run coverage report
```

The coverage gate requires 100%; it is a target, not a claim that the current suite meets it.
The standard execution path is the only engine. The optional graph pilot and unregistered
ACP transport have been removed. Retained pilot records are preserved and prevent an
implicit restart under the standard workflow. Agents arrange reviews through native
delegation; review prompt formatting and duplicate pre-review test transcripts are not
acceptance gates. Required candidate, reviewer, verdict, and check evidence remains enforced.
