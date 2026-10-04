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
when Ready candidates are published. This is not a background lifecycle repair
service.

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

The dev-methodology observer calls the same `codex_backlog.provider.inventory`
function used by its dashboard. It counts only eligible records, then applies
configured queue paths, epic-directory scope, and active-item exclusions. Thus six
stored Ready children waiting for prerequisites yield **zero**, matching the
Ready dashboard metric. Resolving prerequisites includes looking outside the
selection scope; limiting selected paths must not hide predecessor records.

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
