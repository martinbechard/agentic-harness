"""CLI boundaries preserve operation identity and exact user-supplied evidence paths."""

import json

import pytest

from backlog_harness.application import Application
from backlog_harness.cli import main


@pytest.mark.parametrize(
    "command,service,flag",
    [
        ("reconcile-stopped-owner", "reconcile_stopped_owner", "--evidence"),
        ("record-estimate", "record_estimate", "--decision"),
        ("defer-item", "defer_item", "--question"),
        ("reconcile-provider-effect", "reconcile_starting_effect", "--evidence"),
    ],
)
def test_evidence_commands_route_exact_identity_and_path(
    config_file, monkeypatch, capsys, command, service, flag
):
    from backlog_harness import estimation, recovery_flow

    config, _data = config_file
    calls, gates = [], []

    def record(app, identity, path):
        assert isinstance(app, Application)
        calls.append((identity, path))
        return {"recorded": identity}

    async def async_record(*args):
        return record(*args)

    module = estimation if command == "record-estimate" else recovery_flow
    monkeypatch.setattr(
        module, service, record if command == "reconcile-provider-effect" else async_record
    )
    monkeypatch.setattr(
        Application, "require_legacy_execution", lambda self, identity: gates.append(identity)
    )
    evidence = config.parent / "evidence with spaces.json"
    assert main(["--config", str(config), command, "exact-id", flag, str(evidence)]) == 0
    assert calls == [("exact-id", evidence)]
    assert gates == (["exact-id"] if command == "reconcile-stopped-owner" else [])
    assert json.loads(capsys.readouterr().out) == {"recorded": "exact-id"}


def test_recover_provider_uses_inspection_config_without_valid_generation_profile(
    config_file, monkeypatch, capsys
):
    import yaml

    config, data = config_file
    data["profiles"] = {}
    config.write_text(yaml.safe_dump(data))
    calls = []

    def recover(app, operation):
        calls.append(operation)
        assert app.config.data["generation_configuration_error"]
        return {"operation": operation}

    monkeypatch.setattr(Application, "recover_provider", recover)
    assert main(["--config", str(config), "recover-provider", "existing-operation"]) == 0
    assert calls == ["existing-operation"]
    assert json.loads(capsys.readouterr().out) == {"operation": "existing-operation"}


def test_observation_resume_preserves_operation_and_native_session(
    config_file, monkeypatch, capsys
):
    config, _data = config_file
    calls = []

    async def resume(app, operation, session):
        calls.append((operation, session))
        return {"resumed": True}

    monkeypatch.setattr(Application, "resume_provider_observation", resume)
    assert (
        main(
            ["--config", str(config), "resume-provider-observation", "operation", "native-session"]
        )
        == 0
    )
    assert calls == [("operation", "native-session")]
    assert json.loads(capsys.readouterr().out) == {"resumed": True}


def test_hold_review_passes_requested_ceiling_and_reference(config_file, monkeypatch, capsys):
    config, _data = config_file
    calls = []

    async def review(app, item, *, requested_ceiling, reference):
        calls.append((item, requested_ceiling, reference))
        return {"reviewed": True}

    monkeypatch.setattr(Application, "review_hold", review)
    assert (
        main(
            [
                "--config",
                str(config),
                "review-hold",
                "item",
                "--ceiling",
                "250",
                "--reference",
                "operator decision",
            ]
        )
        == 0
    )
    assert calls == [("item", 250.0, "operator decision")]
    assert json.loads(capsys.readouterr().out) == {"reviewed": True}


def test_refresh_rejects_direct_provider_without_launching(config_file, capsys):
    assert main(["--config", str(config_file[0]), "refresh-provider"]) == 2
    assert "requires provider_interaction: agent" in json.loads(capsys.readouterr().err)["error"]


def test_dashboard_cli_serves_and_closes_its_http_listener(config_file, monkeypatch, capsys):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from urllib.request import urlopen

    from backlog_harness import dashboard

    created = threading.Event()
    servers = []
    original = dashboard.create_dashboard_server

    def create(*args, **kwargs):
        server = original(*args, **kwargs)
        servers.append(server)
        created.set()
        return server

    monkeypatch.setattr(dashboard, "create_dashboard_server", create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(main, ["--config", str(config_file[0]), "dashboard", "--port", "0"])
        assert created.wait(5)
        server = servers[0]
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                assert response.status == 200
                assert b"<html" in response.read().lower()
        finally:
            server.shutdown()
        assert future.result(timeout=5) == 0
    assert server.socket.fileno() == -1
    assert "Read-only dashboard:" in capsys.readouterr().out
