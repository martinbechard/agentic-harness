# ACP prototype verification

Verified October 1, 2026 by Northstar. This is an isolated experiment, not a
production adapter installation or backlog delivery.

## Observed result

The subscription-backed run completed through an initial run and one bounded
review clarification. The original failure remains intact. The producer and
its completed preparation were not run again.

| Check | Evidence |
| --- | --- |
| Maintained standard transport | ACP SDK 1.6.0 over stdio to codex-acp 2.1.1; bundled Codex 0.159.3; LangGraph JS 1.4.18. No HTTP host. |
| Subscription authentication | Adapter account status reported `chat-gpt` in both processes. API-key/gateway overrides removed; no billing fallback. |
| Genuine clarification | Codex asked which language to use after reading the names and writing their count. The application did not manufacture the question. |
| Same-session answer | Two producer prompts used `01a0f8ce-b509-7952-a11e-3bca4e014b82`; the demonstrator supplied French. |
| Native skill and tools | Recorded native command read `names.csv` and `.agents/skills/prototype-format/SKILL.md`; native edit produced the greeting with the skill's required suffix. |
| No repeated completed preparation | `completed-work.txt` remained exactly `2\n`, with identical SHA256 and modification time before/after the answer and review recovery. |
| Fresh review | Separate `session/new` created reviewer `01a0f8cf-2393-7bc2-aef4-49b359df72a7`; it received requirements and files, not producer chat. |
| Candidate verification | Reviewer independently computed artifact SHA256 `7e00f89a063d75151c69e7261a60642e103c745e6d1471fb1c51c380bb48c4f6`; caller checked the same hash after review. |
| Review-only recovery | Standard `session/load` loaded that reviewer in a new adapter process. One clarification prompt produced ACCEPT; producer session was not prompted. |
| Cleanup | Both evidence records report `processGroupGone: true`; a separate process inspection found no remaining prototype adapter process. |
| Focused tests | Five tests passed: checkpoint resume, malformed decisions, billing configuration, final-message isolation, and review evidence/candidate rejection. |

## Preserved evidence and the prompt defect

Local raw evidence is ignored by Git to keep operational output out of source:

- [Original run](runs/1790880690514/evidence.json)
- [Review-only recovery](runs/1790880690514/review-recovery.json)
- [Produced artifact](runs/1790880690514/workspace/greeting.md)

The initial review prompt said “recording two names”, although the producer's
requirement was to record their numeric count. The reviewer interpreted that as
requiring literal names and rejected the otherwise correct artifact. The shared
review contract now specifies exactly `2` followed by a newline. The existing
reviewer accepted after clarification and independently checking the bytes. This
corrected a prompt defect without weakening the original acceptance criteria.

Three native turns ran initially and one reviewer clarification ran afterward.
Native ACP usage fields are preserved as reported; they are not asserted to be
complete cumulative usage or billing totals. No additional demo run was needed.

## Recommendation and limits

ACP is a demonstrated candidate for the local client-to-Codex boundary. Reuse
the maintained adapter rather than building a custom transport. Keep workflow
transitions, evidence gates, and checkpoint ownership in the harness; let Codex
execute its native skills and tools.

This demonstration uses an application-level question at the end of a turn,
followed by another prompt in the same session. It does **not** test native
`elicitation/create`. The installed adapter implements that negotiated input
mechanism, but test it before selecting it for production in-turn questions.
Permission requests are separate and were not used as clarification answers.

`session/load` recovery was observed for the reviewer. Durable LangGraph crash
recovery, duplicate prevention after uncertain submission, complete usage/OTEL
attribution, concurrent tasks, and general production permissions are not proven
by this demo. MemorySaver only establishes in-process workflow continuation.
Fresh review sessions share the assigned workspace and installed instructions;
they do not inherit the producer's conversation.

For a production ACP proposal, specify the narrow adapter boundary, preserve
per-invocation configuration reload and permission binding, establish attributed
usage coverage, and test cancellation/restart/idempotency before switching any
dispatch. Choose and test either the demonstrated two-turn question contract or
native elicitation. No wholesale framework migration is needed for that decision.

Normal backlog delivery remains on the existing harness. Its pending repairs
must finish separately: bind existing operator authorization to the retained
conditional delivery request, preserve source/proof/candidate gates, allow only
identical already-integrated paths, run focused tests and independent review,
install the bounded repair, then integrate and close the one retained item via
the existing provider operations. ACP is not a prerequisite for that work.
