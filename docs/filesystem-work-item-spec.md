# Filesystem Work Item Management Specification

Working draft for Martin's review. This specification covers managing work item files across worktrees. It is separate from the harness specification.

## Principles

### P-001: One agent edits a work item at a time

Normally, a work item has one agent working on it. That agent edits the item in its current worktree.

### P-002: Create on main; publish worktree defects according to their purpose

When the human user requests a work item, the provider agent creates it directly on main. When an agent discovers a defect while working in a worktree, it records the defect there. If the fix is needed to complete the assigned work, the coding agent fixes it and the defect item is committed and merged with that work. If the defect is unrelated to completing the assigned work, its item is committed separately and cherry-picked into main. Main contains the published backlog; unpublished worktree items remain local.

### P-003: Folders represent status

A work item's folder represents its status, such as ready or holding. Changing status means moving the item to the corresponding folder. The move is a work item change and is published under P-002. Status is not maintained separately in a competing field or shared coordination record.

### P-004: Identity survives moves

Each work item has a stable, unique identifier. Moving it between status folders or changing its title does not change its identity. References between items use identifiers so that folder moves do not break them.

### P-005: Preserve unrelated changes

A work item cherry-pick contains only the intended work item changes. The publishing agent preserves unrelated files and reconciles conflicts with the current main backlog rather than overwriting it with a stale worktree copy.

### P-006: Manage records, not agent execution

The filesystem provider creates, reads, edits, moves, and publishes work items. It does not dispatch agents, monitor processes, decide retries, run delivery tests, or merge implementation changes.

## Supported use cases

### UC-001: Create a work item

1. The human user asks the provider agent to create a work item, supplying its description.
2. The provider agent creates the item with a unique identifier on main: `ready` when eligible, `waiting` when its only impediment is valid unfinished prerequisites, or `blocked` when a concrete problem requires intervention.
3. The provider agent commits the new item on main, preserving unrelated changes.

### UC-002: Record a defect needed to complete the assigned work

Same as UC-001, but replace all steps starting with step 1 with:

1. An agent working on an assigned item discovers a defect whose fix is needed to complete that work.
2. The discovering agent records a new defect item in the backlog's `ready` subfolder in the current worktree, with a unique identifier and a reference to the assigned item.
3. The coding agent fixes the defect in that worktree and updates the defect item.
4. The defect item and its fix are committed and merged with the rest of the assigned work. No separate cherry-pick is needed for the defect item.

The coding and merge actions are performed by the delivery agents; this use case defines when the defect record reaches main.

### UC-003: Record a defect unrelated to completing the assigned work

Same as UC-002, but replace step 1 with:

1. An agent working on an assigned item discovers a defect that does not need to be fixed to complete that work.

Replace all steps starting with step 3 with:

3. The agent creates a separate commit containing only the new defect item.
4. The publishing agent cherry-picks that commit into main, preserving unrelated changes and resolving any conflict under P-005.
5. The publishing agent confirms that the defect item is in the main backlog's `ready` subfolder for separate work. The discovering agent continues its original assignment.

### UC-004: Record updates to a work item while working on it

1. While working on an assigned item, the agent reads the work item in its current worktree.
2. The agent records progress and findings in the item and moves it between status folders as the work progresses.
3. When the work is complete, the work item updates are committed and merged with the rest of the work.

### UC-005: Wait for prerequisites

1. Record stable dependency identifiers. Use `waiting` when the only impediment is
   unfinished valid prerequisites; use `blocked` for missing references, cycles or
   other problems requiring help. Reconcile legacy dependency-only Blocked items.
2. After an item is published Completed, recheck its Waiting dependents. Promote
   them to `ready` only when all prerequisites are Completed and other readiness
   conditions hold. Resolve dependencies across queues and completed archives.
3. Repeat reconciliation during regular integration checks, including checks with
   no merges, to recover missed or externally published completions.
4. Revalidate current revisions and ownership, preserve human decisions and
   unrelated edits, publish only owned lifecycle changes, and verify readback.
   Repeated unchanged checks do not add history or commits.

## Not supported use cases

- Dispatching, scheduling, monitoring, stopping, or replacing agents.
- Deciding retry policy or whether interrupted implementation work should be resumed or discarded.
- Implementing fixes, running delivery tests, approving implementation, or merging implementation branches.
- A separate shared assignment or status database alongside the work item folders.
- Concurrent independent editing of the same work item as the normal workflow.
- Hosted work item providers such as GitHub Issues.
