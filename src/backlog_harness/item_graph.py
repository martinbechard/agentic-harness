"""Fresh-item pilot: checkpoints own position, existing services own effects and gates."""

import asyncio
import json
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .contracts import digest, plain
from .delivery import integrate
from .evidence import atomic_json, component
from .native_evidence import verify_native_review
from .provider import AgentProvider, TransitionBlocked, git
from .workflow import require, validate_candidate, validate_transition


class State(TypedDict, total=False):
    item_id: str
    assignment: dict
    base: str
    revision: str
    acceptance: dict
    produced: dict
    question: dict
    answer: dict
    turn: int
    transition: dict
    next_node: str
    review: dict
    checks: dict
    candidate: str
    delivery: dict
    result: dict
    guard_target: str
    hold_decision: dict


def answer_evidence_path(app, item_id, answer):
    return app.root / "item-graphs" / component(item_id) / ("answer-" + digest(answer) + ".json")


def build_graph(app, checkpointer):
    """Caller supplies existing item/admission locking and exclusive engine routing."""

    def prepare(state, target, authority, next_node):
        return {
            "transition": {"revision": state["revision"], "target": target, "authority": authority},
            "next_node": next_node,
        }

    async def effect(state):
        request = state["transition"]
        result = await app.transition(
            state["item_id"],
            request["revision"],
            request["target"],
            request["authority"],
            validate=validate_transition,
        )
        return {"revision": result["revision"], "result": result}

    def freeze(state):
        item_id = state["item_id"]
        app.execution_policy(item_id)
        item = app.provider.item(item_id)
        require(
            item.state == "Ready" and item.original_high is not None,
            "Graph pilot requires fresh Ready item with known estimate",
        )
        require(not app.recovery_record(item_id), "Graph pilot cannot import legacy recovery")
        require(
            all(row["state"] == "Completed" for row in app.admission_dependencies(item_id)),
            "Dependencies are incomplete",
        )
        path = app._stage_path(item_id, "assignment")
        assignment = {
            "content": item.content,
            "provider_revision": item.revision,
            "provider_path": item.path,
            "original_high": item.original_high,
            "historical_original_high": item.original_high,
            "workflow": plain(app.item_workflow(item_id)),
            "candidate_root": app.config.data.get("candidate_root"),
            "workspace": app.config.data["workspace"],
        }
        if path.exists():
            require(json.loads(path.read_text()) == assignment, "Frozen assignment differs")
        else:
            atomic_json(path, assignment, exclusive=True)
        repo = app.candidate_repository(item_id)
        require(repo != app.config.repository, "Candidate must be separate from provider")
        require(not git(repo, "status", "--porcelain"), "Candidate is dirty")
        return {
            "assignment": assignment,
            "revision": item.revision,
            "base": git(repo, "rev-parse", "HEAD"),
            "turn": 0,
        }

    async def admit(state):
        item_id = state["item_id"]
        result = await app.invoke(
            item_id,
            "admit",
            "coordinator",
            "Decide admission only. Return JSON {operation:new|assess,item_id,provider_revision,reason}. "
            "Do not mutate or implement.\n"
            + json.dumps(
                {
                    "item_id": item_id,
                    "provider_revision": state["revision"],
                    "assignment": state["assignment"]["content"],
                }
            ),
        )
        app.validate_call_limits(result, app.config.data["coordinator_limits"])
        value = app.result_json(result)
        require(
            value.get("operation") == "new"
            and value.get("item_id") == item_id
            and value.get("provider_revision") == state["revision"],
            "No current admission",
        )
        return prepare(
            state, "Starting", app.authority(result, item_id, operation="new"), "gate_accept"
        )

    async def accept(state):
        item_id = state["item_id"]
        result = await app.invoke(
            item_id,
            "accept",
            "orchestrator",
            "Accept canonical ownership read-only. Do not implement or delegate. Return JSON "
            "{item_id,accepted:true}.\n"
            + json.dumps({"item_id": item_id, "assignment": state["assignment"]["content"]}),
        )
        value = app.result_json(result)
        require(
            value.get("accepted") is True and value.get("item_id") == item_id,
            "Canonical acceptance missing",
        )
        return {
            "acceptance": result,
            **prepare(
                state, "Running", app.authority(result, item_id, accepted=True), "gate_produce"
            ),
        }

    async def produce(state):
        item_id = state["item_id"]
        require(
            app.provider.item(item_id).owner == state["acceptance"]["session"]["session_id"],
            "Canonical owner differs",
        )
        prompt = (
            "Implement only the assigned candidate paths; agents own organization and native delegation. "
            "Use claims yourself only if effective policy requires them. Never mutate provider lifecycle. "
            "Run required checks and commit candidate. Arrange a fresh independent native spawn_agent "
            "with fork_context=false or fork_turns=none. Reviewer must read exact candidate and return "
            "JSON {candidate,verdict:ACCEPT|REJECT,unresolved_findings:[]}. Wait for review. Return "
            "JSON {item_id,candidate,reviewer_session,request_completion:true}. If essential operator "
            "input is missing, leave candidate clean at base and return {item_id,question:{question_id,text}}.\n"
            + json.dumps(
                {
                    "item_id": item_id,
                    "assignment": state["assignment"],
                    "answer": state.get("answer"),
                }
            )
        )
        result = await app.invoke(
            item_id,
            "graph-produce-" + str(state["turn"]),
            "orchestrator",
            prompt,
            session=app.session(state["acceptance"]),
            read_only=False,
        )
        value = app.result_json(result)
        require(value.get("item_id") == item_id, "Worker item differs")
        if value.get("question"):
            question = value["question"]
            require(
                isinstance(question, dict)
                and all(
                    isinstance(question.get(k), str) and question[k].strip()
                    for k in ("question_id", "text")
                ),
                "Invalid question",
            )
            repo = app.candidate_repository(item_id)
            require(
                not git(repo, "status", "--porcelain")
                and git(repo, "rev-parse", "HEAD") == state["base"],
                "Question contains unapproved source work",
            )
            return {
                "produced": result,
                "question": question,
                **prepare(
                    state,
                    "User Action Required",
                    app.authority(result, item_id, question=question),
                    "ask",
                ),
            }
        require(value.get("request_completion") is True, "Completion request missing")
        return {"produced": result, "candidate": value.get("candidate"), "next_node": "review"}

    def ask(state):
        answer = interrupt(
            {
                "item_id": state["item_id"],
                "revision": state["revision"],
                "question": state["question"],
            }
        )
        require(
            isinstance(answer, dict)
            and answer.get("question_id") == state["question"]["question_id"]
            and answer.get("revision") == state["revision"]
            and isinstance(answer.get("text"), str)
            and answer["text"].strip(),
            "Unbound answer",
        )
        require(app.item_quiescent(state["item_id"]), "Canonical execution is not quiescent")
        return {
            "answer": {
                "text": answer["text"],
                "digest": digest(answer["text"]),
                "question_revision": state["revision"],
            }
        }

    def persist_answer(state):
        item_id = state["item_id"]
        evidence = {
            "engine": "langgraph",
            "item_id": item_id,
            "question": state["question"],
            "answer": state["answer"],
            "before_revision": state["revision"],
        }
        path = answer_evidence_path(app, item_id, state["answer"])
        if path.exists():
            require(json.loads(path.read_text()) == evidence, "Answer evidence changed")
        else:
            atomic_json(path, evidence, exclusive=True)
        authority = {
            "role": "operator",
            "item_id": item_id,
            "invocation_id": "operator:" + digest(evidence),
            "observed_result": True,
            "question": state["question"],
            "answer": state["answer"],
            "graph_answer_evidence": str(path),
        }
        return prepare(state, "User Action Required", authority, "gate_classify_answer")

    async def classify_answer(state):
        item_id = state["item_id"]
        result = await app.invoke(
            item_id,
            "graph-answer-" + digest(state["answer"]),
            "orchestrator",
            "Classify this exact answer read-only; no implementation. Return JSON "
            "{question_id,answer_digest,disposition:approve|defer|decline|ambiguous}.\n"
            + json.dumps({"question": state["question"], "answer": state["answer"]}),
            session=app.session(state["acceptance"]),
        )
        value = app.result_json(result)
        require(
            value.get("question_id") == state["question"]["question_id"]
            and value.get("answer_digest") == state["answer"]["digest"]
            and value.get("disposition") in {"approve", "defer", "decline", "ambiguous"},
            "Answer disposition is unbound",
        )
        approved = value["disposition"] == "approve"
        return {
            "turn": state["turn"] + 1,
            **prepare(
                state,
                "Running" if approved else "User Action Required",
                app.authority(
                    result,
                    item_id,
                    question=state["question"],
                    answer=state["answer"],
                    disposition=value["disposition"],
                ),
                "gate_produce" if approved else "ask",
            ),
        }

    def review(state):
        produced = state["produced"]
        value = app.result_json(produced)
        review = verify_native_review(
            produced["session"]["native_session_id"],
            value.get("reviewer_session"),
            state["candidate"],
            app.native_sessions_root(produced["binding"]),
        )
        repo = app.candidate_repository(state["item_id"])
        checks = app.checks(repo, state["item_id"], state["candidate"], "source-checks")
        validate_candidate(
            repo,
            state["candidate"],
            state["base"],
            state["assignment"]["workflow"]["allowed_paths"],
            produced["session"]["native_session_id"],
            review,
            checks,
        )
        return {"review": review, "checks": checks}

    async def deliver(state):
        delivery = await asyncio.to_thread(
            integrate,
            app,
            state["item_id"],
            app.candidate_repository(state["item_id"]),
            state["candidate"],
            state["base"],
            state["review"],
            state["checks"],
            expected_owner=state["acceptance"]["session"]["session_id"],
            expected_revision=state["revision"],
        )
        if isinstance(app.provider, AgentProvider):
            await app.refresh_provider()
        return {"delivery": delivery}

    def close(state):
        return prepare(
            state,
            "Completed",
            app.authority(state["produced"], state["item_id"], delivery=state["delivery"]),
            "finished",
        )

    def gate(target):
        def check(state):
            try:
                app.guard(state["item_id"], record_transition=False)
                return {"next_node": target}
            except TransitionBlocked:
                current = app.provider.item(state["item_id"])
                require(current.revision == state["revision"], "Guard provider revision changed")
                usage = app.usage_view(state["item_id"])
                require(not usage["may_generate"], "Guard failed without a usage hold")
                if current.state == "User Action Required":
                    return {
                        "guard_target": target,
                        "next_node": "held",
                        "hold_decision": {
                            "reason": "Restore usage evidence before answer classification"
                        },
                    }
                require(current.state in {"Starting", "Running"}, "Unexpected guard boundary")
                admission = json.loads(app.admission_path(state["item_id"]).read_text())
                authority = app.authority(
                    admission,
                    state["item_id"],
                    incident="usage_unknown" if usage["status"] == "unknown" else "usage_limit",
                    policy="configured original-estimate generation guard",
                    usage=usage,
                )
                return {
                    "guard_target": target,
                    **prepare(state, "Holding", authority, "hold_review"),
                }

        return check

    async def hold_review_node(state):
        from .graph_hold import prepare_graph_hold_review

        packet = await prepare_graph_hold_review(app, state["item_id"])
        if packet.get("held"):
            return {"hold_decision": packet["decision"], "next_node": "held"}
        require(packet["revision"] == state["revision"], "Hold assessment revision changed")
        return {
            "hold_decision": packet["decision"],
            **prepare(
                state, packet["target"], packet["authority"], "gate_" + state["guard_target"]
            ),
        }

    def held(state):
        resume = interrupt(
            {
                "kind": "hold",
                "item_id": state["item_id"],
                "revision": state["revision"],
                "decision": state["hold_decision"],
            }
        )
        require(resume == {"review_hold": True}, "Hold requires evidence reassessment")
        return {
            "next_node": "hold_review"
            if app.provider.item(state["item_id"]).state == "Holding"
            else "gate_" + state["guard_target"]
        }

    graph = StateGraph(State)
    for name, node in {
        "freeze": freeze,
        "admit": admit,
        "effect": effect,
        "accept": accept,
        "produce": produce,
        "ask": ask,
        "persist_answer": persist_answer,
        "classify_answer": classify_answer,
        "review": review,
        "deliver": deliver,
        "close": close,
        "finished": lambda state: {},
        "hold_review": hold_review_node,
        "held": held,
        **{
            "gate_" + target: gate(target)
            for target in ("accept", "produce", "classify_answer", "deliver")
        },
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "freeze")
    graph.add_edge("freeze", "admit")
    for name in ("admit", "accept", "persist_answer", "classify_answer", "close"):
        graph.add_edge(name, "effect")
    graph.add_conditional_edges("effect", lambda state: state["next_node"])
    graph.add_conditional_edges(
        "produce", lambda state: "effect" if state["next_node"] == "ask" else "review"
    )
    graph.add_edge("ask", "persist_answer")
    graph.add_edge("review", "gate_deliver")
    for target in ("accept", "produce", "classify_answer", "deliver"):
        graph.add_conditional_edges(
            "gate_" + target,
            lambda state: "effect" if state["next_node"] == "hold_review" else state["next_node"],
        )
    graph.add_conditional_edges(
        "hold_review", lambda state: "held" if state["next_node"] == "held" else "effect"
    )
    graph.add_conditional_edges("held", lambda state: state["next_node"])
    graph.add_edge("deliver", "close")
    graph.add_edge("finished", END)
    return graph.compile(checkpointer=checkpointer)


