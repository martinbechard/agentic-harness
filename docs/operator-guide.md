# Operator Guide

## Configuration And Installation

Use explicit absolute paths for the target repository, candidate workspace and methodology installation. Evidence defaults to the target repository's `.agent-ops/backlog-harness`; set `operational_root` only for a deliberate absolute override. The application never imports arbitrary configured adapters. Configuration reload happens before each harness-issued CLI invocation. The complete snapshot and resolved dependency digests govern that invocation. Accepted item scopes, checks and candidate storage remain frozen; incompatible edits fence the next invocation. The effective Codex home is recorded in the session binding and applied to both capability validation and launch; changing it cannot silently resume another authentication context. Changes to the originating CLI or authentication context cannot replace a retained native session. For sessions with a recorded capability digest, model, reasoning effort, and skill changes reload on resume while the native session identity stays fixed. Role, permission, tool, and user-config-loading changes remain fenced. Each resumed invocation records its previous and current bindings plus the current configuration digest. Legacy sessions without the capability digest require an unchanged profile.

Build with `uv build`. Install the wheel using `uv tool install /absolute/path/to/agentic_harness-0.1.0a49-py3-none-any.whl`, or use `uv sync` and `uv run` from the source checkout. `agentic-harness --version` identifies the package. Codex authentication stays in its own CLI context; authenticate there with `codex login`. A named alternate context uses `agent_clis.NAME.adapter_options.codex_home`; the harness stores its reference and digests, never credentials.

The fixture generator is a concrete configuration example:

```sh
uv run python scripts/acceptance_fixtures.py \
  --root /absolute/path/isolated-acceptance \
  --methodology-root /absolute/path/dev-methodology
```

It creates separate SOLO and MULTITASK provider repositories, each with three dummy items and unchanged authoritative tests. It refuses to replace an existing fixture. Creating fixtures makes no model call. Run their generated config through `validate`, then `run --until-terminal` or `app`.

A setup example is [dev-methodology-observation.yaml](examples/dev-methodology-observation.yaml). It requires the project setup owner to create the named independent workspace and storage directories. Its example scope/checks are for schema validation only and must be replaced with the selected item’s accepted requirements before dispatch.

## Workflow And Authority

The dev-methodology integration selects `provider_interaction: agent`. It is under verification; historical release acceptance covers the earlier direct fixture route. `validate` checks configuration without a model call. `refresh-provider` performs a bounded, read-only Coordinator observation when the provider source changes and otherwise returns the cached observation. Neither command dispatches a Work Item. Dashboard and status use the cache and report stale observations rather than launching agents.

The configured Coordinator and Orchestrator must be able to read the applicable provider-management skills from the methodology installation. They may load them by reference; literal profile duplication is unnecessary. Management invocations execute in the authoritative repository; implementation invocations stay in the separate candidate workspace. A writable management operation additionally requires the configured role's `workspace-write` permission and the recorded actor, revision, transition, prompt, and exact provider paths. Agents use their configured claim helper. The harness does not acquire claims. A separate executor never becomes the canonical owner. Management usage is retained separately and checked against `administrative_review_limits`.

The default is `provider_interaction: agent`. Explicit `provider_interaction: direct` preserves the earlier fixture route. When replaying an older fixture configuration, add that explicit selection; its retained evidence is not migrated to the agent route. The byte-oriented `recover-provider` command below belongs to that route; it must not be used to adopt or replace an existing desktop task. Agent-mediated operation recovery reuses the recorded operation and invocation evidence. A desktop task ID alone is not a CLI resume identity.

The Coordinator requests admission against the current provider revision. Ready becomes Starting before a canonical Orchestrator starts. That Orchestrator accepts ownership while read-only; the provider records Running for its portable session before writable source work. The agent may use native specialists and arrange a native independent review.

A completion request must identify the exact candidate. The harness independently reads native producer and reviewer records, verifies fresh context and distinct identity, verifies the candidate and passing checks, merges it into the configured primary branch, runs integrated checks, and closes and archives the provider record in a separate exact-path Git commit. Neither a successful launch nor a report of done permits closure.

