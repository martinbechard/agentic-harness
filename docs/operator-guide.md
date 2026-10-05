# Operator and agent protocol

## Ownership

The harness controls agent capacity, dispatch, time limits, two automatic retries per item, pause/resume, and merge invocation timing. Agents own provider details and delivery conventions. Only one merge invocation runs at a time; development and provider agents may operate while it runs.

The work item access agent selects ready items with satisfied dependencies, prioritizing the selected epic. A second query can fill unused capacity with items outside the epic. It prepares the worktree and branch and returns their identities. It must respect the active-item exclusions and consult known workspaces so unpublished filesystem status changes do not cause duplicate dispatch. Selection itself does not mark an item running. Before returning selections, it publishes proven newer committed waiting checkpoints through the provider's main-side lifecycle transaction, even when the product is not ready to merge. This includes pending questions and recorded blocker reason, owner, next action and waiting time. It compares history against newer human decisions and delivered completion, preserves unrelated files and index state, excludes ambiguous assignments, verifies published readback, and does not write duplicate updates on unchanged scans. Existing assigned worktrees must be discovered through provider records after a restart, not solely the harness's in-memory workspace list.

The development agent marks its item running, delivers it, and records its own status and delivery information. A user-action outcome means the question or action is already recorded in the item and its status is **User Action required**. The harness does not collect answers. Once the provider reports the item ready again, ordinary dispatch applies.

The discovering agent records defects through the provider. The development agent fixes blocking defects. New defect records are created in the current worktree and normally published with delivery. Waiting lifecycle checkpoints use the access-agent reconciliation described above; other provider-specific early publication belongs to the filesystem-provider specification.

The merge agent identifies eligible work according to the project's convention, prepares integration with the latest target branch, runs required tests on the combined result, merges passing work, and updates the item. A successful development outcome triggers a check, not an assumption that a PR exists. Periodic checks also run. The merge agent reconciles competing attempts' work item information even when Git merges cleanly.

## Optional ready-count check

The [Work Item Provider protocol](work-item-provider-protocol.md) can suppress
empty ready-selection invocations through `work_item_provider` configuration.
The file and GitHub label implementations return only a count; agents retain
selection and lifecycle authority. With no implementation configured, the existing
agent flow remains available. A configured provider failure defers selection.

## Commands

Configure each role as a nonempty argument array. Commands execute without a shell. Project and state paths may be absolute or relative to the configuration file. Other relative command arguments are interpreted in the agent's working directory; use absolute paths for scripts and executables.

Each invocation receives:

- `HARNESS_REQUEST`: an absolute path to a JSON request.
- `HARNESS_RESULT`: an absolute path where it must write its JSON result before exiting.
- Current directory: the project for ready/epic/merge queries; the assigned worktree for development and item-specific status/failure/hold operations.

Write results atomically. A result is accepted only after the process has stopped. Stdout and stderr are activity output, never parsed as the authoritative result. Results must be JSON objects no larger than 1 MiB.

The bundled `python -m backlog_harness.codex` entry point implements `AgentAdapter` using the official Codex Python SDK. Optional arguments are `--model` and `--effort`. The SDK starts its matching app server, inherits existing authentication and the configured sandbox, and receives role instructions through `thread_start(developer_instructions=...)`. The assignment and output schema are passed to `thread.turn`. Permission escalation uses `ApprovalMode.deny_all`; the adapter does not bypass the sandbox. SDK notifications stream to stdout and therefore enter the existing console, activity, and OTEL logs. Only a completed turn with a JSON object publishes a result.

`adapter.py` owns the backend-neutral contract, role instructions, schemas, and atomic result publication. Its `AgentAdapter.run(instructions, request, schema, cwd, emit)` contract uses Python strings, dictionaries, and paths; SDK types stay within `codex.py`. Another backend implements that contract and invokes `execute(adapter)` from its entry point. Change the configured role command to switch implementations. Backend processes must remain in the invocation process group so timeout and exit cleanup can reap them. Custom commands can also implement the file protocol directly.

