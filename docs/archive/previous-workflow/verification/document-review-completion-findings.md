# Documentation completion review

Reviewer: Northstar. Date: 2026-09-30.

Verdict: **BLOCKED**. The required mechanical review-evidence validator cannot validate this repository through its installed command interface. This is a review-tool capability error, not a new source finding or an approval of the documentation.

## Frozen candidate and checks

The candidate is `.agent-ops/document-review-candidate.json`, SHA-256 `e6b12051fd8c8bdd025c1fac4b1d80dcb6a80dc0dea949ea3ebee270670b1940`. Its 16 file hashes match the checkout. All 40 source-file hashes in `docs/verification/release-acceptance.json` also match. HEAD is the accepted source commit `8f0e5dc5ed8fee097095178afe48ab152d0c61fd`.

The review read the plan, README, operator guide, active ARC-002 and HLD-003, six module designs, requirements matrix, and four retained JSON receipts. The accepted source review was not reopened. Historical native delivery, current installed replay, and current deterministic verification remain separate evidence classes.

## Required capability error

The installed `review-structured-artifact` skill requires `scripts/validate_review_evidence.py` for final acceptance. Its configured installed path is `/Users/martinbechard/.agents/skills/review-structured-artifact/scripts/validate_review_evidence.py`.

The validator was called with the exact frozen candidate using its supported command interface:

```sh
python3 /Users/martinbechard/.agents/skills/review-structured-artifact/scripts/validate_review_evidence.py \
  --candidate /Users/martinbechard/dev/agentic-harness/.agent-ops/document-review-candidate.json \
  --stage pass-1 --pass-one-status clear --verdict BLOCKED
```

It exited with status 1 before validating evidence:

```text
ValueError: candidate is outside the repository: /Users/martinbechard/dev/agentic-harness/.agent-ops/document-review-candidate.json
```

The trace identifies `/Users/martinbechard/.agents` as the validator's repository root. The supplied script derives its root from its own installed location. Its CLI has no repository-root argument. Its canonical-checklist validation also requires repository-local `skills/review-*/references/review-checklist-*.md` paths. The application repository and installed canonical skill sources do not share that layout.

The available Python function has a `repository_root` parameter, but setting it to the application root does not make the installed canonical checklists repository-local. Broadening a root, relocating canonical sources, changing the validator, or substituting a custom validator would exceed this review's scope and the instruction to respect tool-root limits. None was attempted.

The required capability is therefore unavailable for this candidate in the configured installation. The review stops under the instruction: “If a required tool or function is unavailable, stop and report the missing capability as an error. Do not install, emulate, bypass, or substitute for it.” The structured-review skill also requires BLOCKED when a required mechanical validator is unavailable.

## Provisional essential evidence

No new material essential defect was established in the reviewed candidate. These observations do not constitute final acceptance or a completed checklist.

| Concern | Current evidence and provisional assessment |
| --- | --- |
| RELEASE-1 correction | ARC-002 Architectural Layers explicitly defines caller-to-dependency arrows. Application resolves contracts and registry; controller calls application; terminal calls controller and projection; projection reads provider/evidence/application usage; dashboard calls an injected collector. `runtime.py` is explicitly a contract module. The adjacent launch and runtime summaries agree with those boundaries. |
| HLD context | HLD-003 Parent Architecture shows agent decisions returning to application, then a validated transition request to the provider. It does not give agents direct provider mutation authority. |
| Problem fit and scope | The plan and designs preserve native agent organization with harness actor, candidate, independent-review, checks, delivery, and usage gates. They retain the selected Codex 0.159.2, file/main, resource-coordination `none`, SOLO or approved MULTITASK route. |
| Admission and execution | HLD admission branches separate assess/invalid/unchanged outcomes from successful admission or validated continuation. The documents distinguish provider Starting, canonical acceptance, Running, and writable source work. |
| Configuration and identity | MOD-001 and HLD distinguish full validation from control-only inspection and immutable invocation snapshots. Session compatibility binds executable, adapter, profile, and authentication-home origin; ARC explicitly excludes stable account-principal identity within an unchanged home. |
| Recovery and effects | MOD-002 through MOD-004 distinguish intent, adapter-owned requested marker, observed session/event persistence, uncertain effects, provider commit proof, prepared recovery, and delivery re-entry. They preserve exact retained identities and prohibit blind replacement of an uncertain paid effect. |
| Questions and usage | HLD separates read-only answer classification from later source work. MOD-005 distinguishes unknown usage from threshold crossing, preserves original estimates and owners, and retains item-local holds. |
| Native telemetry limitation | All relevant prose preserves the rejected 512-span batch, no retry observed in the tested scenario, 1,200 later spans, and matching 11 output tokens. It does not claim lossless export or complete flush. |
| Views and controls | MOD-006 separates the full projection from the 11 interactive status fields and session inspection. Recorded runtime facts do not become proof of liveness or provider authority. |
| Evidence provenance | The matrix and receipts distinguish six historical native deliveries from current accepted source, 78 tests, lint, matching installation, controls, and zero-new-invocation replay. Current replay retains the SOLO 9 and MULTITASK 12 invocation sets. |

## Page, terminology, and evidence status

The Markdown link tool checked all 12 Markdown candidates. It returned 18 `path_outside_root` findings for sibling-methodology provenance links and no other findings. Those external links remain unverified within the allowed root. No fallback attempted to resolve rejected paths. This known link-verification gap is separate from the validator error and does not itself establish a candidate defect.

Terminology loading returned only `reference_not_found`: **TERMINOLOGY STANDARDS LOADED — ABSENT** within configured snapshot `d801aa1fb7ddcc330a5e3173372ea6af4a3d08ec58074478e85aa5603e926658`. No wider absence claim is made.

The core structured-review, page-verification, STE, terminology, architecture, HLD, module, routing, and interface-pattern guidance was retrieved. Canonical checklists and the module template were retrieved as review inputs. The memory search found no task-specific entry and supplied no judgment.

The complete item-level checklist, complete page/sentence evidence, and a valid mechanical receipt were **not completed**. No `GOOD` or `PAGE_VERIFICATION_EVIDENCE_READY` result is issued. Existing source acceptance remains intact; the documentation release gate remains open.

## Next owner action

The methodology/tool owner must provide a supported validator route that binds an external application candidate and its installed canonical checklists without changing authority or bypassing root restrictions. Then obtain a fresh independent completion review of the same frozen candidate, or a newly frozen candidate if any covered input changes. That review must finish all applicable checklist, page, sentence, and mechanical evidence before GOOD.

Only new `document-review-completion*` review artifacts were created. Candidate documents, source, tests, earlier reviews, and immutable receipts were not changed. No tests, native executions, paid calls, or commits were performed.