Delivery requires clean tracked files and index. Unrelated untracked files and symlinks remain in place; their content and mode fingerprints are retained in the delivery receipt and checked again after verification and on replay. Exact path collisions and ancestor/descendant collisions with reviewed changes block integration. The merge also refuses to overwrite ignored files. A changed preservation baseline blocks advancement without deleting or stashing user work.

Provider and delivery effects have durable requested records. If a commit completed but its receipt was lost, reconciliation proves its unique parent, paths and bytes and rebuilds the receipt. A merge is recovered by its exact parents. Unproven mutations stay unresolved. For a request that is proven uncommitted, `recover-provider OPERATION_ID` explicitly finishes the recorded bytes under the provider transaction lock. It requires the original main HEAD, exact original/intended checkout and index bytes, the recorded actor gate, and no unrelated changes. A committed operation recovers its receipt without another commit. The application does not reset a checkout or repeat an unknown paid launch.

For an agent-managed `Ready → Starting` commit whose invocation was interrupted, a separate bounded read-only invocation in the same native session may verify the original operation and committed effect. Record that invocation under the item’s `reservation-effect-reconciliation` stage. Pass its saved result to `reconcile-provider-effect OPERATION_ID --evidence /absolute/result.json`. This command validates the exact commit, owner, changed paths, preserved requirements, stopped original invocation, same-session read-only evidence, and administrative call limits before updating the projection. It retains the original unresolved outcome and non-advancing receipt, and writes a separate `effect-reconciliation.json`. Admission can consume that verified reservation without repeating the mutation. Missing telemetry remains incomplete; reconciliation neither supplies missing usage nor waives implementation usage, review, approval, or delivery gates. The queue reads provider observations under the provider transaction lock so a commit awaiting its result does not cancel the in-flight invocation.

An interrupted read-only inventory observation has a separate recovery command:

```sh
agentic-harness --config /absolute/path/config.yaml \
  resume-provider-observation OPERATION_ID NATIVE_SESSION_ID
```

The command resumes the exact native session under the original provider operation. It creates one attributed continuation invocation and reuses that invocation on command retry. It rejects a live or unknown process, a changed binding, a different native session, writable evidence, changed provider source, or incomplete usage evidence. A timeout or call-limit change can apply to the continuation when the CLI binding and provider source still match. Configure those values before the first continuation attempt. A later configuration change cannot alter an unsubmitted continuation intent.

The preflight reconciles the interrupted native cumulative output with content-bound telemetry and completed native-child usage. New model output after the last covered usage span makes usage unknown and blocks the continuation. The returned continuation counter remains cumulative for the same native session. The configured Coordinator token limit applies to that cumulative count, while the one returned continuation turn satisfies the existing one-turn call limit. After return, the normal inventory and policy validators run against the current source before the cache advances.

## Integration And Retained Proof

`reconcile-integration ITEM --instruction FILE` binds the original accepted candidate, exact current primary, existing task authorization and a prospective remaining estimate. It prepares a separate integration workspace, preserves original receipts, runs the declared checks and obtains a fresh independent native review before delivery can select the merged candidate.

A complete failed integration check can supply feedback for the single remaining correction attempt. The harness preserves the failed candidate, every check receipt and the execution intent; the scoped agent repairs the cause, then all configured checks and fresh review run again. A second failure exhausts the bound. Interrupted or incomplete checks do not authorize a correction.

If a failed freshness check needs an additional existing generator, pass `--amendment FILE` to the same reconciliation command. The append-only amendment binds `item_id`, `instruction_digest`, `failed_attempt: 1`, `failed_candidate`, exact `primary`, `next_attempt: 2`, `purpose: reproduce-failed-required-check`, and the exact failed `required_check` argv. Its additive `generators`, `generated_paths` and `generator_inputs` must use the configured runtime, unchanged tracked script/input hashes and original accepted output scope. The generator must be the script executed by that check; leading Python `-B` is supported. The amendment cannot change task authority, permissions or the prospective allowance. Replays reuse its retained effects.

