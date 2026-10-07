# Work Item Provider protocol

The optional Work Item Provider protocol answers one read-only question without
starting an agent: how many eligible Ready items exist in this selection scope?
It returns only a count. The access agent still reconciles lifecycle evidence,
rechecks current dependencies and human decisions, chooses the appropriate items, and
prepares their worktrees. A positive count is permission to ask that agent, not
permission to deliver a particular item. It can legitimately select no items.

## Contract

`backlog_harness.work_item_provider.WorkItemProvider` defines:

```python
async def ready_count(
    *, epic: str | None, outside: bool, excluded: list[str]
) -> int:
    ...
```

The implementation is bound to the configured project/provider at startup.
`epic=None` means the whole configured backlog. Otherwise, `outside=False`
selects the epic and `outside=True` selects its complement. `excluded` contains
active work item IDs. Results are nonnegative integers; booleans are invalid.
Implementations must perform no lifecycle writes or agent/model calls.

Before each ready-item selection, if capacity is available, the harness queries
the implementation. Zero skips the access agent until a later poll. A positive
count allows the existing selection call, whose capacity and safety checks remain
in force. No count is cached or used to allocate work. Reads and subsequent
selection are not atomic: the access agent must verify current state again.

A read failure, malformed result, or timeout emits `ready_count_failed` and skips
selection for that poll. It never becomes an empty count or an agent fallback.
Successful reads emit `ready_count` with the count and scope. Both events use the
normal console, JSONL and OTEL path. The default count timeout is 30 seconds.

Omitting `work_item_provider`, or setting `type: agent`, retains agent-based
selection without a count check. This supports providers without an implementation.
Unknown configured types or settings fail configuration validation. Provider
selection is explicit and is never inferred from a Git remote or hosting service.
The same provider configuration is included in every agent request, so selection
and delivery use the configured provider scope. A new implementation implements this method and registers its configuration in
`configured_provider`; no scheduler change is needed.

This gate applies only to **new Ready selection**. Epic completion, integration,
exception updates, explicit human decisions, and already assigned retries/recovery
retain their existing calls. Zero Ready items does not establish epic completion
or absence of work to integrate. A zero count also means the access agent's
selection-time lifecycle reconciliation does not run during that poll; it resumes
when Ready candidates are published. Dependency-state reconciliation still runs through the merge agent at startup,
on its regular interval, and after each published completion, independently of
this Ready count gate. Counts themselves never change lifecycle state.

## Filesystem implementation

The file provider invokes its configured **eligibility observer**, without a shell
or model. The observer must reuse the provider's readiness calculation, including
prerequisites and required series order. Counting stored `Status: Ready` headers is
not an implementation of this contract: such records can be effectively Holding
or Blocked. The harness no longer contains a second Markdown lifecycle parser.

```yaml
work_item_provider:
  type: file
  command:
    - /absolute/path/to/python
    - /absolute/path/to/provider-ready-count.py
  layout: status-field
  paths:
    - backlog/defect-backlog
    - backlog/feature-backlog
    - backlog/analysis-backlog
    - backlog/investigation-backlog
  timeout: 30
```

`command` is required and is a nonempty argument array. Version 0.1.0a56 file
configurations must add this observer command before upgrading; missing commands
fail startup rather than reverting to the incorrect stored-state count.
`layout` and `paths` describe the provider's queues and are passed to the observer.
They default to `status-folders` and `[backlog/ready]`; an observer must reject
layouts it does not support. Paths are relative to the configured project or
absolute. Configure actual active queues, excluding archives and Future Ideas.

The observer runs with the configured project as its current directory and receives
one JSON object on standard input:

```json
{"epic":null,"outside":false,"excluded":[],"paths":["backlog/feature-backlog"],"layout":"status-field"}
```

It writes exactly this result shape to standard output and exits successfully:

```json
{"ready_count":0}
```

Diagnostics go to standard error. Counts must be nonnegative integers, never
booleans. Read or validation failures must exit nonzero; they must not report zero
or fall back to an agent. Timeout/cancellation stops and reaps the command process
group, including helper processes. The observer must not mutate provider records.

