# Codex ACP stdio prototype

This isolated experiment uses the maintained `@agentclientprotocol/codex-acp`
adapter and official ACP SDK. It does not change production dispatch or process
backlog items. Martin selected ACP after the initial A2A investigation.

```
LangGraph assessment -> official ACP client --stdio--> codex-acp
                                                      -> Codex App Server -> ChatGPT subscription
```

The adapter and its Codex child run on demand and are cleaned up as a private
process group. No HTTP listener, daemon installation, API-key fallback, or custom
wire transport is needed. ACP is the outer protocol. The maintained adapter owns
the inner App Server connection. A2A standard transports do not include stdio;
ACP and A2A are different protocols, not interchangeable names.

## Run

Node 22+, existing Codex ChatGPT login, and local package installation are needed.

```sh
cd /Users/martinbechard/dev/agentic-harness/prototypes/acp_codex
npm ci
npm test                 # five offline tests; no model calls
npm run demo             # real subscription usage: producer twice, fresh reviewer once
```

`node demo.mjs /absolute/new/run-directory` selects a new output directory;
its parent must exist. Existing directories are refused. `PROTOTYPE_MODEL` may
select a model supported by the installed CLI; otherwise native default applies.
Dependencies are exact-pinned with a lockfile: codex-acp 2.1.1, ACP SDK 1.6.0,
LangGraph JS 1.4.18. The lockfile resolves bundled Codex 0.159.3. The adapter installs its compatible Codex dependency locally.
No global package or configuration is modified.

The live demo gives Codex an incomplete greeting assignment. Codex must use its
native tools to count names and record one preparation line, then formulate the
missing-language question itself. The graph stores that decision before its
separate interrupt node. A scripted demonstrator answer supplies French on the
same ACP/Codex session. The preparation file must retain both hash and mtime.
The native project skill supplies a required artifact suffix. A separate
`session/new` starts the reviewer with only the requirements and artifact paths;
it independently computes the candidate hash. The caller checks verdict,
matching hash, evidence, and unchanged artifact after review.

This demonstrates workflow-selected fresh review **before launch**, rather than
merely rejecting inherited context after it has consumed tokens. It does not
replace agent autonomy for ordinary delegation.

## Boundaries and evidence

- `authentication/status` is the adapter's extension backed by App Server
  `account/read`; only `chat-gpt` is accepted. Account email is discarded. API key,
  gateway, default-auth-request and custom binary environment overrides are
  stripped. `forced_login_method=chatgpt` and `features.memories=false` are local
  child configuration overrides. Native skills and tools remain enabled.
- Workspace-write native mode is used for the producer and read-only for review.
  The maintained adapter's modes allow normal temporary-directory behavior;
  they are not a bespoke filesystem allowlist. All permission requests are
  rejected; no full-access mode or sandbox escape is granted. A denial fails
  visibly rather than being answered as a clarification.
- `input_required` here is an **application decision**, not an ACP v1 stop reason.
  The native turn ends, then a second `session/prompt` resumes its existing
  session. The maintained adapter also supports negotiated form elicitation for
  native in-turn user questions; this prototype does not test that extension.
- `evidence.json` retains question, session IDs, native tool-event summaries,
  response usage where provided, artifact/hash, preparation before/after,
  independent verdict, and process cleanup. Thought streams and auth secrets
  are not stored. Usage is native reported evidence, not a complete billing or
  descendant accounting guarantee. A successful turn alone is not acceptance.
- The LangGraph MemorySaver and live ACP process cover pause/resume **within one
  demonstration process**. Crash/restart recovery, durable checkpoint storage,
  idempotent remote retries, and production workflow integration are not claimed.
- A 10-minute run timeout terminates the private process group. Normal cleanup
  also checks that the group has exited. No adapter auto-restart/retry is added.

Offline tests prove checkpoint replay behavior, decision rejection, isolated
billing configuration, and separation of commentary from the final message.
They use a clearly scripted assessment fixture and do not prove live Codex
behavior. Inspect the live evidence for that result.

## Sources

- Maintained adapter, native modes, skills and stdio:
  https://github.com/agentclientprotocol/codex-acp
- Official ACP SDK and standard stdio client:
  https://github.com/agentclientprotocol/typescript-sdk
- A2A standard transport distinction:
  https://a2a-protocol.org/latest/topics/custom-protocol-bindings/
- Workflow pattern adapted from sibling `lg-report`'s
  `src/agent_runtime/workflows/quote_request.py`: assessment is checkpointed
  before a separate interrupt node, so resume does not repeat assessment.

## Bounded review clarification recovery

The first live run completed native preparation, clarification, same-session
follow-up and skill-backed artifact production. Its independent reviewer rejected
ambiguous wording about the preparation file: the producer correctly recorded
the count `2`, while the review interpreted "recording two names" as requiring
person names. Original failed evidence is preserved at
`runs/1790880690514/evidence.json`; it is not rewritten as success.

To reassess only that existing reviewer with corrected count wording:

```sh
node recover-review.mjs runs/1790880690514/evidence.json runs/1790880690514/review-recovery.json
```

This validates original artifact/preparation hashes and marker mtime before any
model call, refuses an existing output evidence file, then uses standard ACP
`session/load` to load the original independent reviewer in a new adapter process.
Only one review prompt is sent. Producer work is not relaunched. Exact candidate,
verdict and evidence are validated, then artifact/preparation are checked again.
This tests native ACP session persistence across adapter processes; it does not
claim durable LangGraph crash recovery. The recovery completed with ACCEPT;
see [verification evidence](VERIFICATION.md). The example output file already
exists locally, so another invocation requires a new output path and consumes
another review turn; it is not needed to inspect the retained result.
