"""Trace gaps preserve returned work while the independent usage guard fences spending."""

import asyncio
from dataclasses import asdict
from uuid import uuid4

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import digest
from backlog_harness.evidence import JsonlWriter, atomic_json
from backlog_harness.provider import TransitionBlocked
from backlog_harness.telemetry import Sink


@pytest.mark.parametrize("gap", ["empty", "rejected-export"])
def test_returned_result_reuses_without_generation_but_unknown_usage_holds(
    config_file, provider, monkeypatch, gap
):
    path, config = config_file
    config["repository"] = str(provider.repository)
    config["operational_root"] = str(provider.evidence_root)
    path.write_text(yaml.safe_dump(config))
    app = Application(path)
    item_id = "item-one"
    telemetry = app.root / "telemetry/runs/run/invocation/spans.jsonl"
    if gap == "rejected-export":
        JsonlWriter(telemetry).append(
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "a" * 32,
                                        "spanId": "b" * 16,
                                        "attributes": [
                                            {
                                                "key": "gen_ai.usage.output_tokens",
                                                "value": {"intValue": 1},
                                            }
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        )
    sink = Sink(telemetry, {})
    result = {
        "request_digest": digest("retained prompt"),
        "outcome": "returned",
        "invocation_id": "retained",
        "role": "orchestrator",
        "binding": asdict(app.config.binding("orchestrator")),
        "text": '{"candidate":"retained"}',
        "session": {"native_session_id": str(uuid4())},
        "events": [
            {
                "at": "2026-10-02T12:00:00+00:00",
                "type": "turn.completed",
                "usage": {"output_tokens": 1},
            }
        ],
        "telemetry_path": str(telemetry),
        "telemetry": {
            "span_count": len(sink.seen),
            "rejected_exports": int(gap == "rejected-export"),
            "evidence_sha256": sink.evidence_digest(),
        },
    }
    saved = app._stage_path(item_id, "produce")
    atomic_json(saved, result)
    before = saved.read_bytes()
    # Native-child discovery is independently tested; this case isolates the trace gap.
    monkeypatch.setattr("backlog_harness.native_evidence.child_usage", lambda *args: (0, []))
    for _ in range(2):
        assert (
            asyncio.run(app.invoke(item_id, "produce", "orchestrator", "retained prompt")) == result
        )
    assert saved.read_bytes() == before
    assert not list((app.root / "runs").rglob("intent.json"))
    view = app.usage_view(item_id)
    assert view["status"] == "unknown"
    assert view["may_generate"] is False
    with pytest.raises(TransitionBlocked, match="held pending usage review"):
        app.guard(item_id)

    # Relaxing completeness never permits forged receipt contents.
    result["telemetry"]["evidence_sha256"] = "forged"
    with pytest.raises(TransitionBlocked, match="not bound"):
        app.validate_invocation_result(result)
