"""A durable pilot cannot be advanced by the legacy engine or unbound answers."""

import asyncio
import json
from dataclasses import replace

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked


def graph_app(config_file, provider=None):
    config, data = config_file
    if provider:
        data.update(repository=str(provider.repository), provider_interaction="agent")
    data["workflow"]["items"] = {
        "item-one": {"engine": "langgraph", "allowed_paths": ["answer.py"]}
    }
    config.write_text(yaml.safe_dump(data))
    return Application(config), data


def test_bound_engine_cannot_switch_or_enter_legacy_answer(config_file):
    app, data = graph_app(config_file)
    assert app.execution_engine("item-one", bind=True) == "langgraph"
    with pytest.raises(TransitionBlocked, match="graph resume"):
        app.require_legacy_execution("item-one")
    data["workflow"]["items"]["item-one"]["engine"] = "legacy"
    app.config_path.write_text(yaml.safe_dump(data))
    with pytest.raises(TransitionBlocked, match="cannot change"):
        app.execution_engine("item-one")


def test_graph_rejects_import_of_existing_legacy_execution(config_file):
    app, _ = graph_app(config_file)
    atomic_json(app._stage_path("item-one", "accept"), {"historical": True})
    with pytest.raises(TransitionBlocked, match="fresh execution"):
        app.execution_engine("item-one", bind=True)


@pytest.mark.parametrize("fault", [None, "path", "invocation", "answer", "engine"])
def test_agent_provider_requires_exact_graph_answer_authority(
    config_file, provider, monkeypatch, fault
):
    app, _ = graph_app(config_file, provider)
    app.execution_engine("item-one", bind=True)
    item = replace(provider.item("item-one"), state="User Action Required")
    question = {"question_id": "q", "text": "Proceed?"}
    answer = {"text": "yes", "digest": digest("yes"), "question_revision": item.revision}
    saved = {
        "engine": "langgraph",
        "item_id": item.item_id,
        "question": question,
        "answer": answer,
        "before_revision": item.revision,
    }
    path = (
        app.root / "item-graphs" / component(item.item_id) / ("answer-" + digest(answer) + ".json")
    )
    atomic_json(path, saved)
    authority = {
        "role": "operator",
        "item_id": item.item_id,
        "observed_result": True,
        "invocation_id": "operator:" + digest(saved),
        "question": question,
        "answer": answer,
        "graph_answer_evidence": str(path),
    }
    if fault == "path":
        authority["graph_answer_evidence"] = str(path.parent / "unrelated.json")
    elif fault == "invocation":
        authority["invocation_id"] = "operator:unrelated"
    elif fault == "answer":
        altered = json.loads(path.read_text())
        altered["answer"]["text"] = "different"
        atomic_json(path, altered)
    elif fault == "engine":
        altered = dict(saved, engine="legacy")
        atomic_json(path, altered)
    calls = []

    async def invoke(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("Reached verified provider invocation")

    monkeypatch.setattr(app, "invoke", invoke)
    if fault:
        with pytest.raises(TransitionBlocked):
            asyncio.run(
                app.invoke_provider_transition(item, "User Action Required", authority, [item.path])
            )
        assert not calls
    else:
        with pytest.raises(RuntimeError, match="Reached verified provider invocation"):
            asyncio.run(
                app.invoke_provider_transition(item, "User Action Required", authority, [item.path])
            )
        assert len(calls) == 1
