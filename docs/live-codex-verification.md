# Live Codex verification — 2026-10-02

Both live scenarios passed against harness commit `4487f228d7f0faa419c1ae0743e0213038ef3a34`. Codex executed the access, development, and merge roles through the production adapter; no fixture agent substituted for these roles. No production source changes were needed.

| Scenario | Result | Duration | Codex invocations |
| --- | --- | --- | --- |
| Ready item → development → tested merge → epic completion | Passed | 5 min 32 sec | 6 access, 1 development, 3 merge |
| Interrupted development → same-worktree recovery → tested merge | Passed | 7 min 49 sec | 10 access, 2 development, 4 merge |

## Environment and task

Each scenario used its own disposable local Git project and filesystem backlog, separate from agentic-harness. The test item required correcting `calculator.add`, with three existing tests covering positive, negative, and zero addition. The initial tests failed; after delivery and merge all three passed without changes to their expectations.

The harness used its public CLI with an epic, capacity one, the bundled Codex adapter, and the existing authenticated Codex configuration. Project instructions defined status folders and a branch/status integration convention. No remote repository was involved.

## Verified behavior

- The access agent selected the ready item and prepared an independent feature worktree and branch.
- The development agent updated the item in that worktree, fixed the function, ran tests, and committed a merge handoff.
- The merge agent pinned a committed candidate, tested the combined result, created a two-parent merge commit on main, and moved the item to completed.
- In the recovery scenario, the verifier terminated the first Codex development invocation after the item became running. The access agent reported `running`, and the harness dispatched a replacement with the identical item, worktree, and branch plus interruption instructions.
- The replacement assessed and completed the existing work. An unrelated verifier file remained unchanged and uncommitted in the feature worktree.
- Both harness runs exited normally with exit code zero after epic completion and final merge checks. Both main checkouts were clean.
- All 260 activity records in the first run and 394 in the second matched their OTLP JSON log entries, including timestamps and content.
- No tracked harness, agent, or captured interruption-descendant processes remained at the final cleanup check. Test projects and logs were retained for inspection.

## Evidence

| Scenario | Independent verification | Raw requests, results, and output | Final main commit |
| --- | --- | --- | --- |
| Delivery and merge | [verification.json](../.agent-ops/live-codex/20261002T213933Z/verification.json) | [run directory](../.agent-ops/live-codex/20261002T213933Z/) | `46c457399cf5590cc7515658428a68cb7c2c5223` |
| Interruption recovery | [verification.json](../.agent-ops/live-codex/20261002T214626Z/verification.json) | [run directory](../.agent-ops/live-codex/20261002T214626Z/) | `f533b7ffecda8174b6d88906a9abe73e6aefc617` |

Each run directory contains `config.yaml`, `context.json` with the retained project path, console output, per-invocation native Codex output and results, final unittest output, completion status, and cleanup evidence. The recovery directory also contains `interruption.json` with the original process, worktree, and branch identities. These raw artifacts are local ignored files.

The entry command for each run was:

```sh
.venv/bin/python -m backlog_harness --config <run-directory>/config.yaml --epic live-smoke
```

Local preparation and independent assertion scripts are retained under `.agent-ops/verification/`. The scenarios exercise live filesystem delivery and stopped-process recovery; they do not claim live GitHub, parallel Codex delivery, or elapsed-time timeout coverage. The deterministic suite covers those harness scheduling and timeout boundaries separately; it does not substitute for live provider verification.
