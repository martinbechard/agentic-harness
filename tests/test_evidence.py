import json

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
    store.session(path, {"session_id": "native-session"})
    assert json.loads((path / "session.json").read_text())["session_id"] == "native-session"
    with pytest.raises(FileExistsError):
        store.session(path, {"session_id": "replacement"})
    assert store.reconcile(path)["outcome"] == "unresolved"
    with pytest.raises(FileExistsError):
        store.requested(path)
    with pytest.raises(FileExistsError):
        store.begin("op", "inv", snapshot, snapshot.binding("orchestrator"), action="start")
    store.outcome(path, "returned")
    assert store.reconcile(path)["outcome"] == "returned"


def test_intent_optionally_binds_coordination_digest(config_file, tmp_path):
    config, _ = config_file
    snapshot = load_config(config)
    path = EvidenceStore(tmp_path / "ops", "run").begin(
        "op",
        "inv",
        snapshot,
        snapshot.binding("orchestrator"),
        action="start",
        coordination_digest="a" * 64,
    )
    intent = json.loads((path / "intent.json").read_text())
    assert intent["coordination_digest"] == "a" * 64


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


def test_incremental_read_rejects_truncation_and_invalid_complete_records(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlWriter(path).append({"original": True})
    _, offset, _ = read_jsonl(path)
    path.write_bytes(b"\n")
    with pytest.raises(EvidenceError, match="shrank"):
        read_jsonl(path, offset)
    for malformed in (b"not json\n", b"\xff\n"):
        path.write_bytes(malformed)
        with pytest.raises(EvidenceError, match="Invalid complete evidence line"):
            read_jsonl(path)


@pytest.mark.parametrize(
    "field,value",
    [("version", 2), ("invocation_id", "other"), ("operation_id", "other"), ("run_id", "other")],
)
def test_reconciliation_rejects_wrong_identity(config_file, tmp_path, field, value):
    snapshot = load_config(config_file[0])
    store = EvidenceStore(tmp_path / "ops", "run")
    path = store.begin("op", "inv", snapshot, snapshot.binding("orchestrator"), action="start")
    record = json.loads((path / "intent.json").read_text())
    record[field] = value
    (path / "intent.json").write_text(json.dumps(record))
    with pytest.raises(EvidenceError, match="identity|version"):
        store.reconcile(path)
    assert not (path / "requested.json").exists()


@pytest.mark.parametrize("content", [None, "invalid", '{"version":1}'])
def test_reconciliation_rejects_unreadable_intent(tmp_path, content):
    path = tmp_path / "invocation"
    path.mkdir()
    if content is not None:
        (path / "intent.json").write_text(content)
    with pytest.raises(EvidenceError, match="Cannot reconcile"):
        EvidenceStore.reconcile(path)


def test_partial_outcome_remains_unresolved(config_file, tmp_path):
    snapshot = load_config(config_file[0])
    store = EvidenceStore(tmp_path / "ops", "run")
    path = store.begin("op", "inv", snapshot, snapshot.binding("orchestrator"), action="start")
    store.requested(path)
    store.outcome(path, "returned")
    with (path / "outcomes.jsonl").open("ab") as stream:
        stream.write(b'{"classification":')
    result = store.reconcile(path)
    assert result["outcome"] == "unresolved"
    assert result["partial"] is True


def test_atomic_replacement_and_failed_serialization_preserve_existing_evidence(tmp_path):
    from backlog_harness.evidence import atomic_json

    path = tmp_path / "receipt.json"
    atomic_json(path, {"old": True}, exclusive=True)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        atomic_json(path, {"invalid": float("nan")})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".pending-*"))
    atomic_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"new": True}


def test_operation_lock_waits_and_releases_after_failure(tmp_path):
    import asyncio

    from backlog_harness.evidence import async_operation_lock, operation_lock

    path = tmp_path / "operation.lock"

    async def scenario():
        entered = asyncio.Event()

        async def contender():
            async with async_operation_lock(path):
                entered.set()
                raise RuntimeError("work failed")

        with operation_lock(path):
            task = asyncio.create_task(contender())
            await asyncio.sleep(0.06)
            assert not entered.is_set()
        with pytest.raises(RuntimeError, match="work failed"):
            await task
        assert entered.is_set()
        async with async_operation_lock(path):
            assert path.exists()
        with operation_lock(path):
            pass

    asyncio.run(scenario())
