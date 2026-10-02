"""Unknown legacy provenance refreshes once before retained integration delivery."""

import json
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, command, install_wheel


@pytest.fixture(scope="session")
def retained_cache_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def test_legacy_refresh_then_retained_reconciliation_delivery_reuses_observation(
    retained_cache_python, tmp_path
):
    harness = InstalledHarness.create(
        tmp_path,
        retained_cache_python,
        scenario="legacy",
        dispatcher=Path(__file__).parent / "fixtures/integration_agent.py",
    )

    def git(*args):
        result = command(["git", "-C", harness.repo, *args])
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def run(*args):
        result = harness.run(*args)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def observations():
        rows = [
            json.loads(line) for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
        ]
        return [row for row in rows if "Observe the authoritative file provider" in row["prompt"]]

    (harness.repo / "answer.txt").write_text("\n".join("base " + str(i) for i in range(30)) + "\n")
    git("add", "answer.txt")
    git("commit", "-m", "Shared source base")
    result = command(["git", "-C", harness.candidate, "pull", "--ff-only"])
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load(harness.config_path.read_text())
    config["workflow"]["checks"] = [
        [
            str(retained_cache_python),
            "-c",
            "from pathlib import Path; assert Path('answer.txt').read_text().splitlines()[0] == 'done'",
        ]
    ]
    harness.config_path.write_text(yaml.safe_dump(config))
    first = harness.run("run-item", "item-one")
    assert first.returncode == 2 and "Primary source paths advanced" in first.stderr
    baseline = len(observations())
    cache_path = harness.repo / ".agent-ops/backlog-harness/provider-observation.json"
    cache = json.loads(cache_path.read_text())
    # Model the observed missing provenance, not an invented equivalent old binding.
    cache["observer_digest"] = "unrecoverable-historical-binding-fingerprint"
    cache.pop("capability_digest", None)
    cache.pop("observer_binding_digest", None)
    cache_path.write_text(json.dumps(cache))
    run("refresh-provider")
    assert len(observations()) == baseline + 1
    refreshed = json.loads(cache_path.read_text())
    assert refreshed["capability_digest"] and refreshed["observer_binding_digest"]
    assert refreshed["source_manifest"] == cache["source_manifest"]

    (harness.repo / "unrelated.txt").write_text("source-only advance\n")
    git("add", "unrelated.txt")
    git("commit", "-m", "Source only advancement")
    config = yaml.safe_load(harness.config_path.read_text())
    config["profiles"]["control"].update(model="next-model", effort="high")
    harness.config_path.write_text(yaml.safe_dump(config))
    run("refresh-provider")
    assert len(observations()) == baseline + 1

    candidate = command(["git", "-C", harness.candidate, "rev-parse", "HEAD"]).stdout.strip()
    instruction = tmp_path / "integration.json"
    instruction.write_text(
        json.dumps(
            {
                "item_id": "item-one",
                "original_candidate": candidate,
                "primary": git("rev-parse", "HEAD"),
                "source_reference": "fixture-task-authority",
                "authorization": "Complete this item including integration repair",
                "prospective_estimate": {"remaining_high": 100},
            }
        )
    )
    run("reconcile-integration", "item-one", "--instruction", str(instruction))
    assert len(observations()) == baseline + 1
    run("run-item", "item-one")
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    assert len(observations()) == baseline + 1
    run("run-item", "item-one")
    assert len(observations()) == baseline + 1


@pytest.mark.parametrize("change", ["missing-receipt", "tool-configuration"])
def test_capability_loss_replays_evidence_and_tool_change_refreshes(
    retained_cache_python, tmp_path, change
):
    harness = InstalledHarness.create(tmp_path, retained_cache_python)
    first = harness.run("refresh-provider")
    assert first.returncode == 0, first.stderr
    cache_path = harness.repo / ".agent-ops/backlog-harness/provider-observation.json"
    if change == "missing-receipt":
        cached = json.loads(cache_path.read_text())
        cached.pop("capability_digest")
        cache_path.write_text(json.dumps(cached))
    else:
        config = yaml.safe_load(harness.config_path.read_text())
        config["agent_clis"]["primary"]["adapter_options"]["load_user_config"] = True
        harness.config_path.write_text(yaml.safe_dump(config))
        (harness.native_home / "config.toml").write_text(
            '[mcp_servers.claim_helper]\ncommand = "changed-helper"\n'
        )
    refreshed = harness.run("refresh-provider")
    assert refreshed.returncode == 0, refreshed.stderr
    calls_path = tmp_path / "agent-calls.jsonl"
    expected_calls = 1 if change == "missing-receipt" else 2
    assert len(calls_path.read_text().splitlines()) == expected_calls
    assert json.loads(cache_path.read_text())["capability_digest"]
    repeated = harness.run("refresh-provider")
    assert repeated.returncode == 0, repeated.stderr
    assert len(calls_path.read_text().splitlines()) == expected_calls
