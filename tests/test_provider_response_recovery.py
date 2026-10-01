"""Historical clipped output can be restored without repeating a provider call."""

import json
from pathlib import Path

import pytest
from test_observation_recovery import prepare_interrupted_observation

from backlog_harness.contracts import utcnow
from backlog_harness.provider import TransitionBlocked


@pytest.mark.parametrize("case", ["valid", "different", "ambiguous", "identity"])
def test_restore_exact_completed_provider_response(config_file, provider, monkeypatch, case):
    app, operation, _ = prepare_interrupted_observation(config_file, provider)
    result = json.loads(
        app._stage_path("provider-inventory", operation.split(":", 1)[1]).read_text()
    )
    full = json.dumps({"inventory": "x" * 70000})
    result.update(
        outcome="returned", text=full[:16000], events=[{"type": "turn.completed", "at": utcnow()}]
    )
    # Invocation/telemetry validation has separate coverage; isolate native result restoration.
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    record = {
        "timestamp": utcnow(),
        "payload": {"type": "task_complete", "last_agent_message": full},
    }
    records = [record]
    if case == "different":
        record["payload"]["last_agent_message"] = "different"
    elif case == "ambiguous":
        records.append(record)
    elif case == "identity":
        result["session"]["native_session_id"] = "other-session"
    monkeypatch.setattr(
        "backlog_harness.native_evidence.native_records", lambda *_: (records, "native-hash")
    )
    if case != "valid":
        with pytest.raises(TransitionBlocked):
            app.result_json(result)
        return
    assert app.result_json(result) == json.loads(full)
    assert len(result["text"]) == 16000
    receipt = json.loads(
        (Path(result["evidence_path"]) / "native-response-recovery.json").read_text()
    )
    assert receipt["text"] == full
    assert receipt["invocation_id"] == result["invocation_id"]


@pytest.mark.parametrize("omissions", ["unknown", "none"])
@pytest.mark.parametrize("reason", ["Omissions were checked against the source.", "", None])
def test_refresh_resumes_for_typed_clarification(config_file, provider, monkeypatch, omissions, reason):
    import asyncio
    from copy import deepcopy

    app, operation, _ = prepare_interrupted_observation(config_file, provider)
    original = json.loads(
        app._stage_path("provider-inventory", operation.split(":", 1)[1]).read_text()
    )
    original["text"] = json.dumps({
        "items": [{"item_id": "item-one"}],
        "dependencies": {},
        "dependency_omissions": "unknown",
        "policy": {"eligible": ["item-one"]},
    })
    clarification = deepcopy(original)
    clarification["invocation_id"] = "clarification-invocation"
    clarification["text"] = json.dumps({
        "dependency_omissions": omissions, "admission_open": False, "reason": reason,
    })
    calls = []
    accepted = []

    async def invoke(*args, **kwargs):
        calls.append((args, kwargs))
        return original if len(calls) == 1 else clarification

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    monkeypatch.setattr(app, "result_json", lambda result: json.loads(result["text"]))
    monkeypatch.setattr(
        app, "_accept_provider_observation",
        lambda *args, **kwargs: accepted.append(kwargs["value"]),
    )
    if not reason:
        with pytest.raises(TransitionBlocked, match="semantics are incomplete"):
            asyncio.run(app.refresh_provider())
        assert not accepted
        return
    asyncio.run(app.refresh_provider())
    assert len(calls) == 2
    assert calls[1][1]["session"] == app.session(original)
    assert calls[1][0][1].startswith("clarify-")
    assert accepted[0]["policy"]["eligible"] is False
    assert accepted[0]["observation_invocations"] == [
        original["invocation_id"], "clarification-invocation",
    ]
    if omissions == "none":
        assert "dependency_omissions" not in accepted[0]
        assert accepted[0]["dependencies"] == {"item-one": []}
    else:
        assert accepted[0]["dependencies"] == {}
