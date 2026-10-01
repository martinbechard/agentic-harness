"""Offline graph checks retain actual provider Git transitions and candidate integration."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backlog_harness import item_graph
from backlog_harness.application import Application
from backlog_harness.evidence import atomic_json
from backlog_harness.provider import FileProvider, TransitionBlocked, git


@pytest.fixture
def pilot(provider, tmp_path, monkeypatch):
    return make_pilot(provider, tmp_path, monkeypatch)


def make_pilot(provider, tmp_path, monkeypatch, *, initialize=True):
    candidate = tmp_path / "candidate"
    if initialize:
        git(tmp_path, "clone", str(provider.repository), str(candidate))
        git(candidate, "config", "user.email", "test@example.invalid")
        git(candidate, "config", "user.name", "Test")
    workflow = {"allowed_paths": ["answer.py"], "checks": [["test"]], "primary_branch": "main"}

    class App:
        root = tmp_path / "graph-ops"
        config = SimpleNamespace(
            repository=provider.repository,
            data={
                "workflow": workflow,
                "workspace": str(candidate),
                "coordinator_limits": {},
                "administrative_review_limits": {"generated_tokens": 1000},
            },
        )
        question = True
        fail_after = None
        fail_result = False
        held = False
        crossed = False
        hold_operation = "escalate"

        def __init__(self):
            self.calls = []
            self.review_patch = {}

        def _stage_path(self, item, stage):
            path = self.root / "stages" / (stage + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            return path

        def execution_engine(self, item):
            return "langgraph"

        def execution_policy(self, item):
            return {}

        def recovery_record(self, item):
            return None

        def admission_dependencies(self, item):
            return []

        def item_workflow(self, item):
            return workflow

        def candidate_repository(self, item):
            return candidate

        def validate_call_limits(self, *args):
            pass

        def result_json(self, result):
            return result["value"]

        def session(self, result):
            return result["session"]

        def native_sessions_root(self, binding):
            return tmp_path

        def item_quiescent(self, item):
            return True

        guard = Application.guard

        def admission_path(self, item):
            return self._stage_path(item, "admit")

        def validate_invocation_result(self, result):
            pass

        def usage_view(self, item):
            allowance = self._stage_path(item, "allowance")
            ceiling = json.loads(allowance.read_text())["ceiling"] if allowance.exists() else 200
            total = 201 if self.crossed else 1
            status = "unknown" if self.held else "crossed" if total >= ceiling else "below"
            return {
                "status": status,
                "generated_tokens": None if self.held else total,
                "original_high": 100,
                "ceiling": ceiling,
                "may_generate": status == "below",
            }

        def authority(self, result, item, **fields):
            return {
                "item_id": item,
                "role": result["role"],
                "observed_result": True,
                "invocation_id": result["stage"],
                "session_id": "owner",
                "native_session_id": "owner",
                **fields,
            }

        async def transition(self, item, revision, target, authority, **kwargs):
            result = provider.transition(item, revision, target, authority, **kwargs)
            if self.fail_after == target or (
                self.fail_after == "release" and authority.get("operation") == "resume"
            ):
                self.fail_after = None
                raise RuntimeError("crash after committed provider effect")
            return result

        def checks(self, repo, item, candidate, stage):
            return [
                {
                    "candidate": candidate,
                    "returncode": 0,
                    "argv": ["test"],
                    "evidence_sha256": "checks",
                }
            ]

        async def invoke(self, item, stage, role, prompt, **kwargs):
            path = self._stage_path(item, stage)
            if path.exists():
                return json.loads(path.read_text())
            self.calls.append((stage, kwargs.get("session")))
            if stage == "admit":
                value = {
                    "item_id": item,
                    "provider_revision": provider.item(item).revision,
                    "operation": "new",
                }
            elif stage.startswith("graph-hold-review"):
                current = provider.item(item)
                value = {
                    "item_id": item,
                    "revision": current.revision,
                    "retained_owner": current.owner,
                    "operation": self.hold_operation,
                    "remaining_high": 10,
                    "cause": "Missing usage evidence",
                    "revised_approach": "Restore accounting",
                }
            elif stage == "accept":
                value = {"item_id": item, "accepted": True}
            elif stage.startswith("graph-answer"):
                record = json.loads(prompt.split("\n", 1)[1])
                value = {
                    "question_id": record["question"]["question_id"],
                    "answer_digest": record["answer"]["digest"],
                    "disposition": "approve",
                }
            elif self.question and stage == "graph-produce-0":
                value = {
                    "item_id": item,
                    "question": {"question_id": "language", "text": "Language?"},
                }
            else:
                (candidate / "answer.py").write_text("answer = 42\n")
                git(candidate, "add", "answer.py")
                git(candidate, "commit", "-m", "Answer")
                value = {
                    "item_id": item,
                    "candidate": git(candidate, "rev-parse", "HEAD"),
                    "reviewer_session": "reviewer",
                    "request_completion": True,
                }
            result = {
                "value": value,
                "role": role,
                "stage": stage,
                "binding": {},
                "invocation_id": stage,
                "events": [{"usage": {"output_tokens": 1}}],
                "session": {"session_id": "owner", "native_session_id": "owner"},
            }
            atomic_json(path, result)
            if self.fail_result and stage.startswith("graph-produce"):
                self.fail_result = False
                raise RuntimeError("crash after retained native result")
            return result

    app = App()
    app.provider = provider

    def review(producer, reviewer, candidate, root):
        return {
            "producer_session": producer,
            "reviewer_session": reviewer,
            "candidate": candidate,
            "native_verified": True,
            "fresh_context": True,
            "verdict": "ACCEPT",
            "unresolved_findings": [],
            "evidence_sha256": "review",
            **app.review_patch,
        }

    monkeypatch.setattr(item_graph, "verify_native_review", review)
    return app


def test_restart_question_real_provider_and_delivery(pilot):
    result = asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    question = result["__interrupt__"][0]
    assert pilot.provider.item("item-one").state == "User Action Required"
    answer = {"question_id": "language", "revision": question["revision"], "text": "Python"}
    result = asyncio.run(item_graph.run_item_graph(pilot, "item-one", answer=answer))
    assert result["result"]["state"] == "Completed"
    assert (pilot.config.repository / "answer.py").read_text() == "answer = 42\n"
    assert [stage for stage, _ in pilot.calls].count("graph-produce-0") == 1
    assert all(
        session["native_session_id"] == "owner"
        for stage, session in pilot.calls
        if stage.startswith("graph-")
    )
    before = list(pilot.calls)
    asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    assert pilot.calls == before
    assert not list(pilot.root.rglob("continuation.json"))


@pytest.mark.parametrize(
    "patch",
    [
        {"verdict": "REJECT"},
        {"candidate": "stale"},
        {"fresh_context": False},
        {"evidence_sha256": None},
        {"reviewer_session": "owner"},
    ],
)
def test_invalid_review_blocks_real_integration(pilot, patch):
    pilot.question = False
    pilot.review_patch = patch
    with pytest.raises(TransitionBlocked):
        asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    assert pilot.provider.item("item-one").state == "Running"
    assert not (pilot.config.repository / "answer.py").exists()


def test_committed_transition_recovery_does_not_repeat_model(pilot):
    pilot.fail_after = "Starting"
    with pytest.raises(RuntimeError, match="crash"):
        asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    assert [stage for stage, _ in pilot.calls].count("admit") == 1


def test_wrong_answer_and_unknown_usage_hold(pilot):
    result = asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    question = result["__interrupt__"][0]
    before = list(pilot.calls)
    with pytest.raises(TransitionBlocked, match="Unbound"):
        asyncio.run(
            item_graph.run_item_graph(
                pilot,
                "item-one",
                answer={"question_id": "wrong", "revision": question["revision"], "text": "x"},
            )
        )
    assert pilot.calls == before


def test_native_result_reused_after_checkpoint_crash(pilot):
    pilot.fail_result = True
    with pytest.raises(RuntimeError, match="retained native"):
        asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    result = asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    assert result["__interrupt__"]
    assert [stage for stage, _ in pilot.calls].count("graph-produce-0") == 1


def test_unknown_usage_blocks_generation(pilot):
    pilot.held = True
    result = asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    assert result["__interrupt__"][0]["kind"] == "hold"
    assert pilot.provider.item("item-one").state == "Holding"
    assert all(stage != "accept" for stage, _ in pilot.calls)
    before = list(pilot.calls)
    result = asyncio.run(item_graph.run_item_graph(pilot, "item-one", hold_review=True))
    assert result["__interrupt__"][0]["kind"] == "hold"
    assert pilot.calls == before


def initial_question_process(repository, evidence_root, workspace, output):
    """Fresh interpreter reconstructs services; only SQLite/evidence cross the boundary."""
    provider = FileProvider(Path(repository), Path(evidence_root))
    with pytest.MonkeyPatch.context() as patches:
        app = make_pilot(provider, Path(workspace), patches, initialize=False)
        result = asyncio.run(item_graph.run_item_graph(app, "item-one"))
        Path(output).write_text(json.dumps(result["__interrupt__"][0]))


def test_question_survives_separate_process_restart(pilot):
    output = pilot.root.parent / "child-question.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy,sys; runpy.run_path(sys.argv[1])['initial_question_process'](*sys.argv[2:])",
            str(Path(__file__).resolve()),
            str(pilot.provider.repository),
            str(pilot.provider.evidence_root),
            str(pilot.root.parent),
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # subprocess.run kills and waits for its child on timeout as well as normal exit.
    assert completed.returncode == 0, completed.stderr
    question = json.loads(output.read_text())
    result = asyncio.run(
        item_graph.run_item_graph(
            pilot,
            "item-one",
            answer={
                "question_id": question["question"]["question_id"],
                "revision": question["revision"],
                "text": "Python",
            },
        )
    )
    assert result["result"]["state"] == "Completed"
    assert [stage for stage, _ in pilot.calls] == [
        next(stage for stage, _ in pilot.calls if stage.startswith("graph-answer-")),
        "graph-produce-1",
    ]


@pytest.mark.parametrize("crash_boundary", ["Holding", "release"])
def test_hold_release_after_committed_hold_crash_preserves_graph_revision(pilot, crash_boundary):
    pilot.crossed = True
    pilot.hold_operation = "resume"
    pilot.fail_after = crash_boundary
    with pytest.raises(RuntimeError, match="crash"):
        asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    assert pilot.provider.item("item-one").state == (
        "Holding" if crash_boundary == "Holding" else "Starting"
    )
    waiting = asyncio.run(item_graph.run_item_graph(pilot, "item-one"))
    question = waiting["__interrupt__"][0]
    assert question["question"]["question_id"] == "language"
    assert pilot.provider.item("item-one").state == "User Action Required"
    complete = asyncio.run(
        item_graph.run_item_graph(
            pilot,
            "item-one",
            answer={"question_id": "language", "revision": question["revision"], "text": "Python"},
        )
    )
    assert complete["result"]["state"] == "Completed"
    assert len([stage for stage, _ in pilot.calls if stage.startswith("graph-hold-review")]) == 1
