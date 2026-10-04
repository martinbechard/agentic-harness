# Work Item Provider protocol

The optional Work Item Provider protocol answers one read-only question without
starting an agent: how many stored Ready candidates exist in this selection scope?
It returns only a count. The access agent still reconciles lifecycle evidence,
checks dependencies and human decisions, chooses the appropriate items, and
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

Status folders:

```yaml
work_item_provider:
  type: file
  layout: status-folders
  paths: [backlog/ready]
  timeout: 30
```

Legacy folders with explicit lifecycle fields:

```yaml
work_item_provider:
  type: file
  layout: status-field
  paths:
    - backlog/defect-backlog
    - backlog/feature-backlog
    - backlog/analysis-backlog
    - backlog/investigation-backlog
```

Paths are directories relative to `project`, or absolute directories. Configure
only the actual active queues; do not point this at the entire backlog, archives,
Future Ideas, or a directory of documents. Each configured directory must exist;
an empty directory returns zero, while a missing directory is an error. Settings
are loaded on startup, so restart after changing the configuration.

The implementation reads Markdown files recursively. Work item IDs are filename
stems, matching the file provider convention. `index.md` and `README.md` are
coordination documents and are not counted. Duplicate IDs from overlapping paths
are counted once. The metadata header ends at the first level-two heading, so
examples or historical `Status: Ready` text cannot reopen an item.

In `status-folders` mode, files are Ready by location. A retained `Status` header
must agree exactly with `Ready`; disagreement fails the check. In `status-field`
mode, only an explicit `Status: Ready` header counts. No status, Running, Starting,
Holding, Blocked, User Action Required, or terminal states do not count. Other
record formats require another protocol implementation; this is not a generic
Markdown interpretation engine.

An epic is a project-relative or absolute directory, such as
`backlog/feature-backlog/release`. The count uses directory membership. Projects
whose epics span status folders through index links should retain agent-only mode
for epic runs until a matching scope implementation exists.

Counts read the configured checkout, without writes or Git operations. Newer
worktree checkpoints, prerequisite completion, and conflicting human decisions
are intentionally checked by the access agent after a positive count. A stored
Ready candidate with an unresolved dependency can therefore still trigger a call.

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