The dev-methodology observer and dashboard use the same configured provider
implementation. The provider owns inventory, eligibility, queue and epic scope,
history, source evidence, artifact inspection, decision locators, and questions
recorded in assigned worktrees. Its filesystem implementation owns Markdown and
Git interpretation. The dashboard consumes normalized observations and retains
presentation, runtime telemetry, scheduling controls, and decision submission.
It must not reconstruct provider facts by reading backlog files itself.

The count command remains a small adapter to that implementation; its JSON
contract does not expand to include dashboard details. It counts only eligible
records within the configured queue paths and epic scope, excluding active item
IDs. Thus six stored Ready children waiting for prerequisites yield **zero**,
matching the Ready dashboard metric. Resolving prerequisites includes looking
outside the selection scope; limiting selected paths must not hide predecessor
records. Compare counts for the same scope and observation time: the dashboard
can show a wider inventory than a configured harness selection scope.

Observation capabilities are provider-specific. Supporting this count protocol
alone does not imply dashboard history, artifacts, or decision support. An
unsupported configured dashboard provider must fail clearly, rather than read
filesystem records as a fallback. The GitHub count implementation below does not
by itself provide GitHub dashboard observation capabilities.

Selection still revalidates changing state after a positive count. Priority,
compatibility between items, and final worktree preparation remain with the agent.

## GitHub implementation

```yaml
work_item_provider:
  type: github
  repository: owner/repository
  ready_label: 'Status: Ready'
  executable: gh
  timeout: 30
```

This implementation is for repositories whose Ready state is represented by one
explicit label. It uses the installed, authenticated GitHub CLI to read open issues
with that label, follows every page, excludes pull requests, and returns a unique
issue count. It does not infer a Ready label or read GitHub Projects status fields.
For a Projects-based workflow without a matching implementation, use `type: agent`.
An epic is an exact milestone title. Active IDs can be issue numbers, `#number`,
`owner/repository#number`, or issue URLs. The access agent must use that same
repository, label, milestone convention, and one of these identity forms.

The implementation uses the documented [repository issues endpoint](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
and [GitHub CLI pagination](https://cli.github.com/manual/gh_api).
Authentication, rate-limit, command, and response errors defer selection. The CLI
process is killed and reaped when the count is cancelled or times out. No live
GitHub project is required for the automated tests, and those tests do not establish
live account access.

## Blocked count and recovery

Implement `blocked_count(*, epic, excluded) -> int` alongside `ready_count`.
This counts the provider's **Blocked** lifecycle state, not prerequisite-ineligible
Ready items, Waiting, Holding, User Action Required, or terminal states. Counts are read-only;
recovery uses a separate access-agent invocation only after a positive count.
An epic restricts the count to that epic; null covers all configured queues.
There is no outside-epic recovery pass. Active and pending retry IDs are excluded.

File observers receive the following request on the same command's standard input:

```json
{"action":"blocked_count","epic":null,"outside":false,"excluded":[],"paths":["backlog/blocked"],"layout":"status-folders"}
```

Return only `{"blocked_count": N}` with a nonnegative integer. Requests without
`action` retain the existing Ready contract. The `paths` in a Blocked request come
from optional `blocked_paths`, defaulting to `paths` for status-field layouts and
`["backlog/blocked"]` for status-folders. Use existing active queues. The harness
never parses work item files or substitutes an access agent after observer errors.
Upgrade existing Ready-only commands before relying on blocked recovery.

GitHub uses the same pagination, issue exclusions, identity aliases and milestone
scope as Ready counting, but requires an explicit `blocked_label` configuration.
The harness does not infer that label from `ready_label`. Missing configuration is
a count error, not an empty queue. Both paths share `work_item_provider.timeout`.
Agent-only configuration uses the access `blocked_count` action with the same count
schema, under the provider lock and the count timeout.

## Waiting lifecycle

Waiting is a provider state for valid unfinished prerequisites, excluded from both
Ready and Blocked counts. No `waiting_count` operation or additional observer
command is needed. The merge agent reconciles Waiting items even if Ready counts
stay zero, and promotes only those with all dependencies authoritatively Completed.
Use the provider's Waiting folder, status field, or label convention. Dependencies
outside the selected queue/epic and in completed archives still require lookup.
Agents migrate dependency-only Ready or legacy Blocked items; read-only observers
must never perform that migration. Invalid dependencies and genuine blockers stay
Blocked for periodic recovery. Existing observers/providers must support this
state distinction; harness tests do not establish compatibility with a separately
installed provider implementation.
