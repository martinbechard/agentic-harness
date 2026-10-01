import asyncio
import json
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
import yaml

from backlog_harness.adapters.codex.adapter import CodexAdapter
from backlog_harness.contracts import load_config
from backlog_harness.evidence import EvidenceStore
from backlog_harness.provider import TransitionBlocked
from backlog_harness.runtime import AgentRequest
from backlog_harness.telemetry import TelemetryReceiver


@pytest.mark.parametrize(
    "mode,outcome",
    [("normal", "returned"), ("large", "returned"), ("bad", "unresolved"), ("hang", "unresolved")],
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
if "resume" in args:
 assert args[args.index("-m")+1]=="reloaded-model"
 assert 'model_reasoning_effort="medium"' in args
session=args[args.index('resume')+1] if 'resume' in args else '00000000-0000-4000-8000-000000000001'
print(json.dumps({'type':'thread.started','thread_id':session}),flush=True)
if mode=='large':print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'inventory':'x'*70000})}}),flush=True)
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
            if mode == "large":
                message = next(
                    e["text"] for e in handle.events if e.get("item_type") == "agent_message"
                )
                assert json.loads(message) == {"inventory": "x" * 70000}
            outcome_record = json.loads((path / "outcomes.jsonl").read_text().splitlines()[-1])
            if mode == "hang":
                assert outcome_record["failure_reason"] == "timeout"
            if mode == "normal":
                data["profiles"]["worker"]["model"] = "reloaded-model"
                data["profiles"]["worker"]["effort"] = "medium"
                config.write_text(yaml.safe_dump(data))
                updated = load_config(config)
                updated_binding = updated.binding("orchestrator")
                assert updated_binding.profile_digest != binding.profile_digest
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
                        snapshot=updated,
                        binding=updated_binding,
                    ),
                )
                assert resumed.session == handle.session
                audit = json.loads((next_path / "resume-binding.json").read_text())
                assert audit["previous_binding"]["profile_digest"] == binding.profile_digest
                assert audit["current_binding"]["profile_digest"] == updated_binding.profile_digest
                assert audit["current_config_digest"] == updated.file_digest
                data["profiles"]["worker"]["permissions"] = ["read"]
                config.write_text(yaml.safe_dump(data))
                changed_permissions = load_config(config)
                with pytest.raises(ValueError, match="permission changes"):
                    await adapter.resume_session(
                        handle.session,
                        replace(
                            request,
                            snapshot=changed_permissions,
                            binding=changed_permissions.binding("orchestrator"),
                        ),
                    )

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


def test_invocation_reloads_timeout_before_launch(config_file, monkeypatch):
    from backlog_harness.application import Application

    config, data = config_file
    app = Application(config)
    observed = []

    class Inspected(Exception):
        pass

    class Adapter:
        def validate_profile(self, request):
            observed.append(request.timeout_seconds)
            raise Inspected

    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: Adapter())
    for index, timeout in enumerate([180, 900]):
        if index:
            data["invocation_timeout_seconds"] = timeout
            config.write_text(yaml.safe_dump(data))
        with pytest.raises(Inspected):
            asyncio.run(app.invoke("timeout", str(index), "coordinator", "observe"))
        assert observed[-1] == timeout


def test_unsubmitted_intent_rejects_later_timeout_configuration(config_file, monkeypatch):
    from backlog_harness.application import Application

    config, data = config_file
    app = Application(config)
    calls = []

    class Inspected(Exception):
        pass

    class Adapter:
        def validate_profile(self, request):
            calls.append(request.timeout_seconds)
            raise Inspected

    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: Adapter())
    with pytest.raises(Inspected):
        asyncio.run(app.invoke("timeout", "same", "coordinator", "observe"))
    data["invocation_timeout_seconds"] = 900
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(TransitionBlocked, match="configuration changed"):
        asyncio.run(app.invoke("timeout", "same", "coordinator", "observe"))
    assert calls == [180]


def test_resume_configuration_identity_and_unsubmitted_retry(config_file, tmp_path, monkeypatch):
    from backlog_harness.runtime import SessionHandle

    config, data = config_file
    original = load_config(config)
    binding = original.binding("orchestrator")
    session = SessionHandle("canonical", "00000000-0000-4000-8000-000000000001", binding)
    data["profiles"]["worker"]["model"] = "new-model"
    config.write_text(yaml.safe_dump(data))
    current = load_config(config)
    request = AgentRequest(
        "operation",
        "invocation",
        current,
        current.binding("orchestrator"),
        "prompt",
        tmp_path / "evidence",
        None,
    )
    adapter = CodexAdapter()

    async def unsubmitted(*_):
        return "not submitted"

    monkeypatch.setattr(adapter, "_invoke", unsubmitted)
    assert asyncio.run(adapter.resume_session(session, request)) == "not submitted"
    assert asyncio.run(adapter.resume_session(session, request)) == "not submitted"
    data["profiles"]["worker"]["model"] = "other-model"
    config.write_text(yaml.safe_dump(data))
    other = load_config(config)
    with pytest.raises(ValueError, match="evidence differs"):
        asyncio.run(
            adapter.resume_session(
                session, replace(request, snapshot=other, binding=other.binding("orchestrator"))
            )
        )
    data["profiles"]["worker"]["role"] = "different-role"
    config.write_text(yaml.safe_dump(data))
    other = load_config(config)
    with pytest.raises(ValueError, match="permission changes"):
        asyncio.run(
            adapter.resume_session(
                session, replace(request, snapshot=other, binding=other.binding("orchestrator"))
            )
        )


