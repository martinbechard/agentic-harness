import json
import subprocess
import sys

import pytest
import yaml

from backlog_harness.cli import Events, load_config


def test_every_activity_has_matching_console_log_and_otel_record(tmp_path, capsys):
    emit = Events(tmp_path)
    emit("dispatched", item="one")
    emit("agent_output", text="agent activity")
    emit(
        "heartbeat",
        monitoring_status="idle",
        interval_seconds=10,
        active_items=[],
        active_invocations=[],
    )
    console = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    logged = [json.loads(line) for line in (tmp_path / "activities.jsonl").read_text().splitlines()]
    otel = [json.loads(line) for line in (tmp_path / "otel.jsonl").read_text().splitlines()]
    assert len(console) == len(logged) == len(otel) == 3
    for visible, activity, export in zip(console, logged, otel, strict=True):
        record = export["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        assert record["timeUnixNano"] == activity.pop("timeUnixNano")
        assert json.loads(record["body"]["stringValue"]) == activity == visible


def config(tmp_path):
    return {
        "project": str(tmp_path),
        "state": "state",
        **{role: [sys.executable, "agent.py"] for role in ("access", "development", "merge")},
    }


def test_configuration_paths_are_relative_to_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config(tmp_path)))
    assert load_config(path)["state"] == str(tmp_path / "state")


@pytest.mark.parametrize(
    "change",
    [
        {"access": "shell command"},
        {"development": [1]},
        {"project": "/nonexistent-project"},
        {"state": None},
        {"run_item": "one"},
        {"access_timeout": 0},
        {"decision_timeout": 0},
        {"scheduling": {"manual_item": "one"}},
    ],
)
def test_invalid_or_retired_configuration_is_rejected(tmp_path, change):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**config(tmp_path), **change}))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("module", ["backlog_harness", "backlog_harness.cli"])
def test_retired_commands_are_not_exposed(module):
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    for unsupported in ("run-item", "answer", "dashboard", "reconcile"):
        assert unsupported not in result.stdout
    assert "--epic" in result.stdout


def test_invalid_config_has_clear_cli_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("[]")
    result = subprocess.run(
        [sys.executable, "-m", "backlog_harness", "--config", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Configuration must contain" in result.stderr