Retained auxiliary proof must remain applicable to that candidate. An unchanged complete tree can reuse its bound proof directly. A recognized scoped proof package must retain its original accepted native review, linked artifact hashes, dependency bindings and case obligations. The required integration reviewer also assesses whether that package covers the merged candidate, explicitly addressing every changed bound dependency and every case. This uses the existing review invocation. The agent judges semantic applicability; the harness verifies identities, hashes, complete assessments and the exact candidate-bound verdict. Changed guidance can remain compatible; changed behavior, expectations or acceptance obligations require fresh verification of affected cases. Missing or tampered bindings, incomplete decisions and a fresh-proof-required verdict prevent advancement. Historical proof and unknown usage are never rewritten to make reuse pass.

## Modes And Capacity

SOLO permits one executing Work Item. MULTITASK requires the provider's explicit concurrent setting. Each item uses an independent clone under `candidate_root`. Admission conservatively blocks overlapping declared paths, and actual candidate diffs and current-main compatibility are checked again during integration.

`max_active_invocations` controls harness-issued invocations. Durable slots retain uncertain executions after process-lock loss. A lowered cap includes existing higher-numbered slots. `native_max_threads` is a Codex-native request governing native child work; those children remain internal to the CLI. The harness verifies independent review and reconciles their generated tokens with native telemetry before permitting subsequent advancement. It does not select another CLI or reload a profile for each internal child.

## Usage And Holds

The normal queue can prepare unreserved Ready items when `workflow.preparation` is configured with `allowed_roots` (relative source files or directories) and `check_commands` (exact argv lists). A bounded, read-only Coordinator invocation reads the complete canonical item and applicable project guidance, returns exact source paths, selects checks from that catalog, and supplies content-bound authority evidence. It cannot change CLI permissions, workflow mode, review or delivery requirements; configured base checks remain mandatory. Item overrides may change only paths and checks. Preparation records and their prospective estimate provider operations are retained across retries. Another item's configuration does not invalidate them; changed relevant configuration, canonical content, or authority evidence requires reconciliation. A preparation refusal remains item-local and is not regenerated on each queue poll. Existing reserved assignments keep their frozen workflow. Authority sources are repository-local, except an exact native completion record already bound by this item’s stopped-owner recovery: its retained path/hash, current canonical citation, and completed native turn must all validate. This exception grants no additional source-write authority.

The preparation `workflow` object has a closed schema. It requires `allowed_paths` and `checks`. It can retain commentary in `check_status`, `checks_executed`, `scope_conditions`, `implementation_constraints`, `required_gates`, and `verification_limit`. Ordinary obligations that the runtime already enforces belong in `implementation_constraints`. Additional proof, design, or approval obligations remain in `required_gates` and block dispatch unless an existing explicit workflow selection covers them.

An older retained response can use the unsupported `gates` field. The harness permits one read-only correction for that exact representation error before any provider effect. The Coordinator classifies every original entry by index and exact text. A constraint classification names a closed, configured runtime obligation and states that no additional proof is required. Other entries remain required gates. The harness preserves existing required gates, scope, checks, authority evidence, estimates, and historical values. It retains the original response unchanged and writes a separate linked correction and resolution. Replay uses the accepted correction without another call. An incomplete classification, changed authority, or remaining unsupported gate stops dispatch.

An explicit per-item `review_requirements` selection can route retained source-analysis obligations through the existing independent source review. It binds the current provider revision, effective preparation digest, and, for a corrected response, the original preparation and correction-resolution digests. Every requirement has a unique ID, canonical reference, acceptance text, and exact retained required-gate value; uncovered obligations still block dispatch. Before spawning the existing reviewer, the producer runs each configured check separately against the committed candidate and supplies the complete native call receipts, canonical item, preparation lineage, requirement set, and candidate source hashes in the recorded spawn packet. The harness validates those native executions and outputs occurred before the spawn, then runs its own checks as separate corroboration. The child assessment must bind the candidate, requirement-set digest and prior receipt hashes, accept every requirement with named candidate evidence, and report no unresolved findings. A generic `ACCEPT`, later green checks, or a finding that the evaluation was weakened cannot satisfy this selection. Replay retains the same production and review evidence and adds no review call.

