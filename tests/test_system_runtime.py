"""Installed runtime checks. Agent generation remains outside these no-model scenarios.

These cover S08-S12 boundaries, not full matrix acceptance: no live subscription,
ACP exporter compatibility or interactive terminal rendering is claimed.
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from system_support import InstalledHarness


@pytest.fixture(scope="session")
def installed_harness(installed_package_python):
    return SimpleNamespace(python=installed_package_python)


def clean_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def final_json(output):
    """Run emits compact progress records followed by a formatted final result."""
    decoder = json.JSONDecoder()
    values = []
    while output.strip():
        value, end = decoder.raw_decode(output.lstrip())
        values.append(value)
        output = output.lstrip()[end:]
    return values[-1]


def python_run(installed_harness, code, *args, cwd=None):
    return subprocess.run(
        [str(installed_harness.python), "-c", code, *map(str, args)],
        cwd=cwd,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def cli(installed_harness, config, *args, cwd=None):
    return subprocess.run(
        [str(installed_harness.python.parent / "agentic-harness"), "--config", str(config), *args],
        cwd=cwd,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def fixture_config(config_file, provider):
    path, data = config_file
    data["repository"] = str(provider.repository)
    path.write_text(yaml.safe_dump(data))
    return path, data


def test_s08_installed_config_reload_isolation_and_missing_executable(
    installed_harness, config_file, provider, tmp_path
):
    path, data = fixture_config(config_file, provider)
    outside = tmp_path / "outside"
    outside.mkdir()
    first = cli(installed_harness, path, "validate", cwd=outside)
    assert first.returncode == 0, first.stderr + first.stdout
    original = json.loads(first.stdout)
    data["profiles"]["worker"]["effort"] = "high"
    path.write_text(yaml.safe_dump(data))
    changed = cli(installed_harness, path, "validate", cwd=outside)
    assert changed.returncode == 0, changed.stdout
    assert json.loads(changed.stdout)["config_digest"] != original["config_digest"]
    data["workspace"] = data["repository"]
    path.write_text(yaml.safe_dump(data))
    invalid = cli(installed_harness, path, "validate", cwd=outside)
    assert invalid.returncode != 0
    data["workspace"] = str(tmp_path / "candidate")
    data["agent_clis"]["primary"]["executable"] = str(tmp_path / "missing-executable")
    path.write_text(yaml.safe_dump(data))
    assert cli(installed_harness, path, "validate", cwd=outside).returncode != 0
    # Inspection remains available and explicitly reports the generation configuration fence.
    status = cli(installed_harness, path, "status", cwd=outside)
    assert status.returncode == 0, status.stdout + status.stderr
    view = json.loads(status.stdout)
    assert view["generation_configuration_error"]
    assert view["items"][0]["item_id"] == "item-one"
    assert not (provider.repository / ".agent-ops/backlog-harness/runs").exists()


def test_s09_installed_capacity_uncertainty_and_controller_stop(
    installed_harness, config_file, provider
):
    path, _ = fixture_config(config_file, provider)
    result = python_run(
        installed_harness,
        r"""
