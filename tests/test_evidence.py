import pytest

from backlog_harness.contracts import load_config
from backlog_harness.evidence import EvidenceError, EvidenceStore, JsonlWriter, read_jsonl


def test_intent_requested_and_uncertain_are_not_replayed(config_file, tmp_path):
    config, _ = config_file
    snapshot = load_config(config)
    store = EvidenceStore(tmp_path / "ops", "run")
    path = store.begin("op", "inv", snapshot, snapshot.binding("orchestrator"), action="start")
    assert store.reconcile(path)["outcome"] == "not_submitted"
    store.requested(path)
    assert store.reconcile(path)["outcome"] == "unresolved"
    with pytest.raises(FileExistsError):
        store.requested(path)
    with pytest.raises(FileExistsError):
        store.begin("op", "inv", snapshot, snapshot.binding("orchestrator"), action="start")
    store.outcome(path, "returned")
    assert store.reconcile(path)["outcome"] == "returned"


def test_partial_line_is_not_published_or_overwritten(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlWriter(path).append({"a": 1})
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    rows, offset, partial = read_jsonl(path)
    assert rows == [{"a": 1}] and partial
    assert read_jsonl(path, offset) == ([], offset, True)
    with pytest.raises(EvidenceError):
        JsonlWriter(path).append({"next": 1})
