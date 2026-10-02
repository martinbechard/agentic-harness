# Operator and agent protocol

## Ownership

The harness controls agent capacity, dispatch, time limits, two automatic retries per item, pause/resume, and merge invocation timing. Agents own provider details and delivery conventions. Only one merge invocation runs at a time; development and provider agents may operate while it runs.

The work item access agent selects ready items with satisfied dependencies, prioritizing the selected epic. A second query can fill unused capacity with items outside the epic. It prepares the worktree and branch and returns their identities. It must respect the active-item exclusions and consult known workspaces so unpublished filesystem status changes do not cause duplicate dispatch. Selection itself does not mark an item running.

The development agent marks its item running, delivers it, and records its own status and delivery information. A user-action outcome means the question or action is already recorded in the item and its status is **User Action required**. The harness does not collect answers. Once the provider reports the item ready again, ordinary dispatch applies.

The discovering agent records defects through the provider. The development agent fixes blocking defects. Filesystem records are created in the current worktree and published with delivery. Provider-specific early publication belongs to the separate filesystem-provider specification.

The merge agent identifies eligible work according to the project's convention, prepares integration with the latest target branch, runs required tests on the combined result, merges passing work, and updates the item. A successful development outcome triggers a check, not an assumption that a PR exists. Periodic checks also run. The merge agent reconciles competing attempts' work item information even when Git merges cleanly.

## Commands

Configure each role as a nonempty argument array. Commands execute without a shell. Project and state paths may be absolute or relative to the configuration file. Other relative command arguments are interpreted in the agent's working directory; use absolute paths for scripts and executables.

Each invocation receives:

- `HARNESS_REQUEST`: an absolute path to a JSON request.
- `HARNESS_RESULT`: an absolute path where it must write its JSON result before exiting.
- Current directory: the project for ready/epic/merge queries; the assigned worktree for development and item-specific status/failure/hold operations.

Write results atomically. A result is accepted only after the process has stopped. Stdout and stderr are activity output, never parsed as the authoritative result. Results must be JSON objects no larger than 1 MiB.

The bundled `python -m backlog_harness.codex` adapter passes the request to `codex exec` and uses `--output-schema` and `--output-last-message` for the result. Optional adapter arguments are `--executable`, `--model`, `--effort`, and `--profile`. It inherits permissions and does not bypass the configured sandbox.

## Requests and results

All requests contain `role`: `access`, `development`, or `merge`. Item identities have this shape:

```json
{"id":"item-1","worktree":"/absolute/path/to/worktree","branch":"feature/item-1"}
```

| Access action | Request fields | Required result |
| --- | --- | --- |
| `ready` | `limit`, `epic` (string or null), `outside`, `excluded`, `workspaces`, `instruction` | `{"items":[...]}` |
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