import asyncio, json, os, sys
from pathlib import Path
from backlog_harness.application import Application
from backlog_harness.coordination import RunController
from backlog_harness.evidence import atomic_json, EvidenceStore
from backlog_harness.provider import TransitionBlocked
app = Application(Path(sys.argv[1]))
async def check():
    async with app.capacity_slot() as first:
        assert first.parent.name == '0'
    # A retained reservation with no verifiable invocation is conservatively occupied.
    unknown = EvidenceStore(app.root, 'capacity-fixture').begin('pending','capacity-invocation',app.config,app.config.binding('orchestrator'),action='fixture',request_digest='fixture')
    EvidenceStore.requested(unknown)
    atomic_json(app.root / 'capacity/0/reservation.json', {'evidence_path': str(unknown)})
    try:
        async with app.capacity_slot():
            raise AssertionError('uncertain capacity admitted work')
    except TransitionBlocked:
        pass
    (app.root / 'capacity/0/reservation.json').unlink()
    EvidenceStore.outcome(unknown, 'submission_rejected')
    controller = RunController(app)
    controller.state = 'Running'
    controller.pause()
    assert controller.state == 'AdmissionPaused' and not controller.admission_open
    started = asyncio.Event()
    async def pending():
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(pending())
    controller.tasks['fixture'] = task
    await started.wait()
    stopped = await controller.stop()
    assert task.cancelled() and stopped['admission_open'] is False
    assert stopped['state'] == 'Available'
    saved = json.loads(controller.path.read_text())
    assert saved['active_items'] == []
    from backlog_harness.adapters.codex.adapter import CodexAdapter
    from backlog_harness.runtime import InvocationHandle
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)', start_new_session=True)
    adapter = CodexAdapter()
    adapter.processes['interrupt-fixture'] = process
    try:
        answer = await adapter.request_interrupt(InvocationHandle('interrupt-fixture', app.root))
        assert answer['outcome'] == 'requested' and answer['verified_stopped'] is False
        await asyncio.wait_for(process.wait(), 5)
        try:
            os.killpg(process.pid, 0)
            raise AssertionError('native process group remains live')
        except ProcessLookupError:
            pass
    finally:
        if process.returncode is None:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    print(json.dumps({'pause': 'verified', 'stop': stopped['state'], 'capacity': 'uncertain-fenced', 'native_interrupt':'group absent'}))
asyncio.run(check())
""",
        path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["capacity"] == "uncertain-fenced"


def test_s10_installed_real_http_telemetry_attribution_and_corruption(
    installed_harness, config_file, provider
):
    config, _ = fixture_config(config_file, provider)
    result = python_run(
        installed_harness,
        r"""