The admitted item's execution high generated-token estimate is frozen. If an unowned Ready item has no historical estimate, `record-estimate ITEM_ID --decision DECISION_JSON` records a validated Coordinator's dated prospective estimate through the provider manager. It preserves the unknown historical estimate and usage, verifies the exact provider commit, and binds the new budget to that revision before admission. It does not launch implementation. Usage for this route is labeled `prospective_execution`; it does not reconstruct historical consumption. The default ceiling is twice that estimate. Root cumulative counts, resumed calls, per-request OTLP usage, and observed native child counts are reconciled without adding output and reasoning tokens twice. Missing, partial or conflicting data stays unknown.

At a safe boundary, an affected item is held before further generation when usage is unknown or crosses the ceiling. Its canonical owner and session are retained. A bounded Coordinator review can release the hold only with trustworthy usage below an authorized cumulative ceiling. A higher ceiling needs an operator approval reference. An allowance never bypasses unknown usage. `review-hold ITEM_ID CEILING APPROVAL_REFERENCE` is the interactive command; the noninteractive command accepts `--ceiling` and `--reference`.

Coordinator and administrative call budgets are verified from observed CLI turns and generated output before their decisions can authorize advancement. A single opaque native CLI call may overshoot a requested budget; the harness gates subsequent effects and records that fact. Native internal tool cycles are managed by the CLI.

## Questions And Run Controls

`item show ITEM_ID` displays the exact provider record and usage. `item answer ITEM_ID QUESTION_ID` presents the current exact question, captures the provider revision, and prompts for text. The answer is durably persisted before the canonical session classifies it. Only `approve` permits continuation; defer, decline and ambiguous retain the question. An answer to a stale revision is rejected. After a crash, `resume-answer ITEM_ID` resumes the unique persisted answer operation with its original revision and canonical session. Scheduling fences the item while that operation is incomplete; an approval cannot be lost between its provider commit and continuation record.

Pause closes admission while accepted work continues. Resume reconciles before reopening the retained run intent. Stop closes admission, cancels owned requests through their adapter, and reports proven stopped processes separately from unresolved workflow evidence. Quit cannot substitute for stop. Restart preserves pending identities and keeps admission closed until an explicit run or resume request.

Watch polls deterministically and calls no model for unchanged scope. Status, validation, dashboard refresh, and evidence reconciliation make no model calls. Until-terminal success requires every in-scope item to be Completed and no unresolved effect. A terminal scope containing Failed, Abandoned or other nondelivery outcomes reports settlement without success.

## Telemetry And Inspection

The loopback OTLP/HTTP JSON receiver lives inside the foreground application, independently of the dashboard. It attributes standard native spans to the provider, item and invocation using a private invocation token. It preserves native trace relationships, deduplicates retries, rejects conflicting correlation, and fsyncs before acknowledging storage. A transient storage failure remains an unresolved export until that exact batch is durably received; an overlapping partial batch cannot clear it. New receipts bind the actual span contents, so an unchanged count cannot hide changed evidence.

The dashboard and terminal use the same projection of provider records, invocation evidence, usage and traces. Missing observations are unknown; stale runtime data does not prove a process stopped. Raw invocation and native telemetry evidence stays under the configured operational root. Keep that directory when recovering a run. Status, dashboard, terminal controls and reconciliation resolve current provider/evidence storage even when a generation profile is missing or invalid. They report the configuration error. Every new CLI invocation still requires complete current validation; control inspection never authorizes generation from stale configuration. An invalid generation edit pauses admission without cancelling a running invocation.

