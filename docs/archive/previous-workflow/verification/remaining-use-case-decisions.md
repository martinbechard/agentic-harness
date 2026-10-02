# Harness ownership audit and removal decisions

Northstar — 2026-10-02. Replaces the earlier keep/cut recommendations in this file, including the incorrectly retained harness integration responsibility.

**Governing boundary:** agents organize and execute development work. Git supplies version control and merge/conflict behavior. The agent performing a merge resolves its conflicts and arranges relevant testing/review. The harness enforces declared workflow transitions and controls the invocations it launches. It does not become an engineering manager, integration agent or second version-control system.

**Ownership test:** a problem being real, frequent or costly does not assign it to the harness. First identify who owns the action. Only retain a harness control if it enforces an explicit transition rule or protects a resource/effect the harness itself controls. Prefer existing provider/Git/CLI operations; do not duplicate them.

**Probabilities:** all earlier numerical estimates are withdrawn. No defensible per-item bad-state incidence is established; the assigned likelihood remains **0%** under Martin's unknown-means-zero rule. This does not mean physical impossibility. Explicitly required basic controls remain requirements; zero-estimate hypothetical protection does not justify optional features. No use-frequency figures or invented conditional factors are retained.

## The complete responsibility list

| Subject | Actual owner | What the harness may retain | What must leave the harness |
| --- | --- | --- | --- |
| Who can request a transition | Declared workflow; authorized agent/operator requests it | Validate actor, item and permitted transition at the mutation boundary | Inferring authority from CLI exit or an agent saying done; extra approval roles invented by harness |
| Current state and stale updates | Existing provider mutation operation | Existing revision/state check at the actual write boundary | Duplicate validators that protect the same boundary; new provider framework |
| Canonical work-item owner | Provider/workflow, requested by authorized agent | Enforce declared owner where the transition requires it | Managing an agent's internal specialist team or delegation ownership |
| Duplicate execution of one invocation | Harness | One durable submission identity and narrow operation lock; uncertain execution holds that item | Broad recovery state machines, reconstructing lost success, silently resubmitting |
| Concurrent agents' source work | Agents and Git | Limit harness-launched concurrency if configured; do not launch the same item twice | Custom source conflict predictor, merge reservation framework or delegation transport |
| Git branch/worktree/commit strategy | Agent performing source work; Git executes it | Read candidate identity/delivery facts needed by the declared transition | Creating integration candidates, forcing commit count/history shape, managing agent source commits |
| Git merge and conflict resolution | Agent doing the merge; Git reports conflicts | Check declared delivery evidence when requested | Automatic merges, cherry-picks, conflict resolution, correction attempts or merge-recovery orchestration |
| Correctness of combined source | Agent doing integration and its reviewers | Bind required evidence to the candidate the workflow requires | Assessing semantic interaction risk, inventing new integration-review stages or deciding tests to add |
| Review arrangement and specialist delegation | Agents through native CLI capabilities | Verify required independence, candidate identity, verdict and supporting evidence before protected advancement | Selecting/spawning reviewer hierarchies, forcing another general verifier, custom delegation runtime |
| Test selection and execution for an item | Implementing/integrating agents under project requirements | Validate evidence required by the declared transition; a separately explicit fixed check hook can execute a configured command, without selecting more work | Selecting tests, automatic post-merge suites, rerunning unchanged tests, interpreting coverage gaps to create work |
| Tests of the harness product itself | Development agents maintaining this repository | Nothing as an extra runtime workflow feature | Turning the user's 100% code-coverage requirement for this product into a new rule for every processed item |
| Packaging/install verification | Agent changing/releasing the package | Validate packaging evidence only if the actual transition contract requires it | Per-item installation trials, automatic release verification stages unrelated to the item |
| Design, screenshots, browser proof, specialized acceptance | Agents working on an item whose requirements call for them | Accept/validate the evidence specifically required by that item's transition | Generic proof-production engine, default extra design/verifier/approval workflows, proof-reuse adjudication |
| Item dependencies and queue ordering | Coordinator agent and existing provider operations | Enforce explicitly declared provider preconditions where required | Infer dependencies, build a scheduling graph, arrange specialists or invent sequencing policies |
| Resource claims | Agents using their configured claim helper | Honor explicit launch conditions and faithfully pass configuration where required | Claim orchestration, helper-discovery dispatch blocker, pretending to police native internal delegation |
| Scope judgment and amendments | Authorized agent/operator | Validate required amendment authority and bound item; apply approved provider change | Model calls to mechanically edit status/content, independently deciding scope expansion |
| Configuration | Operator provides it; harness loads it | Reload and validate relevant invocation settings before every launch | Scanning unrelated global skills or historical cache fingerprints |
| Usage/accounting and configured limits | Harness for observable/attributable usage | OTel JSONL attribution, deduplication, configured holds and pre-launch checks | Guessing unknown usage; blocking valid returned work for nonessential diagnostic gaps; claiming control of invisible CLI-internal actions |
| Cancellation and process lifecycle | Harness for processes it launches | Stop owned process group; report uncertainty accurately | Global process hunts, stopping unrelated agent processes, automatic replacement work |
| Harness file/provider writes | Harness/provider for their own effects | Exact intended paths, existing mutation operation, preserve unrelated files | Global source cleanliness policing; taking over agents' staging/commit practices |
| Questions and explicit approvals | Agent asks; operator/authorized responder answers | Bind the answer to the actual question/current required context | Invent new approval gates; historical prompt reconstruction; multiple redundant approval packets |
| Interrupted or failed work | Agent/operator decides how to continue | Preserve available logs/result; stop uncertain work; explicit retry after old execution is stopped | Automatic native-log success reconstruction, speculative repair, retry campaigns |
| Provider observation/cache | Existing provider plus agent interpretation where necessary | Minimal actual-input cache when it avoids paid repetition | Migration framework, broad skill/catalog fingerprints, independent inventory-only substitute for real provider management |
| CLI compatibility and selected dependencies | Adapter for actual launch contract | Small capability/configuration check and selected dependencies | Exact patch-version equality, unrelated private/shared skill validation |
| Status display | Harness reports its own state | Basic read-only status and logs | Optional dashboard expansion unrelated to enforcing transitions |

