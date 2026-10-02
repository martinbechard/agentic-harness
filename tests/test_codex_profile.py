"""Launch readiness tests command support without requiring an exact CLI patch."""

from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from backlog_harness.adapters.codex.adapter import CodexAdapter


@pytest.mark.parametrize(
    "version,failed,missing,ready",
    [
        ("codex-cli 0.159.2", None, None, True),
        ("codex-cli 0.159.3", None, None, True),
        ("other-cli 0.159.2", None, None, False),
        ("codex-cli 0.159.2", "--version", None, False),
        ("codex-cli 0.159.2", "login status", None, False),
        ("codex-cli 0.159.2", "exec --help", None, False),
        ("codex-cli 0.159.2", "exec resume --help", None, False),
        ("codex-cli 0.159.2", None, "--json", False),
        ("codex-cli 0.159.2", None, "--model", False),
        ("codex-cli 0.159.2", None, "--config", False),
    ],
)
def test_profile_checks_interfaces_and_authentication(monkeypatch, version, failed, missing, ready):
    calls = []

    def run(argv, **kwargs):
        command = " ".join(argv[1:])
        calls.append(command)
        assert kwargs["env"]["CODEX_HOME"] == "/selected/auth"
        assert kwargs["timeout"] == 10
        output = version if command == "--version" else "--json --model --config"
        if missing and command == "exec resume --help":
            output = output.replace(missing, "")
        return CompletedProcess(argv, int(command == failed), output.encode(), b"")

    monkeypatch.setattr("backlog_harness.adapters.codex.adapter.subprocess.run", run)
    request = SimpleNamespace(
        binding=SimpleNamespace(
            auth_context="/selected/auth", executable="codex", relevant_digest="binding"
        )
    )
    result = CodexAdapter().validate_profile(request)
    assert result["production_ready"] is ready
    assert result["cli_version"] == version
    assert calls == ["--version", "login status", "exec --help", "exec resume --help"]