If an item is blocked, inspect its provider record, `scheduling-blocks.json`, invocation outcomes, native session evidence and provider/delivery requested records. Correct the actual missing evidence or authorization. Do not delete an uncertain operation directory to force another launch.

## Tested Native Export Failure Boundary

On Codex 0.159.2, no retry was observed in the tested first-export 503 scenario. The first 512 spans were lost while 1200 later spans and all 11 measured output tokens arrived. Matching usage and later spans did not restore the rejected batch. The generation gate rejected the result. Shutdown establishes observed drain/process exit with stable retained spans; it does not prove complete flush while an export is missing. See [native exporter evidence](verification/native-exporter-evidence.json).

The receiver does not promise lossless export or introduce a separate durable spool. If the original validated batch is available, re-ingest those exact bytes through the existing authenticated receiver/correlation boundary and verify persistence before clearing its incident. If it was not retained, keep the gap and affected transition held. The operator must use the selected provider management workflow for an explicit nondelivery disposition; a larger allowance, a fabricated span or another paid invocation cannot substitute for the missing evidence. Inspection, other safe items and operator controls remain available.

The dashboard caches complete trace records by file identity and byte offset, reads appended records once, exposes a partial tail as uncertainty, and orders the displayed recent spans by native timestamp. Counts and item details use the same captured provider revisions; a changed provider snapshot is rejected for refresh. Display configuration reload does not replace an executing invocation’s snapshot. Legacy receipts from the earlier completed acceptance remain historical display evidence; they cannot authorize new advancement without the current content binding.

Provider observations may explicitly retain unknown dependency information. Such items remain visible but cannot execute until their dependency evidence is known and satisfied. A sparse dependency map never implies empty dependencies without an explicit agent clarification. The admission flag is a boolean for new execution, separate from recovery of retained owners. Older clipped provider responses can be reconstructed from the unique matching completed native response; original evidence remains unchanged.

`recover-item ITEM --evidence /absolute/recovery-evidence.json` continues a preserved candidate after an external execution has ended. The input identifies immutable native completion records and the candidate checkout, complete change base, commit, and hashed supporting files. A harness Coordinator must authorize that exact item's redispatch, dependencies, source/check scope, and remaining-work estimate. For a stopped Running item, recovery records `Running → Ready`, then uses the normal `Ready → Starting → Running` acceptance sequence with a new native identity. For an already Ready/Unowned item, include `preserved_execution` with the canonical execution ID, complete canonical content SHA256, verbatim remaining-work excerpt, `candidate_approval_required: true`, and a hashed native execution-receipt map. `snapshot_operation` identifies the exact candidate snapshot entry; `runtime_operations` maps each stopped native session to its completed operation in that same execution attempt. This route imports the unchanged candidate and returns Ready without a provider mutation or source execution. The normal queue must separately reserve and accept it. Prior attempts remain historical; new coding and candidate changes are prohibited. When candidate approval is required, delivery needs an approved operator-answer continuation bound to that exact candidate. It imports the candidate into an independent clone and retains the former identity and evidence as history. Global new admission remains closed when crisis policy requires it; this authorization applies only to the recovered item.

If that preserved execution returns an owned, successful native response with `status: blocked`, `request_completion: false`, and outstanding work, `continue-work ITEM --instruction /absolute/followup.txt` records one bounded follow-up without launching it. The normal queue continues the same accepted native session. The request binds the Running item revision, owner, unchanged candidate, and original blocked result. Changed evidence blocks continuation. Existing source review and checks should be reused where valid; source production, candidate replacement, attempt resets, and gate bypass remain prohibited. The new stage retains the original result and remains subject to current configuration and usage guards. This does not answer a pending user question or authorize delivery.

Recovery usage is labelled `recovery_remaining_work`; historical usage and the historical original estimate remain explicit, including unknown values. The recovery estimate controls only new harness execution and never reconstructs an unknown lifetime total. Unknown usage within the new execution still prevents generation. Restart uses the persisted recovery decision and provider operation receipt. A verified one-item provider transaction updates the projection only when all changed provider paths match its declared scope and current policy evidence validates; it does not reread the entire historical inventory.

