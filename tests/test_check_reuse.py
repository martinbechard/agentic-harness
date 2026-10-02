"""Successful candidate checks are reused; changed inputs and failures are rechecked."""

import json
import sys

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.provider import TransitionBlocked, git


@pytest.fixture
def checks_app(config_file, provider, tmp_path):
    path, data = config_file
    count = tmp_path / "check-count"
    failure = tmp_path / "fail"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('verified'); raise SystemExit(int(Path({str(failure)!r}).exists()))",
    ]
    data["workflow"]["checks"] = [command]
    path.write_text(yaml.safe_dump(data))
    return Application(path), provider.repository, count, failure, data


def run(fixture):
    app, repo, _, _, _ = fixture
    return app.checks(repo, "one", git(repo, "rev-parse", "HEAD"), "source-checks")


def test_successful_checks_are_reused_without_changing_receipts(checks_app):
    app, _, count, _, _ = checks_app
    first = run(checks_app)
    before = {p: p.read_bytes() for p in app.root.rglob("*.json")}
    assert run(checks_app) == first
    assert count.read_text() == "x"
    assert before == {p: p.read_bytes() for p in app.root.rglob("*.json")}


@pytest.mark.parametrize(
    "change", ["candidate", "command", "dirty-source", "missing-intent", "failed"]
)
def test_changed_inputs_or_failed_checks_require_execution(checks_app, change):
    app, repo, count, failure, data = checks_app
    if change == "failed":
        failure.touch()
    if change == "failed":
        with pytest.raises(TransitionBlocked, match="Required checks failed"):
            run(checks_app)
        failure.unlink()
    else:
        run(checks_app)
    if change == "candidate":
        git(repo, "commit", "--allow-empty", "-m", "New candidate")
    elif change == "command":
        data["workflow"]["checks"][0][-1] += '; print("new check")'
        app.config_path.write_text(yaml.safe_dump(data))
        checks_app = (Application(app.config_path), repo, count, failure, data)
    elif change == "dirty-source":
        with (repo / "PROJECT.yaml").open("a") as f:
            f.write("\n# Changed input\n")
    elif change == "missing-intent":
        app._stage_path("one", "source-checks-execution").unlink()
    run(checks_app)
    assert count.read_text() == "xx"


def test_corrupt_success_receipt_is_not_reused_or_silently_replaced(checks_app):
    app, _, count, _, _ = checks_app
    run(checks_app)
    path = app._stage_path("one", "source-checks")
    rows = json.loads(path.read_text())
    rows[0]["output"] = "forged result"
    path.write_text(json.dumps(rows))
    with pytest.raises(TransitionBlocked, match="evidence differs"):
        run(checks_app)
    assert count.read_text() == "x"


def test_checks_run_on_dirty_source_are_not_reused_after_cleanup(checks_app):
    _, repo, count, _, _ = checks_app
    source = repo / "PROJECT.yaml"
    original = source.read_bytes()
    source.write_bytes(original + b"\n# temporary change\n")
    run(checks_app)
    source.write_bytes(original)
    run(checks_app)
    assert count.read_text() == "xx"


def test_untracked_source_prevents_reuse(checks_app):
    _, repo, count, _, _ = checks_app
    run(checks_app)
    (repo / "new_source.py").write_text("raise RuntimeError('new input')\n")
    run(checks_app)
    assert count.read_text() == "xx"