## Removal checklist — no ownership exemptions for these features

| Feature | Decision | Replacement / retained boundary |
| --- | --- | --- |
| Harness-executed Git merge | REMOVE | Agent merges using Git; harness reads required delivery facts. |
| Harness-created integration candidates/workspaces | REMOVE | Agent chooses its source workspace and candidate. |
| Conflict resolution and automatic correction attempts | REMOVE | Agent handles Git conflict/check feedback. |
| Automatic integration reviews and post-merge check stages | REMOVE | Agent arranges required evidence; harness verifies declared contract. |
| Hard-coded requirement for a separate integrated_checks packet | REMOVE as a universal gate | Required evidence can be agent-supplied; no compulsory harness stage name. Preserve candidate/result binding. |
| Integration proof-reuse/changed-dependency assessment | REMOVE | Reviewer judges evidence applicability for the actual requirement. |
| Generic extra acceptance verifier | REMOVE | Existing required independent review. |
| Generic design/proof/approval subsystems | REMOVE | Item-specific evidence under existing workflow; no invented approval. |
| Automatic native-log completion/accounting reconstruction | REMOVE speculative reconstruction | Keep available recorded results and attributable accounting; unknown state holds spending/item. |
| Dedicated missing-summary recovery and historical prompt compatibility | REMOVE | Accept rare lost work; explicit retry; no byte-equality gate. |
| Exact prompt equality, coordination markers, rigid review layout | REMOVE | Operation/candidate/actor/verdict/evidence checks. |
| Identical automatic test/review repetition | REMOVE | Reuse valid evidence; agents decide necessary new work. |
| Flexible output-folder machinery | REMOVE | One fixed harness evidence location. Agents manage their project artifacts. |
| Model calls for mechanical mutations | REMOVE | Existing deterministic provider operation after authorization. |
| Broad cache fingerprints and migrations | REMOVE excess | Minimal actual-input reuse or refresh. |
| Exact CLI patch lock and unrelated skill checks | REMOVE | Required capability/selected dependency checks only. |
| Completion block for nonessential trace gaps | REMOVE | Returned result retained; accounting independently controls further spending. |
| Single-commit/direct-parent source candidate restriction | REMOVE | Required candidate identity and Git facts, without custom source-history policy. |
| Second execution engine/custom delegation/agent claim transport | REMOVE | One execution path, native delegation, agent-owned claim helper use. |
| Dashboard enhancements | REMOVE/defer optional work | Basic status/logs. |

## Current source contradicts this boundary

These are implementation targets, not claims of removal:

- [delivery.py](../../src/backlog_harness/delivery.py): `integrate` performs `git merge` and invokes integrated checks. This source integration responsibility belongs to the merging agent.
- [application.py](../../src/backlog_harness/application.py): `_run_item` invokes `integrate`; integration reconciliation/review/proof methods expose additional harness-owned stages. Replace this orchestration with validation of the agent's declared delivery evidence and the existing provider transition.
- [integration_flow.py](../../src/backlog_harness/integration_flow.py): `reconcile_integration`, `_attempt`, `resolve_conflict` and correction feedback own an agent's correction/review workflow.
- [integration_reconciliation.py](../../src/backlog_harness/integration_reconciliation.py): candidate workspace creation and resolution finalization implement a second source integration workflow.
- [integration_authority.py](../../src/backlog_harness/integration_authority.py): integration-specific instruction/proof assessment supports that extra workflow. Preserve only genuinely required evidence validation when removing callers; do not move the same framework elsewhere.
- [workflow.py](../../src/backlog_harness/workflow.py): completion currently demands `integrated_checks` alongside delivery evidence. Remove the compulsory extra stage, while retaining verification of the actual required candidate/review/check/delivery facts.
- [estimation.py](../../src/backlog_harness/estimation.py): preparation text ties acceptance obligations to dedicated harness proof/design support. An agent's ability to satisfy a real requirement must not depend on the harness having a custom feature for that evidence type.

The file provider already checks revision and transition validity inside its mutation transaction. Agent-provider and file-provider routes are alternatives; seeing similar checks in both does not establish redundant execution. Simplification must preserve the effective boundary for each retained route.

## Verification boundary

The remaining harness tests should demonstrate that valid declared evidence permits the protected transition, wrong/missing evidence blocks it, and execution controls reload configuration, attribute usage, honor limits/stops, and prevent implicit duplicate launch. Git merge scenarios belong to the agent's workflow; the harness does not need a conflict-resolution subsystem or its tests.

This audit changes ownership and removal decisions only. Source removal and 100% coverage remain incomplete. Backlog runs and dev-methodology tests remain stopped. No release/install or paid agent invocation was performed for this audit.
