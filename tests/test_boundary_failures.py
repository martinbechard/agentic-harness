"""External adapter and Git failures must not masquerade as usable execution evidence."""

from types import SimpleNamespace

import pytest

from backlog_harness.adapters.registry import AdapterRegistry
from backlog_harness.delivery import compatible_primary_paths, preserved_untracked
from backlog_harness.provider import TransitionBlocked


@pytest.mark.parametrize(
    "missing",
    [
        "validate_profile",
        "prepare_telemetry",
        "start_session",
        "resume_session",
        "observe_events",
        "reconcile",
        "request_interrupt",
    ],
)
def test_incomplete_adapter_cannot_be_resolved(missing):
    methods = {
        name: lambda: None
        for name in (
            "validate_profile",
            "prepare_telemetry",
            "start_session",
            "resume_session",
            "observe_events",
            "reconcile",
            "request_interrupt",
        )
    }
    methods[missing] = None
    registry = AdapterRegistry({"broken": lambda: SimpleNamespace(**methods)})
    with pytest.raises(TypeError, match=f"does not implement {missing}"):
        registry.resolve("broken")


def test_untracked_inventory_failure_never_claims_files_preserved(provider, monkeypatch):
    import subprocess

    original = subprocess.run

    def fail_inventory(argv, **kwargs):
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"inventory unavailable")
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_inventory)
    with pytest.raises(TransitionBlocked, match="inventory unavailable"):
        preserved_untracked(provider.repository)


def test_primary_comparison_failure_never_claims_compatibility(provider):
    with pytest.raises(TransitionBlocked, match="bad revision"):
        compatible_primary_paths(
            provider.repository, provider.repository, "HEAD", "missing-ref", "HEAD", ["answer.py"]
        )


def test_unsupported_transition_cannot_advance_with_an_observed_actor():
    from backlog_harness.provider import Item
    from backlog_harness.workflow import validate_transition

    item = Item("one", "one.md", "revision", "Completed", "owner", 100, "")
    with pytest.raises(TransitionBlocked, match="Unsupported transition Completed -> Starting"):
        validate_transition(
            item,
            "Starting",
            {
                "role": "coordinator",
                "item_id": "one",
                "invocation_id": "observed",
                "observed_result": {"requested": True},
            },
        )


def test_module_entrypoint_exposes_cli_without_configuration():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "backlog_harness", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "run-item" in result.stdout
    assert "--config" in result.stdout
