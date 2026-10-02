from backlog_harness.analytics import UsageEvidence, UsageLedger


def test_unknown_dedup_cumulative_and_crossing():
    ledger = UsageLedger()
    assert ledger.guard("a", 100)["status"] == "unknown"
    ledger.add(UsageEvidence("1", "a", "session", 100, "cumulative"))
    ledger.add(UsageEvidence("1", "a", "session", 100, "cumulative"))
    ledger.add(UsageEvidence("2", "a", "session", 150, "cumulative"))
    ledger.add(UsageEvidence("3", "a", "child", 60))
    gate = ledger.guard("a", 100, complete=True)
    assert gate["status"] == "crossed" and gate["overshoot"] == 10
    assert not gate["may_generate"]
    assert ledger.guard("a", 100, reviewed_ceiling=250, complete=True)["may_generate"]
    ledger.add(UsageEvidence("4", "a", "session", 140, "cumulative"))
    assert ledger.guard("a", 100, reviewed_ceiling=9999, complete=True)["status"] == "unknown"
    ledger.add(UsageEvidence("5", "b", "other", 1))
    assert ledger.guard("b", 100, complete=True)["may_generate"]


def test_conflicting_usage_identity_blocks_generation():
    ledger = UsageLedger()
    ledger.add(UsageEvidence("same", "item", "session", 10))
    ledger.add(UsageEvidence("same", "item", "session", 20))
    assert ledger.guard("item", 100, complete=True)["status"] == "unknown"


def test_invalid_usage_and_ceiling_never_authorize_generation():
    import pytest

    for amount, form, trustworthy in [
        (None, "delta", True),
        (-1, "delta", True),
        (True, "delta", True),
        (1, "other", True),
        (1, "delta", False),
    ]:
        ledger = UsageLedger()
        ledger.add(UsageEvidence("event", "item", "session", amount, form, trustworthy))
        assert not ledger.guard("item", 100, complete=True)["may_generate"]
    ledger = UsageLedger()
    ledger.add(UsageEvidence("event", "item", "session", 1))
    for ceiling in (True, "200", 199):
        with pytest.raises(ValueError, match="cannot reduce"):
            ledger.guard("item", 100, reviewed_ceiling=ceiling, complete=True)


def test_observed_usage_deduplicates_and_rejects_conflicting_or_invalid_values(tmp_path):
    import json
    from copy import deepcopy

    from backlog_harness.analytics import invocation_usage, observed_invocation_usage

    path = tmp_path / "spans.jsonl"
    span = {
        "traceId": "a" * 32,
        "spanId": "b" * 16,
        "attributes": [{"key": "gen_ai.usage.output_tokens", "value": {"intValue": "12"}}],
    }

    def write(spans):
        path.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}) + "\n")

    result = {
        "telemetry": {"rejected_exports": 0},
        "telemetry_path": str(path),
        "events": [{"usage": {"output_tokens": 12}}],
    }
    write([span, span])
    assert observed_invocation_usage(result) == 12
    assert invocation_usage(result) == 12
    assert invocation_usage(result, previous_session_output=13) is None
    diagnostic = {"traceId": "a" * 32, "spanId": "c" * 16, "attributes": []}
    write([diagnostic, span])
    assert observed_invocation_usage(result) == 12
    write([diagnostic])
    assert observed_invocation_usage(result) is None
    for invalid in (-1, True, {}, "unknown"):
        changed = deepcopy(span)
        changed["attributes"][0]["value"]["intValue"] = invalid
        write([changed])
        assert observed_invocation_usage(result) is None
    changed = deepcopy(span)
    changed["name"] = "conflicting metadata"
    write([span, changed])
    assert observed_invocation_usage(result) is None
    path.write_text('{"partial":')
    assert observed_invocation_usage(result) is None
    write([span])
    result["telemetry"]["rejected_exports"] = 1
    assert observed_invocation_usage(result) is None
    assert invocation_usage(result) is None


def test_missing_recovered_accounting_does_not_authorize_spending(tmp_path):
    from backlog_harness.analytics import invocation_usage, observed_invocation_usage

    path = tmp_path / "empty.jsonl"
    path.write_text("")
    result = {
        "telemetry": {"rejected_exports": None},
        "telemetry_path": str(path),
        "events": [{"usage": {"output_tokens": 1}}],
    }
    assert invocation_usage(result) is None
    assert observed_invocation_usage(result) is None
    result["telemetry"]["rejected_exports"] = 0
    assert observed_invocation_usage(result) is None
    for events in ([], [{"usage": {"output_tokens": "1"}}]):
        result["events"] = events
        assert invocation_usage(result) is None