def test_resume_rejects_enabling_user_config(config_file, tmp_path):
    from backlog_harness.runtime import SessionHandle

    config, data = config_file
    original = load_config(config)
    session = SessionHandle(
        "canonical", "00000000-0000-4000-8000-000000000001", original.binding("orchestrator")
    )
    data["agent_clis"]["primary"]["adapter_options"] = {"load_user_config": True}
    config.write_text(yaml.safe_dump(data))
    current = load_config(config)
    request = AgentRequest(
        "op", "inv", current, current.binding("orchestrator"), "prompt", tmp_path / "evidence", None
    )
    with pytest.raises(ValueError, match="permission changes"):
        asyncio.run(CodexAdapter().resume_session(session, request))


@pytest.mark.parametrize(
    "purpose,read_only,enabled,disable_memories",
    [
        ("implementation", False, True, True),
        ("implementation", True, True, True),
        ("implementation", False, False, True),
        ("proof", False, True, True),
        ("implementation", False, False, False),
    ],
)
def test_invocation_artifact_permissions(
    config_file, tmp_path, purpose, read_only, enabled, disable_memories
):
    """The actual adapter command scopes output separately from source and receipts."""
    import tomllib
    from pathlib import Path

    config, data = config_file
    fake = tmp_path / "artifact-agent"
    fake.write_text(
        "#!" + sys.executable + "\n"
        "import json,sys\n"
        "print(json.dumps({'type':'thread.started','thread_id':'00000000-0000-4000-8000-000000000001'}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'argv':sys.argv,'prompt':sys.stdin.read()})}}))\n"
        "print(json.dumps({'type':'turn.completed','usage':{'output_tokens':1}}))\n"
    )
    fake.chmod(0o755)
    data["agent_clis"]["primary"]["executable"] = str(fake)
    if not disable_memories:
        data["agent_clis"]["primary"]["adapter_options"] = {"disable_memories": False}
    data["profiles"]["worker"]["artifact_output"] = enabled
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    snapshot = load_config(config)
    binding = snapshot.binding("orchestrator")
    store = EvidenceStore(snapshot.operational_root, "item:one")

    async def run():
        with TelemetryReceiver(snapshot.operational_root) as receiver:
            inv = str(uuid4())
            path = store.begin("one:proof", inv, snapshot, binding, action="proof")
            dest = receiver.register(
                run_id="item:one", invocation_id=inv, role="orchestrator", adapter="codex"
            )
            request = AgentRequest(
                "one:proof",
                inv,
                snapshot,
                binding,
                "do proof",
                path,
                dest,
                read_only=read_only,
                purpose=purpose,
            )
            handle = await CodexAdapter().start_session(request)
            assert handle.outcome == "returned"
            value = json.loads(
                next(e["text"] for e in handle.events if e.get("item_type") == "agent_message")
            )
            assert ("features.memories=false" in value["argv"]) == disable_memories
            setting = next(
                a for a in value["argv"] if a.startswith("permissions.harness.filesystem=")
            )
            fs = tomllib.loads(setting)["permissions"]["harness"]["filesystem"]
            assert fs[":root"] == "read"
            output = path / "artifacts"
            if enabled and not read_only:
                assert fs[str(output)] == "write"
                contract = json.loads((path / "artifact-output.json").read_text())
                assert contract["path"] == str(output)
                assert contract["invocation_id"] == inv
                assert str(output) in value["prompt"]
                assert "does not enforce child isolation" in value["prompt"]
            else:
                assert not output.exists()
                assert not (path / "artifact-output.json").exists()
            assert str(path) not in fs
            assert str(snapshot.operational_root) not in fs
            if purpose == "proof" or read_only:
                assert set(fs) == (
                    {":root", str(output)} if enabled and not read_only else {":root"}
                )
            else:
                assert fs[str(Path(data["workspace"]))] == "write"

    asyncio.run(run())


def test_artifact_output_rejects_changed_binding_and_redirect(config_file, tmp_path):
    from backlog_harness.runtime import SessionHandle

    config, data = config_file
    before = load_config(config)
    session = SessionHandle(
        "owner", "00000000-0000-4000-8000-000000000001", before.binding("orchestrator")
    )
    data["profiles"]["worker"]["artifact_output"] = True
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    snapshot = load_config(config)
    binding = snapshot.binding("orchestrator")
    path = EvidenceStore(snapshot.operational_root, "item:one").begin(
        "proof", "inv", snapshot, binding, action="proof"
    )
    request = AgentRequest(
        "proof", "inv", snapshot, binding, "proof", path, None, read_only=False, purpose="proof"
    )
    with pytest.raises(ValueError, match="permission changes"):
        asyncio.run(CodexAdapter().resume_session(session, request))
    (path / "artifacts").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="redirect"):
        CodexAdapter.prepare_artifact_output(request)
    (path / "artifacts").unlink()
    with pytest.raises(ValueError, match="identity differs"):
        CodexAdapter.prepare_artifact_output(replace(request, invocation_id="other"))
    assert not (path / "artifacts").exists()
    contract = CodexAdapter.prepare_artifact_output(request)
    assert CodexAdapter.prepare_artifact_output(request) == contract
    (path / "artifact-output.json").write_text("{}")
    with pytest.raises(ValueError, match="contract changed"):
        CodexAdapter.prepare_artifact_output(request)