`defer-item ITEM --question /absolute/question.json` records an already-pending user question after a canonical execution explicitly declines completion. It also supports an unowned Ready item with a retained, revision-bound Coordinator preparation conflict. The preparation supplies the conflict context; a separate observed Coordinator decision must authorize the exact proposed question, revision, and provider paths. No fake reservation or source execution is required. For this route, the committed item must match the original bytes with only the status header changed and the exact question block appended; unrelated additions or edits are rejected. Archive and established series links are verified. The JSON names the exact `question` (`question_id`, `text`) and provider `paths` (current item, user-action-required destination, and an optional series index). A bounded Coordinator decision must approve those exact values. The harness verifies the declined native result belongs to the current owner, then uses the existing provider transaction and immutable commit receipt. The command does not ask the question again, retry a denied browser action, or authorize another item. It preserves the owner and candidate while making the inactive question state visible in the provider.

`reassess-policy` requests a source-backed Coordinator reassessment of global admission while preserving inventory and item-local questions. Missing or stale authority prevents projection changes. When a returned current inventory was rejected only because its policy was invalid, `reassess-policy --observation /absolute/observe-stage.json` validates that exact terminal observation, its complete current inventory, and its original read-only evidence before reassessing policy. The command never accepts the rejected policy. It publishes the retained inventory only after the replacement policy validates and the provider source, observer, capability, and Coordinator binding remain unchanged. A failed reassessment preserves both the prior cache and the retained invocation evidence.

Harness runtime records and cached projections are observations, not governing policy.
For example, a previous run's `admission_open: false` does not establish an operator
pause for a later run. Policy evidence must cite the current canonical authority;
files inside the harness operational root cannot supply it. Actual operator
pause/stop controls, execution locks, and item-local restrictions still apply.

An active workflow may explicitly exempt agents from claims. The policy then records `claims_required: false`, a `claim_exemption` naming the authority, and current hashed source evidence supporting `coordination`. The harness validates that evidence before skipping helper discovery for management operations. SOLO alone does not establish an exemption; missing applicability fields retain the project's default claim requirements. Agents continue to operate the helper whenever claims apply.

If a committed provider update repeats a policy with a defective citation, the harness may reuse the previous policy only when its cited sources still validate and every control field matches. It retains the original receipt and separate reconciliation evidence. Changed decisions or stale prior authority still block advancement; recovery never repeats the committed mutation.

`reconcile-stopped-owner ITEM --evidence /absolute/runtime-evidence.json` accepts one `runtime_records` entry identifying a hashed native completion log and `owner_binding.section_sha256` binding the unique active Running Acceptance Evidence section, including its owner and canonical native identity. A Coordinator must establish that the recorded owner stopped without producing source changes or a candidate. The existing provider operation records Ready and Unowned while preserving history. This command does not launch replacement work.

Recovery hashes have different domains: the provider revision hashes `path + NUL + content`; a content hash hashes only content. The owner-section hash preserves all whitespace after the Running Acceptance Evidence heading through the next level-two heading boundary. These values must not be compared interchangeably.


## Scoped Proof Artifacts

`profiles.<name>.artifact_output: true` explicitly enables invocation-local operational
output for that profile. The default is disabled. The adapter records the exact path,
invocation identity and permission fingerprint in `artifact-output.json` beside the
invocation receipt. Only its `artifacts` child directory becomes writable; the rest of
the operational root, including harness receipts, remains read-only. A read-only or
provider invocation never receives this grant. Symlink redirects and changed contracts
are rejected. Enabling output changes the permission fingerprint and cannot silently
resume an existing session.