The former `--executable`, `--profile`, and `--context-file` adapter flags are removed. Use the SDK adapter directly; deployment-specific wrapper scripts are not part of this interface.

## Requests and results

All requests contain `role`: `access`, `development`, or `merge`. Item identities have this shape:

```json
{"id":"item-1","worktree":"/absolute/path/to/worktree","branch":"feature/item-1"}
```

| Access action | Request fields | Required result |
| --- | --- | --- |
| `ready` | `limit`, `epic` (string or null), `outside`, `excluded`, `workspaces`, `instruction` | `{"items":[...]}` |
| `blocked_count` | `epic`, `excluded`, `instruction` | `{"blocked_count":0}` (nonnegative integer) |
| `unblock` | `epic`, `excluded`, `workspaces`, `instruction` | `{"status":"success","detail":"Actual recovery outcome"}` |
| `status` | `item` | `{"status":"running"}` or another provider-normalized status |
| `failure` | `item`, `reason` | `{"transient":true}` after recording the failure |
| `hold` | `item` | `{"updated":true}` after moving it to holding |
| `epic_complete` | `epic` | `{"complete":true}` only after all epic items are delivered |

`ready` returns at most `limit` items, without duplicate IDs or shared active worktrees. With no epic it searches the whole backlog. With an epic, `outside=false` searches that epic and `outside=true` searches the rest of the backlog. `workspaces` lists previously returned item/worktree/branch identities so the access agent can inspect unpublished updates. Provider agents must distinguish those updates from subsequent human changes back to ready.

Development requests contain `item` and `instruction`. Merge requests contain `instruction`. Both return:

```json
{"status":"success","transient":false,"detail":"Work delivered or integration checked"}
```

Allowed statuses are `success`, `failed`, and `user_action_required`. `transient` and `detail` are optional for custom commands; defaults are false and empty text. The Codex structured schema requires all three. A nonzero exit cannot claim success. Missing results leave the delivery outcome unknown; malformed results are failures.

## Failure and process handling

A development timeout kills and reaps the process group before recording the failure and deciding whether to retry. Explicit failed outcomes use the development agent's transient classification. Unknown failures use the access agent's classification. Nontransient failures and exhausted retries go to holding. Provider-update failures keep the item assigned and retry the provider operation after the polling interval.

When a process stops without an outcome, the access agent checks the item in its worktree. If it remains running, the replacement receives the same worktree and branch, an interruption notice, and instructions to assess existing work first. It may resume or discard only the interrupted attempt's work and restart, preserving unrelated work. These automatic replacement attempts share the two-retry budget.

Monitoring is independent of provider calls, so slow backlog queries do not extend development timeouts. Access and merge commands also have configurable timeouts. Merge failures are reported and the next periodic check can try again; the two-retry delivery policy applies to work items, not merge scans.

Pause prevents dispatch, including retry/recovery dispatch; it does not cancel current attempts or merge checks. Recovery tracks processes launched during the current foreground run. Harness restart/session restoration is outside this specification. Ctrl-C reaps all child process groups; worktree changes remain on disk.

## Activity output

Every harness event and stdout/stderr chunk is echoed and appended to `activities.jsonl`. The matching `otel.jsonl` entry is an OTLP `ExportLogsServiceRequest` JSON object with the service name, timestamp, run identity, event name, and associated fields. This is a local OTLP log export; the harness does not send it to a network collector. Per-invocation `output.log` retains the exact combined output bytes.


## Monitoring events

`scheduling.heartbeat_interval` defaults to 10 seconds and must be positive and finite. The supervising `Harness.run` task emits a heartbeat immediately and at that interval while it observes scheduling and merge execution. Provider/merge I/O runs in supervised tasks, so quiet agents and slow asynchronous calls do not suppress heartbeats. A blocked event loop or stopped supervisor cannot emit them. This reports monitoring liveness, not proof that an agent is making progress.