async def run_item_graph(app, item_id, *, answer=None, hold_review=False):
    """Resume one nested item graph under caller-held item/serial admission locks."""
    require(app.execution_engine(item_id) == "langgraph", "Item is not graph-owned")
    directory = app.root / "item-graphs" / component(item_id)
    directory.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
        outer = StateGraph(State)
        outer.add_node("item", build_graph(app, None))
        outer.add_edge(START, "item")
        outer.add_edge("item", END)
        graph = outer.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": item_id}, "recursion_limit": 100}
        saved = await graph.aget_state(config)
        require(not (answer is not None and hold_review), "Choose question answer or hold review")
        pending = [entry.value for task in saved.tasks for entry in task.interrupts]
        if hold_review:
            require(len(pending) == 1 and pending[0].get("kind") == "hold", "No graph hold exists")
        if answer is not None:
            pending = [entry.value for task in saved.tasks for entry in task.interrupts]
            require(
                len(pending) == 1
                and isinstance(answer, dict)
                and pending[0].get("question")
                and answer.get("question_id") == pending[0]["question"]["question_id"]
                and answer.get("revision") == pending[0]["revision"]
                and isinstance(answer.get("text"), str)
                and answer["text"].strip(),
                "Unbound answer or no graph question exists",
            )
        incoming = (
            Command(resume={"review_hold": True})
            if hold_review
            else Command(resume=answer)
            if answer is not None
            else (None if saved.values else {"item_id": item_id})
        )
        result = await graph.ainvoke(incoming, config)
        if "__interrupt__" in result:
            result["__interrupt__"] = [entry.value for entry in result["__interrupt__"]]
        return result
