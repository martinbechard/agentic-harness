"""Retiring the pilot must never reinterpret its records as a new execution."""

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import ConfigError
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked


def test_standard_execution_needs_no_engine_binding(config_file):
    app = Application(config_file[0])
    app.require_legacy_execution("one")
    assert not (app.root / "execution-engines").exists()


@pytest.mark.parametrize("engine", ["legacy", "langgraph", "unknown"])
def test_retained_engine_binding_is_never_reinterpreted(config_file, engine):
    app = Application(config_file[0])
    path = app.root / "execution-engines" / (component("one") + ".json")
    atomic_json(
        path, {"item_id": "one", "repository": str(app.config.repository), "engine": engine}
    )
    before = path.read_bytes()
    if engine == "legacy":
        app.require_legacy_execution("one")
    else:
        with pytest.raises(TransitionBlocked, match="not restarted"):
            app.require_legacy_execution("one")
    assert path.read_bytes() == before
    assert not (app.root / "runs").exists()


def test_orphaned_pilot_checkpoint_prevents_new_execution(config_file):
    app = Application(config_file[0])
    (app.root / "item-graphs" / component("one")).mkdir(parents=True)
    with pytest.raises(TransitionBlocked, match="checkpoints"):
        app.require_legacy_execution("one")


def test_retired_engine_configuration_fails_before_dispatch(config_file):
    path, data = config_file
    data["workflow"]["items"] = {"one": {"engine": "langgraph", "allowed_paths": ["answer.py"]}}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="optional workflow engine has been removed"):
        Application(path)
