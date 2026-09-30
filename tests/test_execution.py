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