Heartbeats use the existing writer: `activities.jsonl` contains `timeUnixNano` and `run`, and the console and OTEL carry the same event fields. `monitoring_status` is `paused`, `active` (delivery assignments exist), or `idle` (no delivery assignments). An idle monitor can still have an access or merge invocation running. `active_items` lists assigned items, including those awaiting provider updates; an invocation can be null before launch. `active_invocations` lists currently running child invocations, with `invocation`, `role`, `item` (null for non-item operations), and `pid`.

Example activity-log record (timestamp and identities illustrative):

```json
{"timeUnixNano":"1790980000000000000","event":"heartbeat","run":"run-123","monitoring_status":"active","interval_seconds":10,"active_items":[{"item":"item-1","invocation":"dev-456"}],"active_invocations":[{"invocation":"dev-456","role":"development","item":"item-1","pid":12345}]}
```

Dashboard recommendation: mark a run's monitoring overdue after three heartbeat intervals (30 seconds by default), measured from the last heartbeat's timestamp. Paused and idle runs still require heartbeats. Allow for log-ingestion delay; overdue monitoring does not establish that an agent died. A run that emitted `shutdown_started` is stopping; `shutdown_completed` confirms cleanup finished. These events carry `reason`: `completed`, `cancelled` (including operator cancellation), or `error`. No further heartbeats are emitted after shutdown starts. If cleanup hangs, report incomplete shutdown rather than an unexplained heartbeat loss.

| Event | Fields beyond run/timestamp | Meaning |
| --- | --- | --- |
| `development_timeout` | `invocation`, `role=development`, `item`, `timeout_seconds`, `outcome=unknown` | Delivery deadline elapsed; emitted before killing the invocation or asking the provider to record failure. |
| `agent_exited` | `invocation`, `role`, `item`, `pid`, `exit_code`, `stop_requested` | Child has stopped; emitted once after process/output cleanup. `stop_requested=true` means the harness requested termination while it was live. A zero exit code alone does not mean delivery succeeded. |
| `agent_result` | `invocation`, `role`, `item`, `pid`, `result_state` | Result examination returned `missing`, `invalid` (unreadable/malformed/non-object/oversized), or `reported` (JSON object, subject to role validation). Cancelled result examination need not emit this event. |
| `agent_group_stopped` | `invocation`, `role`, `item`, `pid`, `reason` | Group signal sent for `stop_requested` or post-exit `cleanup`; routine cleanup is not an unexpected death. |
| `interrupted` | `invocation`, `role=development`, `item`, `reason=process_stopped_without_result` | No delivery result was returned and the provider confirms the item remains running; existing recovery policy applies. |
| `outcome` | `invocation`, `role=development`, `item`, `status` | Validated delivery outcome. A reported failed result remains distinct from a missing result. |

Example timeout event fields:

```json
{"event":"development_timeout","invocation":"dev-456","role":"development","item":"item-1","timeout_seconds":3600,"outcome":"unknown"}
```


## Human decision command

Run `agentic-harness --config CONFIG --decision REQUEST.json`. This mode is mutually exclusive with `--epic` and never starts a dispatcher, development agent, or merge agent. The same configuration and state directory as the live upgraded runner are required. The command invokes only its configured access/provider agent.

Request example:

```json
{
  "project": "/absolute/project",
  "item_id": "item-1",
  "workspace": "/absolute/project/.worktrees/item-1",
  "observed": {
    "locator": "backlog/user-action-required/item-1.md",
    "revision": "opaque-provider-revision",
    "question": "Do you approve candidate abc123 for integration?",
    "candidate": "abc123"
  },
  "decision": {"kind": "allow", "answer": null}
}
```

Required fields are exact; extra fields are rejected. `project` must match the configuration. `workspace` is the existing authoritative source workspace, including a current unpublished worktree handoff where appropriate. `locator` is the provider record reference. For filesystem observations, revision is SHA256 of locator UTF-8 bytes, a NUL byte, and exact file bytes—the dashboard's observation convention, not a Git commit. Other providers use their opaque revision. Question and candidate are exact observed values; candidate is null when the pending request names none.