For a preserved Running item whose existing bounded continuation returned blocked,
For ordinary items with retained preparation, an explicitly reviewed
`workflow.items.<id>.proof_requirements` selection can require visual proof before
delivery. It contains `provider_revision`, `preparation_digest` (the complete saved
decision envelope digest), and a nonempty `requirements` list. Each requirement
has a unique `id`, `canonical_reference`, `evidence_kind` (`browser` or `print`),
and `acceptance_text`. Independently review coverage of the full preparation
packet before enabling this selection; the harness does not interpret free-form
gate text as a workflow. Existing scope and check authority must remain unchanged.

With artifact output enabled, the harness collects confined proof once and asks
the owning agent to arrange a fresh native child review. Each requirement needs
supporting artifact hashes, a passing result, and an independent accepted
assessment bound to the candidate, proof result and complete requirement set.
Delivery revalidates these bindings and artifact contents. Missing, altered or
stale proof stops delivery; a changed integration candidate cannot reuse it.
Existing browser/server restrictions still apply. These mechanics establish
evidence integrity and reviewer identity; the reviewer judges visual acceptance.

Set `candidate_approval_required: true` only when the item's authority requires
explicit approval of the exact candidate. The legacy workflow uses the existing
operator question/answer route; the graph workflow currently rejects this option
before dispatch. A normal clarification answer does not grant candidate approval.
The actual dev-methodology selections remain disabled pending their separate
[coverage review](verification/dev-methodology-proof-coverage.md).

When preparation requires design acceptance before implementation, the optional
per-item `design_review` object contains `provider_revision`,
`preparation_digest`, `canonical_reference` and `acceptance_text`. The harness
uses the existing artifact-only invocation permissions to obtain a design and
fresh native child review at the admitted source base. Source-writing invocation
requires accepted review bound to that design artifact and preparation. A genuine
independent rejection permits one correction; missing or invalid evidence does
not permit a retry. The accepted artifact is supplied to the producer and checked
again before delivery. Existing workflows are unchanged when this option is absent.

If acceptance also requires independent overall verification, set
`proof_requirements.verification_required: true`. The fresh proof reviewer receives
the canonical assignment, accepted source review, source-check results and check
execution receipt with exact file hashes. In addition to visual assessments, it
must record a separate overall verification conclusion, acceptance coverage,
inspected receipts and unresolved findings. Its native identity must differ from
the source producer, proof producer and source reviewer. This reuses one review
invocation; it does not certify future integration or provider closure. Bound
check receipts are retained across interrupted or approval-waiting replay, while
their candidate, commands, output integrity and native source review are revalidated.

`run-proof ITEM --instruction /absolute/proof.txt` runs one separate proof invocation.
Add `--prepare-only` to persist the exact invocation intent and report its artifact
directory without launching a process or granting access. This makes a proposed
permission change concrete before authorization. Repeating preparation preserves the
same directory; the eventual invocation consumes that intent.
Use an explicitly authorized configuration with artifact output enabled on its
Orchestrator profile. It may share the original repository, operational root and frozen
workflow; retain the original configuration for the canonical session. The request binds
the current owner, revision, candidate, prior result and exact instruction. Both candidate
source and provider source remain read-only during this proof invocation. Only its own
artifact directory is writable. Repeating the same command reuses the retained stage;
changed requests and unresolved invocations cannot launch replacement work.

This route does not re-admit the item, replace the owner, change the accepted session's
permissions, or permit delivery. Its result remains proof evidence for the canonical
workflow to assess through an authorized continuation. Independent candidate review,
semantic acceptance, usage guards and exact-candidate approval remain required. A
successful CLI exit or the presence of output files does not establish those gates.

The adapter enforces the launched process's filesystem boundary. Agents arrange native
review and instruct reviewers to inspect artifacts read-only. Native child permissions
are managed by the CLI; the harness does not claim independently enforced child
read-only isolation. Agent-owned artifact files remain untrusted supporting evidence.
Queue diagnostics retain both singular `blocker` and plural `blockers` details when an
agent declines completion.


## Target-Owned Evidence Storage

When `operational_root` is omitted, evidence belongs to the configured target repository
at `<repository>/.agent-ops/backlog-harness`, independent of the launch directory and installed
harness location. An explicit absolute `operational_root` remains supported. Setup
configurations should omit that override unless a different evidence location is explicitly intended.