import json, sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from backlog_harness.telemetry import TelemetryReceiver, Sink
from backlog_harness.application import Application
from backlog_harness.provider import TransitionBlocked
app = Application(Path(sys.argv[1]))
root = app.root
payload = {'resourceSpans':[{'resource':{},'scopeSpans':[{'scope':{'name':'codex'},'spans':[{'traceId':'a'*32,'spanId':'b'*16,'name':'handle_responses','attributes':[{'key':'gen_ai.usage.output_tokens','value':{'intValue':'7'}}]}]}]}]}
with TelemetryReceiver(root) as receiver:
    dest = receiver.register(run_id='run',invocation_id='inv',role='worker',adapter='codex',item_id='item',provider_id='file:fixture')
    def send(token):
        request = Request(dest.endpoint, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json','x-harness-invocation':token})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as error:
            return error.code
    assert send('wrong') == 403
    assert send(dest.token) == 200 and send(dest.token) == 200
    report = receiver.report(dest)
    assert report['span_count'] == 1
    rows = [json.loads(line) for line in dest.path.read_text().splitlines()]
    attributes = rows[0]['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['attributes']
    values = {a['key']:a['value'] for a in attributes}
    assert values['harness.invocation.id']['stringValue'] == 'inv'
    assert values['harness.work_item.id']['stringValue'] == 'item'
    assert dest.token not in dest.path.read_text()
    assert Sink(dest.path, {}).evidence_digest() == report['evidence_sha256']
    result = {'outcome':'returned','telemetry':report,'telemetry_path':str(dest.path)}
    app.validate_invocation_result(result)
    # A rejected export cannot erase returned work; its usage stays unknown.
    request = Request(dest.endpoint, data=b'not json', headers={'x-harness-invocation':dest.token})
    try:
        urlopen(request, timeout=5)
        raise AssertionError('malformed export accepted')
    except HTTPError as error:
        assert error.code == 400
    result['telemetry'] = receiver.report(dest)
    assert result['telemetry']['rejected_exports'] > 0
    app.validate_invocation_result(result)
    from backlog_harness.analytics import observed_invocation_usage
    assert observed_invocation_usage(result) is None
    empty = receiver.register(run_id='run',invocation_id='empty',role='worker',adapter='codex',item_id='item',provider_id='file:fixture')
    empty_result = {'outcome':'returned','telemetry':receiver.report(empty),'telemetry_path':str(empty.path)}
    app.validate_invocation_result(empty_result)
    assert observed_invocation_usage(empty_result) is None
    dest.path.write_text(dest.path.read_text().replace('handle_responses','corrupted_name'))
    assert Sink(dest.path, {}).evidence_digest() != report['evidence_sha256']
    try:
        app.validate_invocation_result(result)
        raise AssertionError('changed telemetry passed the production invocation gate')
    except TransitionBlocked:
        pass
print(json.dumps({'http':True,'deduplicated':True,'attribution':True,'corruption_detected':True}))
""",
        config,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["corruption_detected"] is True


def test_s12_installed_wheel_source_parity_and_supported_adapter(
    installed_harness, config_file, provider
):
    path, data = fixture_config(config_file, provider)
    home = path.parent / "auth-home"
    home.mkdir()
    (home / "config.toml").write_text("")
    data["agent_clis"]["primary"]["adapter_options"] = {
        "load_user_config": True,
        "codex_home": str(home),
    }
    path.write_text(yaml.safe_dump(data))
    source = Path(__file__).resolve().parents[1] / "src/backlog_harness"
    result = python_run(
        installed_harness,
        r"""
import asyncio, hashlib, json, sys
from pathlib import Path
import backlog_harness
from backlog_harness.application import Application
from backlog_harness.adapters.registry import AdapterRegistry
from backlog_harness.runtime import AgentRequest
from backlog_harness.telemetry import TelemetryDestination
from backlog_harness.provider import TransitionBlocked
root = Path(backlog_harness.__file__).parent
source = Path(sys.argv[1])
assert root.resolve() != source.resolve(), 'source checkout imported instead of installed wheel'
files = sorted(source.rglob('*.py'))
for path in files:
    assert (root / path.relative_to(source)).read_bytes() == path.read_bytes(), str(path)
registry = AdapterRegistry()
assert set(registry.factories) == {'codex'}
assert registry.resolve('codex').__class__.__name__ == 'CodexAdapter'
try:
    registry.resolve('codex-acp')
except ValueError:
    pass
else:
    raise AssertionError('retired transport remained available')
print(json.dumps({'installed_modules':len(files),'adapter':'codex'}))
""",
        source,
        path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["installed_modules"] > 20


def test_s11_installed_populated_status_and_readonly_dashboard(
    installed_harness, config_file, provider
):
    import selectors
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    path, _ = fixture_config(config_file, provider)
    seeded = python_run(
        installed_harness,
        r"""
import json, sys
from pathlib import Path
from backlog_harness.application import Application
from backlog_harness.evidence import EvidenceStore, JsonlWriter
from backlog_harness.contracts import utcnow
app = Application(Path(sys.argv[1]))
store = EvidenceStore(app.root, 'item:item-one')
path = store.begin('view-fixture','fixture-invocation',app.config,app.config.binding('orchestrator'),action='view-fixture',item_id='item-one',request_digest='fixture')
EvidenceStore.requested(path)
EvidenceStore.session(path, {'session_id':'portable-fixture','native_session_id':'native-fixture'})
JsonlWriter(path / 'events.jsonl').append({'at':utcnow(),'event_id':'fixture-event','type':'thread.started'})
print(json.dumps({'seeded':'requested fixture, no generation'}))
""",
        path,
    )
    assert seeded.returncode == 0, seeded.stderr
    status = cli(installed_harness, path, "status")
    assert status.returncode == 0, status.stderr + status.stdout
    expected = json.loads(status.stdout)
    assert expected["counts"] == {"Ready": 1}
    assert expected["invocations"][0]["invocation_id"] == "fixture-invocation"
    assert expected["invocations"][0]["portable_session_id"] == "portable-fixture"
    assert "fixture-invocation" in expected["uncertainty"]
    process = subprocess.Popen(
        [
            str(installed_harness.python),
            "-c",
            "from backlog_harness.cli import main; import sys; raise SystemExit(main(sys.argv[1:]))",
            "--config",
            str(path),
            "dashboard",
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=clean_env(),
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "Dashboard did not announce listener"
        base = process.stdout.readline().strip().split("Read-only dashboard: ", 1)[1]
        with urlopen(base + "/api/snapshot", timeout=5) as response:
            actual = json.load(response)
        assert actual["items"] == expected["items"]
        assert actual["invocations"] == expected["invocations"]
        assert actual["read_only"] is True
        try:
            urlopen(Request(base + "/api/snapshot", data=b"{}"), timeout=5)
            raise AssertionError("Dashboard accepted a mutation")
        except HTTPError as error:
            assert error.code == 405
        with urlopen(base + "/dashboard.js", timeout=5) as response:
            assert response.status == 200 and response.read()
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_s08_installed_auth_failure_cannot_submit(installed_harness, tmp_path):
    fixture = InstalledHarness.create(tmp_path / "auth-denial", installed_harness.python)
    data = yaml.safe_load(fixture.config_path.read_text())
    agent = Path(data["agent_clis"]["primary"]["executable"])
    agent.write_text(
        "#!" + str(installed_harness.python) + "\n"
        "import sys\n"
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("codex-cli 0.159.2"); raise SystemExit(0)\n'
        'if sys.argv[1:] == ["login", "status"]:\n'
        "    raise SystemExit(1)\n"
        'raise RuntimeError("AUTH FAILURE MUST PREVENT GENERATION")\n'
    )
    agent.chmod(0o755)
    result = fixture.run("refresh-provider")
    assert result.returncode != 0
    assert "launch capability" in result.stdout + result.stderr, result.stdout + result.stderr
    assert not list(
        (fixture.repo / ".agent-ops/backlog-harness/runs").glob(
            "*/operations/*/invocations/*/requested.json"
        )
    )


def test_s09_installed_watch_cancellation_restart_does_not_duplicate(installed_harness, tmp_path):
    import signal
    import time

    fixture = InstalledHarness.create(
        tmp_path / "watch-stop",
        installed_harness.python,
        dispatcher=Path(__file__).with_name("system_runtime_agent.py"),
    )
    data = yaml.safe_load(fixture.config_path.read_text())
    data["invocation_timeout_seconds"] = 90
    fixture.config_path.write_text(yaml.safe_dump(data))
    process = subprocess.Popen(
        [
            str(fixture.python.parent / "agentic-harness"),
            "--config",
            str(fixture.config_path),
            "run",
            "--watch",
        ],
        cwd=fixture.repo,
        env=fixture.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    ready = fixture.repo.parent / "slow-agent-ready.json"
    child = None
    try:
        deadline = time.monotonic() + 40
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.exists(), "Watch did not reach canonical work fixture"
        child = json.loads(ready.read_text())
        calls = (fixture.repo.parent / "agent-calls.jsonl").read_text().splitlines()
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode != 0, stdout + stderr
        with pytest.raises(ProcessLookupError):
            os.killpg(child["pgid"], 0)
        run = json.loads((fixture.repo / ".agent-ops/backlog-harness/run.json").read_text())
        assert run["admission_open"] is False and not run["active_items"]
        retained_intents = {
            path: path.read_bytes()
            for path in (fixture.repo / ".agent-ops/backlog-harness/runs").glob(
                "*/operations/*/invocations/*/intent.json"
            )
        }
        restarted = fixture.run("run", "--until-terminal", timeout=20)
        assert restarted.returncode == 2, restarted.stdout + restarted.stderr
        assert final_json(restarted.stdout)["outcome"] == "blocked"
        after = (fixture.repo.parent / "agent-calls.jsonl").read_text().splitlines()
        assert after[: len(calls)] == calls
        # Unknown interrupted usage may require one bounded provider Holding mutation;
        # it must never repeat inventory, admission or implementation.
        added = [json.loads(line) for line in after[len(calls) :]]
        assert len(added) <= 1
        for call in added:
            assert "Perform only this authorized provider transition" in call["prompt"]
            operation = json.loads(call["prompt"][call["prompt"].index("\n{") + 1 :])
            assert operation["target"] == "Holding"
        assert (
            sum(
                "Running is now recorded for your exact session" in json.loads(line)["prompt"]
                for line in after
            )
            == 1
        )
        again = fixture.run("run", "--until-terminal", timeout=20)
        assert again.returncode == 2 and final_json(again.stdout)["outcome"] == "blocked"
        assert (fixture.repo.parent / "agent-calls.jsonl").read_text().splitlines() == after
        assert all(path.read_bytes() == content for path, content in retained_intents.items())
        assert not (fixture.candidate / "answer.txt").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        if child:
            try:
                os.killpg(child["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_s11_completed_installed_run_populates_views_without_invocations(
    installed_harness, tmp_path
):
    import selectors
    from urllib.request import urlopen

    fixture = InstalledHarness.create(tmp_path / "completed-view", installed_harness.python)
    completed = fixture.run("run-item", "item-one", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = (fixture.repo.parent / "agent-calls.jsonl").read_text().splitlines()
    status = fixture.run("status")
    assert status.returncode == 0, status.stdout + status.stderr
    expected = json.loads(status.stdout)
    assert expected["counts"] == {"Completed": 1}
    assert expected["trace_span_count"] > 0 and expected["invocations"]
    assert expected["telemetry_receipt_binding"] == "content_verified"
    process = subprocess.Popen(
        [
            str(fixture.python.parent / "agentic-harness"),
            "--config",
            str(fixture.config_path),
            "dashboard",
            "--port",
            "0",
        ],
        cwd=fixture.repo,
        env=fixture.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10)
        base = process.stdout.readline().strip().split("Read-only dashboard: ", 1)[1]
        with urlopen(base + "/api/snapshot", timeout=5) as response:
            dashboard = json.load(response)
        assert dashboard["items"] == expected["items"]
        assert dashboard["invocations"] == expected["invocations"]
        assert dashboard["traces"] == expected["traces"]
        assert dashboard["trace_span_count"] == expected["trace_span_count"]
        assert (fixture.repo.parent / "agent-calls.jsonl").read_text().splitlines() == calls
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_installed_relocated_receipts_keep_status_and_tamper_detection(installed_harness, tmp_path):
    fixture = InstalledHarness.create(tmp_path / "relocated-view", installed_harness.python)
    completed = fixture.run("run-item", "item-one", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls_path = fixture.repo.parent / "agent-calls.jsonl"
    calls = calls_path.read_bytes()
    root = fixture.repo / ".agent-ops/backlog-harness"
    alias = tmp_path / "historical-evidence"
    alias.symlink_to(root, target_is_directory=True)
    receipts = list(root.glob("runs/*/operations/*/invocations/*/telemetry.json"))
    assert receipts
    target = None
    for receipt in receipts:
        value = json.loads(receipt.read_text())
        target = Path(value["path"])
        value["path"] = str(alias / target.relative_to(root))
        receipt.write_text(json.dumps(value))
    status = fixture.run("status")
    assert status.returncode == 0, status.stdout + status.stderr
    view = json.loads(status.stdout)
    assert view["counts"] == {"Completed": 1}
    assert view["telemetry_receipt_binding"] == "content_verified"
    original = target.read_bytes()
    records = [json.loads(line) for line in original.splitlines()]
    records[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = "tampered"
    target.write_text("".join(json.dumps(row) + "\n" for row in records))
    tampered = fixture.run("status")
    assert tampered.returncode == 2
    assert "Telemetry receipt mismatch" in tampered.stderr
    target.write_bytes(original)
    outside = root / "not-in-inventory.jsonl"
    outside.write_bytes(original)
    receipt = receipts[-1]
    value = json.loads(receipt.read_text())
    value["path"] = str(alias / outside.relative_to(root))
    receipt.write_text(json.dumps(value))
    missing = fixture.run("status")
    assert missing.returncode == 2
    assert "outside the projection inventory" in missing.stderr
    assert calls_path.read_bytes() == calls
