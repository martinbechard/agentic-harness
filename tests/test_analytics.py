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
