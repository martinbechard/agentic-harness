import asyncio
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
import yaml

from backlog_harness.adapters.codex.adapter import CodexAdapter
from backlog_harness.contracts import load_config
from backlog_harness.evidence import EvidenceStore
from backlog_harness.runtime import AgentRequest
from backlog_harness.telemetry import TelemetryReceiver


@pytest.mark.parametrize(
    "mode,outcome", [("normal", "returned"), ("bad", "unresolved"), ("hang", "unresolved")]
)
def test_actual_subprocess_boundaries(config_file, tmp_path, mode, outcome):
    config, data = config_file
    fake = tmp_path / "fake-agent"
    fake.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import json,sys,time
mode="""
        + repr(mode)
        + """
if mode=='hang':time.sleep(10)
if mode=='bad':print('not json');sys.exit(0)
args=sys.argv
session=args[args.index('resume')+1] if 'resume' in args else '00000000-0000-4000-8000-000000000001'
print(json.dumps({'type':'thread.started','thread_id':session}),flush=True)
sys.stderr.write('x'*200000)
print(json.dumps({'type':'turn.completed','usage':{'output_tokens':1}}),flush=True)
"""
    )
    fake.chmod(0o755)
    data["agent_clis"]["primary"]["executable"] = str(fake)
    config.write_text(yaml.safe_dump(data))
    snapshot = load_config(config)
    binding = snapshot.binding("orchestrator")
    store = EvidenceStore(tmp_path / "ops", "run")

    async def check():
        with TelemetryReceiver(tmp_path / "ops") as receiver:
            inv = str(uuid4())
            path = store.begin("op", inv, snapshot, binding, action="test")
            dest = receiver.register(
                run_id="run", invocation_id=inv, role="orchestrator", adapter="codex"
            )
            request = AgentRequest(
                "op",
                inv,
                snapshot,
                binding,
                "test",
                path,
                dest,
                timeout_seconds=0.2 if mode == "hang" else 3,
            )
            adapter = CodexAdapter()
            handle = await adapter.start_session(request)
            assert handle.outcome == outcome
            assert not adapter.processes
            if mode == "normal":
                next_id = str(uuid4())
                next_path = store.begin("resume", next_id, snapshot, binding, action="resume")
                next_dest = receiver.register(
                    run_id="run", invocation_id=next_id, role="orchestrator", adapter="codex"
                )
                resumed = await adapter.resume_session(
                    handle.session,
                    replace(
                        request,
                        operation_id="resume",
                        invocation_id=next_id,
                        evidence_path=next_path,
                        telemetry=next_dest,
                    ),
                )
                assert resumed.session == handle.session

    asyncio.run(check())


def test_provider_invocation_uses_authoritative_workspace(config_file, monkeypatch):
    from pathlib import Path

    from backlog_harness.adapters.registry import AdapterRegistry
    from backlog_harness.application import Application

    config, data = config_file
    app = Application(config)
    observed = []

    class Inspected(Exception):
        pass

    class Adapter:
        def validate_profile(self, request):
            observed.append(request.snapshot.data["workspace"])
            raise Inspected

    monkeypatch.setattr(AdapterRegistry, "resolve", lambda *_: Adapter())
    for purpose, expected in [
        ("provider", data["repository"]),
        ("implementation", data["workspace"]),
    ]:
        with pytest.raises(Inspected):
            asyncio.run(app.invoke("one", purpose, "coordinator", "observe", purpose=purpose))
        assert Path(observed[-1]) == Path(expected)


def test_saved_implementation_is_not_a_provider_result(config_file):
    from backlog_harness.application import Application
    from backlog_harness.contracts import digest
    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import TransitionBlocked

    config, _ = config_file
    app = Application(config)
    atomic_json(
        app._stage_path("one", "observe"),
        {"request_digest": digest("observe"), "outcome": "returned"},
    )
    with pytest.raises(TransitionBlocked, match="Stage request changed"):
        asyncio.run(app.invoke("one", "observe", "coordinator", "observe", purpose="provider"))
    assert not list((app.root / "runs").glob("*/operations/*/invocations/*/intent.json"))


def test_provider_write_requires_bound_transition(config_file, provider):
    from dataclasses import asdict
    from types import SimpleNamespace

    from backlog_harness.contracts import digest
    from backlog_harness.evidence import atomic_json, component
    from backlog_harness.provider import TransitionBlocked

    config, _ = config_file
    snapshot = load_config(config)
    item = provider.item("item-one")
    prompt = "Recheck revision and perform the authorized reservation using management skills."
    record = {
        "repository": str(snapshot.repository),
        "stage_operation": "item-one:provider-reserve",
        "prompt_digest": digest(prompt),
        "item": asdict(item),
        "target": "Starting",
        "authority": {
            "role": "coordinator",
            "operation": "new",
            "item_id": item.item_id,
            "invocation_id": "decision",
            "observed_result": True,
        },
        "paths": [item.path],
    }

    def request(value):
        identity = digest(value)
        atomic_json(
            snapshot.operational_root
            / "provider-agent-operations"
            / component(identity)
            / "requested.json",
            value,
        )
        return SimpleNamespace(
            snapshot=snapshot,
            read_only=False,
            provider_operation=identity,
            binding=snapshot.binding("coordinator"),
            operation_id=record["stage_operation"],
            prompt=prompt,
        )

    bound = request(record)
    assert CodexAdapter.validate_provider_request(bound, snapshot.repository) == [item.path]
    for changed, message in [
        ({"authority": {**record["authority"], "observed_result": False}}, "Observed actor"),
        ({"authority": {**record["authority"], "role": "orchestrator"}}, "actor differs"),
        ({"prompt_digest": digest("other")}, "prompt differs"),
        ({"paths": ["src/implementation.py"]}, "escapes backlog"),
    ]:
        with pytest.raises(TransitionBlocked, match=message):
            CodexAdapter.validate_provider_request(
                request({**record, **changed}), snapshot.repository
            )
    bound.provider_operation = None
    with pytest.raises(TransitionBlocked, match="operation is required"):
        CodexAdapter.validate_provider_request(bound, snapshot.repository)