`decision.kind` is `allow`, `cancel`, or `answer`. Allow/cancel require `answer:null`. Answer requires exact nonempty selected/free-text text. Allow can grant one named permission when the alternative is withholding that same permission (for example, authorize this verification or keep it paused). It cannot select among competing implementations/endpoints, supply a free-text answer, or attest that an external action such as enabling Docker has occurred. Cancel means cancel the item, not dismiss the dialog. Approval covers only the named pending request/candidate, never unrelated work or automatic merging.

The harness derives `decision_id` as SHA256 of the canonical complete submission (normalized project/workspace paths, sorted JSON keys, UTF-8, compact separators). Repeating the exact submission retains that identity. The provider must store the identity, complete decision and resolution in durable item history and check it before reapplying. Lost responses are retried with the unchanged observation/decision; a confirmed prior decision returns `already_applied`. A revision mismatch alone does not discard the human decision. The agent uses focused provider history to establish whether only an unrelated note or no-op scan changed. If question, candidate, scope/conditions, alternatives and pending resolution are unchanged, it retains the original submission/revision/decision ID, validates the latest revision for its conditional write, and preserves intervening history. A changed decision-relevant field or unverifiable comparison is rejected; approval is never broadened. An intervening published handoff still requires an authoritative source observation.

A historical resolution of the same exact request without this new decision ID returns `already_resolved`. It does not append a new decision or silently repair status. The result describes the existing resolution and identifies any stale User Action Required status that needs provider reconciliation. Its decision ID correlates the incoming submission; it does not assert that ID was stored historically. A prior approval or answer does not satisfy Cancel item; that explicit cancellation is evaluated separately.

All activity goes to the existing console/JSONL/OTEL writer. The caller consumes the final stdout JSONL event with `event:decision_result`; intermediate output and successful process creation are not persistence acknowledgements. Example (usual run/timestamp metadata omitted):

```json
{
  "event": "decision_result",
  "project": "/absolute/project",
  "status": "applied",
  "decision_id": "sha256-of-submission",
  "item_id": "item-1",
  "persisted": true,
  "resolution": "approved",
  "state": "Ready",
  "revision": "revision-after-readback",
  "workspace": "/absolute/project/.worktrees/item-1",
  "locator": "backlog/ready/item-1.md",
  "detail": "Decision verified in persisted item history"
}
```

Exit 0 means `applied` or `already_applied`, with `persisted:true` and resolution `approved`, `cancelled`, or `answered` matching the submitted decision. State, revision and source reflect the provider's actual readback. Exit 3 means either `rejected`, `persisted:false`, `resolution:none`, or `already_resolved`, `persisted:true` referring solely to the historical resolution. For `already_resolved`, `resolution` is the actual prior approved/cancelled/answered value, current state/revision are returned, and detail identifies the evidence/reconciliation need. It is not acknowledgement of a newly applied submission and must be displayed distinctly. Exit 1 means `unknown`, `persisted:null`: an explicit provider report of incomplete publication, exception, timeout, lost result, or inconsistent acknowledgement prevents confirmation, and a write may already have happened. Exit 2 is a malformed request/configuration error before provider launch. Interruption without a final result must also be treated as unknown by the caller.

### Provider serialization and upgrade prerequisite

The runner holds `provider-coordination.lock` for its lifetime. A decision command requires an actively held compatible lease for the same project; a stale file, stopped runner, wrong project, or older runner does not qualify. All access-agent invocations (including ordinary selection/status/failure/hold calls) share `provider.lock` in that state directory. Lock waits yield asynchronously, so monitoring heartbeats continue. Locks release on completion, cancellation, or process exit. A second upgraded runner cannot take the same lease.

This is local cooperative serialization, not a distributed provider lock. The operator must retain sole runner ownership and use the same state directory; an unrelated runner configured elsewhere cannot be detected by this lease. Development/merge agents and external writers may update records directly. The provider therefore must validate the observed revision and use its native conditional-update/transaction safeguards. If it cannot establish safe mutation, it must reject. The harness does not implement filesystem/GitHub transactions or parse work-item Markdown.

