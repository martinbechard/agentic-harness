"""SQLite owns workflow position; invocation evidence owns external effects."""

import json
from hashlib import sha256
from pathlib import Path
from typing import TypedDict

from configuration import reload_config
from invocations import Invocations, parse_json
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from backlog_harness.contracts import digest
from backlog_harness.evidence import operation_lock


class State(TypedDict, total=False):
    execution_id: str
    prompt: str
    requirements: str
    turn: int
    session: dict
    result_ref: dict
    decision: dict
    question_id: str
    clarifications: list[dict]
    answer_digest: str
    review_ref: dict
    outcome: str


def artifact(snapshot, decision):
    if decision.get("state") != "candidate":
        raise ValueError("Expected candidate decision")
    path = (snapshot.workspace / decision["artifact"]).resolve(strict=True)
    if not path.is_relative_to(snapshot.workspace) or not path.is_file():
        raise ValueError("Candidate artifact is outside assigned workspace")
    candidate = sha256(path.read_bytes()).hexdigest()
    if decision.get("candidate") != candidate:
        raise ValueError("Candidate hash differs from artifact bytes")
    return path


def build_graph(invocations, checkpointer, after_result=None):
    async def assess(state):
        reference = await invocations.invoke(
            f"assess:{state['turn']}", "producer", state["prompt"], session=state.get("session")
        )
        if after_result:
            after_result(reference)  # fault injection only, after durable external result
        result = invocations.result(reference)
        decision = parse_json(result["response"]["text"])
        if decision.get("state") == "input_required":
            if not isinstance(decision.get("text"), str) or not decision["text"].strip():
                raise ValueError("Question is missing")
        else:
            artifact(reload_config(invocations.config_path), decision)
        question_id = digest([state["execution_id"], reference["digest"], decision])
        return {
            "result_ref": reference,
            "session": invocations.session(reference),
            "decision": decision,
            "question_id": question_id,
        }

    def ask(state):
        answer = interrupt({"question_id": state["question_id"], "text": state["decision"]["text"]})
        if (
            not isinstance(answer, dict)
            or answer.get("question_id") != state["question_id"]
            or not isinstance(answer.get("text"), str)
            or not answer["text"].strip()
        ):
            raise ValueError("Answer must bind the pending question")
        return {
            "prompt": answer["text"],
            "answer_digest": digest(answer),
            "clarifications": [
                *state.get("clarifications", []),
                {
                    "question_id": state["question_id"],
                    "question": state["decision"]["text"],
                    "answer": answer["text"],
                    "answer_digest": digest(answer),
                },
            ],
            "turn": state["turn"] + 1,
        }

    async def review(state):
        decision = state["decision"]
        snapshot = reload_config(invocations.config_path)
        path = artifact(snapshot, decision)
        prompt = (
            "Independent artifact review in this new session. Do not edit. "
            "Read the artifact and supplied acceptance requirements; independently compute its SHA256. "
            "Return only JSON {verdict:ACCEPT|REJECT,candidate:<actual SHA256>,evidence:<specific findings>}.\n"
            f"Artifact: {path}\nCandidate: {decision['candidate']}\n"
            f"Requirements: {state['requirements']}\n"
            + "Authoritative clarification answers: "
            + json.dumps(state.get("clarifications", []), sort_keys=True)
        )
        reference = await invocations.invoke(
            "review:" + decision["candidate"],
            "reviewer",
            prompt,
            producer_id=state["session"]["session_id"],
            candidate=decision["candidate"],
        )
        invocations.verify_review(reference, decision["candidate"], state["session"]["session_id"])
        artifact(snapshot, decision)
        return {"review_ref": reference, "outcome": "artifact_reviewed"}

    graph = StateGraph(State)
    graph.add_node("assess", assess)
    graph.add_node("ask", ask)
    graph.add_node("review", review)
    graph.add_edge(START, "assess")
    graph.add_conditional_edges(
        "assess",
        lambda state: "ask" if state["decision"]["state"] == "input_required" else "review",
    )
    graph.add_edge("ask", "assess")
    graph.add_edge("review", END)
    return graph.compile(checkpointer=checkpointer)


async def run(
    root, execution_id, config_path, *, prompt=None, answer=None, factory=None, after_result=None
):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    invocations = Invocations(
        root, execution_id, config_path, **({"factory": factory} if factory else {})
    )
    config = {"configurable": {"thread_id": execution_id}}
    # Serial pilot: OS lock also prevents a second process driving the same checkpoint concurrently.
    with operation_lock(root / "execution.lock"):
        async with AsyncSqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
            graph = build_graph(invocations, saver, after_result)
            saved = await graph.aget_state(config)
            if prompt is not None:
                if saved.values:
                    raise ValueError("Execution already exists; resume its checkpoint")
                incoming = {
                    "execution_id": execution_id,
                    "requirements": prompt,
                    "prompt": prompt
                    + '\nReturn only JSON. If essential information is missing: {"state":"input_required","text":"your question"}. '
                    'Otherwise create the requested artifact and return {"state":"candidate","artifact":"relative path","candidate":"actual SHA256"}.',
                    "turn": 0,
                }
            elif answer is not None:
                if (
                    not isinstance(answer, dict)
                    or not saved.values
                    or saved.values.get("decision", {}).get("state") != "input_required"
                    or answer.get("question_id") != saved.values.get("question_id")
                ):
                    raise ValueError("Answer does not match durable pending question")
                incoming = Command(resume=answer)
            else:
                if not saved.values:
                    raise ValueError("Execution not found")
                incoming = None
            result = await graph.ainvoke(incoming, config)
            # Interrupt objects are SDK internals; expose their serializable application payload.
            if "__interrupt__" in result:
                result["__interrupt__"] = [entry.value for entry in result["__interrupt__"]]
            return result