For an existing run, stop admission and verify all invocations are stopped before moving
its evidence. A deliberate filesystem relocation can retain every file unchanged and
leave the old root as a compatibility symlink to the target-owned directory. Verify a
complete file/hash manifest before and after relocation, then update the configured root.
This preserves absolute references in historical telemetry and receipts without rewriting
submitted evidence, owner identities, candidates or native sessions. Keep the relocation
manifest and rollback information with the evidence. Do not resume an old running process
against the changed configuration. Rebind currently effective scheduling holds to the
new configuration digest when their item revision and substantive scope are unchanged;
keep admission paused until continuity is verified.

A prepared but never-submitted invocation must still reject a changed configuration.
If only its storage root changes, retain the original preparation records, verify the
unchanged request and binding, and deliberately rebind that unsubmitted intent's
configuration digest before preparing it again. Preserve its invocation ID. Never apply
this step to an invocation with a submission or process record. Existing permission
denials remain in force; relocating storage does not approve delivery or broaden native
filesystem access beyond an explicitly authorized artifact-output directory.


After proof output is retained, `continue-proof ITEM --instruction /absolute/followup.txt`
registers its exact request and result digests for the original accepted execution.
The normal queue resumes that owner and native session with its original permissions.
The harness verifies the candidate and supporting file hashes before handoff; the agent
arranges a fresh native review of the retained proof package without repeating production,
Judges, or proof. The reviewer returns the usual candidate/verdict/unresolved-findings
contract plus `proof_result_digest`; the producer returns `proof_reviewer_session` separately
from its source reviewer. Before an approval question or completion advances the item,
the harness verifies native reviewer independence, candidate, verdict and proof digest.
Earlier invocation results remain intact. Missing or invalid evidence blocks advancement;
registration alone supplies neither acceptance nor delivery approval. Usage guards and
other items' scheduling holds remain effective.


When valid exporter measurements do not reconcile with native session counters, usage
retains an unknown exact total and reports `coverage: incomplete`. Deduplicated observed
output is exposed as `generated_tokens_lower_bound`, including CLI-internal work seen by
the invocation exporter. A lower bound at or above the ceiling reports `crossed`; below
the ceiling it remains `unknown`. Both prevent generation. Raising a reviewed ceiling
does not repair incomplete coverage. Delivery authorization and usage review are
separate decisions; neither clears the other gate.

When the canonical Orchestrator has already returned a conditional delivery request
(`status: awaiting_exact_candidate_approval`), existing operator authorization can be
bound without another model call to classify the answer:

```sh
agentic-harness --config /absolute/config.yaml authorize-delivery ITEM --authorization /absolute/authorization.json
```

The JSON contains exactly `item_id`, `question_id`, `revision`, `candidate`,
`disposition` (the string `approve`), `answer`, and `source_reference`. Bind it to the
current question, revision and reviewed candidate; retain the actual authorization
and its source. The command does not invent approval or ask the operator to repeat
authorization already given. It uses the existing provider answer and resume operations.
The normal queue then reuses the original conditional result and accepted reviews,
checks the exact candidate and evidence, integrates it, verifies it, and closes the item.
No implementation or proof generation is permitted by this path. Incomplete usage still
blocks further generation; provider administration retains its separate limits.
Interrupted answer operations retain the same authorization for `resume-answer`.

Delivery permits an overlapping path already advanced on the primary branch only when
its Git object and mode exactly match the reviewed candidate. Conflicting changes and
overlapping untracked files still block integration. Delivery receipts prevent replaying
an already proven merge.

Harness Codex invocations disable automatic CLI memory generation by default through a
per-process `features.memories=false` override. This avoids unrelated memory work inside
an item invocation; it does not disable native tools, skills or delegation or edit global
CLI settings. Set `adapter_options.disable_memories: false` explicitly to opt out. All
observed usage remains subject to the usual coverage and attribution checks.