Upgrade the sole runner at a safe point before enabling dashboard actions. The older runner cannot acquire this lease and decision submissions remain rejected during that period. The dashboard owns HTTP authentication, binding the observed item from its server-side snapshot, and presenting explicit human actions. No real approvals or cancellations are used in the synthetic verification suite.


### Decision progress and deadline

`decision_timeout` is a positive finite number of seconds, default 120. It bounds queue waiting plus provider-agent work for each decision, independently of ordinary `access_timeout`. On expiry an active provider invocation is killed/reaped before the final result. Time spent completing cleanup may extend observed wall-clock duration. Ordinary backlog operations retain their existing timeout.

Events use the existing logging metadata (`run`, and `timeUnixNano` in activity logs):

- `provider_waiting`: emitted and flushed before waiting on either provider lock; fields `action`, `timeout_seconds`, and for decisions `decision_id` and `item`.
- `provider_started`: emitted once the access child starts; the same correlation fields plus `invocation`, `role:access`, and `pid`.
- `provider_timeout`: decision budget expired, with correlation fields and `phase:waiting` or `phase:running`.

A waiting timeout returns rejected/persisted=false because no provider agent was launched. An execution timeout returns unknown/persisted=null because the agent may have written before its acknowledgement was lost. Both preserve the original submission/decision identity for recovery; do not ask the human to reapprove it or silently replace its observed revision. The dashboard owns saved-request recovery, and the command does not automatically retry decisions.

Provider instructions limit this to one item and focused history. They prohibit repeated repository-wide discovery, delivery testing, and unrelated work. Configured claim-free SOLO exceptions must be honored: the agent uses permitted native conditional transactions and latest-revision checks, and cannot invent a requirement for an explicitly disabled claim helper. An actual inability to protect the transaction remains a concrete rejection reason.


### Publishing filesystem decisions before dispatch

For a Git-backed filesystem provider, a changed working file is insufficient. The decision agent must commit only the owned work-item changes, including any state-folder move, and verify the committed original decision ID and exact answer/approval before returning `applied` or `already_applied`. Unrelated staged and unstaged changes must remain untouched. A partial write followed by publication failure is unconfirmed (`unknown`); retrying the identical saved submission completes publication without adding another decision.

Ready selection must reconcile that published decision into the exact assigned worktree and branch before returning the item. It preserves candidate commits, local delivery history and unrelated edits, then reads back the decision ID, answer/approval and resolved question. Incomplete publication or conflicting item state prevents selection. Development reads the reconciled record before starting and honors the existing answer. These are provider-agent responsibilities; the harness does not implement Git transactions or select a provider's reconciliation strategy.

## Periodic blocked recovery

UC-016 checks every `scheduling.blocked_interval` seconds (900 by default), starting
after the first interval. A positive provider count starts an `access` invocation
with action `unblock`. Its required status is `success`, `failed`, or
`user_action_required`, and required `detail` describes the actual result.
It shares provider serialization with selection and human decisions. Recovery
waits and execution together are bounded by `scheduling.unblock_timeout` (3600 by
default), rather than the short `access_timeout`. Timeouts cancel and reap the
process group and report an unknown recovery outcome; persisted partial work is
rechecked on the next interval. No delivery retry budget is reset or consumed.

The next interval starts after the previous pass finishes. Paused or completed
runs skip checks; a pause arriving during a count suppresses recovery. Existing
recovery may finish after pause. Active and pending retry assignments are excluded,
and new delivery admission waits for recovery. Existing delivery and merge agents
continue; the recovery agent must revalidate ownership and use provider transactions.
It does not perform product delivery or merge, invent human answers, or resume
Holding/User Action Required records. Unresolved blockers retain their evidence.

`blocked_count` reports count and epic. `blocked_count_failed` reports read,
validation or timeout errors at ERROR. `unblock_started` reports a positive trigger;
`provider_started` identifies the actual invocation after lock acquisition.
`unblock_finished` carries the validated status and detail; it does not imply an
empty Blocked queue. `unblock_error` reports invocation failures/timeouts at ERROR.
All use the normal console, JSONL and OTEL writer.
