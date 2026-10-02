"""Create one retained pre-a54 scoped-question invocation in an isolated test process."""

import json
import sys
from dataclasses import asdict

from backlog_harness.application import Application
from backlog_harness.cli import main
from backlog_harness.contracts import digest
from backlog_harness.native_evidence import source_review_instructions
from backlog_harness.provider import TransitionBlocked

_invoke = Application.invoke
_transition = Application.transition


async def historical_invoke(
    self,
    item_id,
    stage,
    role,
    prompt,
    *args,
    **kwargs,
):
    """Insert the historical approval at its original prompt position."""
    if stage.startswith("scope-continuation-"):
        admission = self.scope_admission(item_id)
        assert admission is not None
        assert stage == "scope-continuation-" + digest(admission)
        assert role == "orchestrator"
        assert asdict(kwargs["session"]) == admission["session"]
        assert kwargs.get("read_only") is False
        assert kwargs.get("coordination") is True
        tail = source_review_instructions(
            admission["workflow"],
            admission["input"]["amended_content"],
            self.candidate_repository(item_id),
        )
        if tail:
            assert prompt.endswith(tail)
            prompt = prompt[: -len(tail)]
        continuation = json.loads(self._stage_path(item_id, "continuation").read_text())
        prompt += "\nPersisted canonical approval: " + json.dumps(continuation["approval"])
        proof = self._stage_path(item_id, "proof-review")
        if proof.exists():
            prompt += (
                "\nReuse this retained fresh proof review; include its reviewer_task "
                "(or reviewer_session if absent) as proof_reviewer_session in your response. "
                "Do not repeat proof or review: " + proof.read_text()
            )
        prompt += tail
    return await _invoke(self, item_id, stage, role, prompt, *args, **kwargs)


async def stop_before_question_effect(
    self,
    item_id,
    expected_revision,
    target,
    authority,
    *args,
    **kwargs,
):
    """Leave the returned historical question terminal before its provider mutation."""
    question = authority.get("question", {})
    if (
        target == "User Action Required"
        and question.get("question_id") == "restore-required-verification-environment"
    ):
        raise TransitionBlocked("Historical fixture stopped before provider effect")
    return await _transition(
        self,
        item_id,
        expected_revision,
        target,
        authority,
        *args,
        **kwargs,
    )


Application.invoke = historical_invoke
Application.transition = stop_before_question_effect
raise SystemExit(main(sys.argv[1:]))
