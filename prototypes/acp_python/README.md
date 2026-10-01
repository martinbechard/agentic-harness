# Python ACP and durable workflow slice

This isolated prototype ports the demonstrated question/session/review boundary
to Python. It uses the official `agent-client-protocol` SDK over stdio, the
maintained `codex-acp` adapter, and Python LangGraph with project-local SQLite.
It does not change production dispatch, process backlog, or claim production
completion. Native paid generation through this Python client has **not** been
run. The earlier JavaScript demonstration remains separate evidence.

## Ownership and persistence

- LangGraph's SQLite checkpoint owns workflow position: assessment, question,
  answer, fresh review, artifact reviewed. An assessment result is checkpointed
  before a separate interrupt node. On answer, a new child adapter loads the
  same recorded ACP session and receives only the answer.
- The existing harness `EvidenceStore`, atomic writes and OS locks retain stable
  invocation IDs, configuration digests, request digests, session creation/load
  exchanges and completed responses. These are external-effect evidence, not a
  second workflow-position engine.
- A completed result surviving checkpoint loss is reused without starting a
  child. If the result promotion was lost but a matching transport-completed
  observation survives, it is validated and promoted without another prompt.
- A submitted prompt with neither completed observation nor result is held with
  its original operation, invocation and request identities. It is **not**
  automatically re-sent. Session history/native completion lookup is not yet
  implemented; that operational recovery gap remains explicit. An uncertain
  session/new response is likewise held rather than allocating a replacement.
- The provider boundary calls the existing `Application.verify_provider_receipt`
  against the original request and observed commit receipt. Missing/invalid
  evidence cannot authorize another mutation. This slice does not dispatch
  provider agents or close actual work items.

Review independence is selected before the review prompt: a new ACP session is
created and its actual SDK request/response is retained. The exact candidate and
producer ID are bound into the assignment. Acceptance checks creation identity,
separate session, result binding, verdict, candidate SHA256 and supporting
findings. Original requirements and explicit question/answer facts are supplied;
the producer conversation is not inherited. These are local observed protocol
receipts, not cryptographic remote attestations.

## Install and offline verification

Use Python 3.12+ and Node 22+. From the repository root:

```sh
.venv/bin/python -m venv prototypes/acp_python/.venv
cd prototypes/acp_python
.venv/bin/pip install -r requirements.txt
# Maintained adapter installed only in the adjacent prototype:
(cd ../acp_codex && npm ci)
.venv/bin/pytest -q tests/test_boundaries.py
```

The requirements pin Python ACP 0.12.1, LangGraph 1.2.12 and its SQLite checkpointer
3.1.1. The editable repository dependency reuses the existing evidence and
provider verifier functions. The adjacent lockfile pins codex-acp 2.1.1 and
bundled Codex 0.159.3. No global installation/configuration change is needed.

Tests launch separate Python fixture processes and reopen the same SQLite file;
they do not merely rebuild a graph in one interpreter. The clearly labeled
`ScriptedClient` fixture proves checkpoint/journal logic without calling Codex.
Cases cover same-question/same-session restart, configuration reload, permission
and auth-context drift, finished-before-checkpoint recovery, observed completion
recovery, uncertain submission, wrong question, independent review provenance,
wrong candidate/reject/missing evidence, and real Git provider receipt validation.

## No-model handshake

Create an existing workspace and JSON configuration:

```json
{"workspace":"/absolute/project-local/workspace","effort":"low"}
```

The `adapter` and `node` fields optionally select installed executable paths;
otherwise the adjacent maintained adapter and PATH's Node are used. An optional
`model` field selects a supported native model. No model choice is required for
this no-generation probe:

```sh
.venv/bin/python cli.py --config /absolute/config.json smoke
```

A verified October 1, 2026 smoke created native session
`01a0f8ec-adcf-73a3-816d-dac643ec20e8`, received the maintained adapter's
`_auth/status_update` account notification labeled `ChatGPT Pro`, and verified
that the private process group exited. It sent **no prompt**. Evidence is in
`runs/smoke/evidence-corrected.json` locally. The first attempt exposed a genuine
SDK compatibility mismatch: Python `ext_method` prefixes `_`, while the adapter's
legacy `authentication/status` request is unprefixed. The correction uses the
adapter's advertised authentication notification through the official extension
callback, which is backed by native `account/read`. No private transport call or
authentication bypass was introduced.

## Explicit workflow use (generates subscription usage)

These commands are runnable but were **not executed live** in this slice:

```sh
.venv/bin/python cli.py --config /absolute/config.json --root /absolute/project/evidence --execution item-example start --prompt /absolute/assignment.txt
# Exit the process; retain SQLite and evidence. Use the returned question identity:
.venv/bin/python cli.py --config /absolute/config.json --root /absolute/project/evidence --execution item-example answer --question-id RETURNED_ID --text 'French'
# Re-enter interrupted assessment using its original invocation evidence:
.venv/bin/python cli.py --config /absolute/config.json --root /absolute/project/evidence --execution item-example resume
```

The first assignment is retained as authoritative requirements; the agent returns
an application question or an artifact path/hash. This uses the two-turn question
contract, not negotiated native in-turn elicitation. Review ends at
`artifact_reviewed`, which is deliberately not provider completion.

Configuration is reloaded for every new invocation. Each invocation runs a new
adapter child; compatible answers use `session/load`. Permission/auth-context
changes cannot silently replace a retained session. The environment removes API
key/gateway overrides, forces ChatGPT login, turns memories off per invocation,
and sets network access false. Producer mode is workspace-write, reviewer mode
read-only; all permission requests are rejected. Native tools/skills stay
available. The adapter's standard modes include its normal temporary-directory
behavior, not a custom write allowlist.

## Remaining production gates

This is the first proposal slice, not a production migration. It does not yet
implement the outer admission/selection graph, source delivery, claims policy,
overrun/re-estimation routing, generation ceilings, or an OTEL exporter/flush
bridge. Retained ACP response/usage observations do not establish complete
billing or descendant accounting. The production harness's existing controls
remain untouched. Native session-history reconciliation where the completed
response was never observed remains to be investigated; a hold alone is not
successful recovery. No exactly-once remote execution guarantee is claimed.

Sources: [official Python ACP SDK](https://github.com/agentclientprotocol/python-sdk),
[maintained Codex adapter](https://github.com/agentclientprotocol/codex-acp),
[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
[interrupt semantics](https://docs.langchain.com/oss/python/langgraph/interrupts).
The assessment/question separation follows sibling `lg-report`'s
`src/agent_runtime/workflows/quote_request.py`.
