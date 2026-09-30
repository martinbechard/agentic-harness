"""One read-only capture for terminal and dashboard; traces come from standard OTLP."""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .contracts import digest, utcnow
from .evidence import EvidenceError, EvidenceStore, read_jsonl
from .telemetry import Sink, spans


def trace_snapshot(app):
    """Append complete records once; a replacement, shrink or partial tail is explicit."""
    paths = sorted((app.root / "telemetry").glob("*/*/*/spans.jsonl"))
    for absent in set(app.trace_cache) - set(paths):
        del app.trace_cache[absent]
    seen, rows, uncertainty = {}, [], []
    for path in paths:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        entry = app.trace_cache.get(path)
        if entry is None or entry["identity"] != identity:
            entry = {
                "identity": identity,
                "offset": 0,
                "rows": {},
                "partial": False,
                "signature": None,
            }
            app.trace_cache[path] = entry
        signature = (stat.st_size, stat.st_mtime_ns)
        if signature != entry["signature"]:
            if stat.st_size < entry["offset"]:
                raise EvidenceError("Telemetry shrank; reconcile before publishing another view")
            # Rewrites without append are not incremental observations.
            if (
                signature[0] == entry["offset"]
                and entry["signature"] is not None
                and signature[1] != entry["signature"][1]
            ):
                raise EvidenceError(
                    "Telemetry was rewritten; reconcile before publishing another view"
                )
            payloads, offset, partial = read_jsonl(path, entry["offset"])
            for payload in payloads:
                fingerprints = dict(Sink.entries(payload))
                for _, _, span in spans(payload):
                    key = span["traceId"].lower(), span["spanId"].lower()
                    fingerprint = fingerprints[key]
                    if key in entry["rows"] and entry["rows"][key][0] != fingerprint:
                        raise EvidenceError("Conflicting native span in projection")
                    attrs = {a["key"]: a["value"] for a in span.get("attributes", [])}
                    entry["rows"][key] = (
                        fingerprint,
                        {
                            "trace_id": span["traceId"],
                            "span_id": span["spanId"],
                            "parent_span_id": span.get("parentSpanId"),
                            "name": span.get("name"),
                            "invocation_id": attrs.get("harness.invocation.id", {}).get(
                                "stringValue"
                            ),
                            "item_id": attrs.get("harness.work_item.id", {}).get("stringValue"),
                            "end_time_unix_nano": str(span.get("endTimeUnixNano", "0")),
                        },
                    )
            entry.update(offset=offset, partial=partial, signature=signature)
        if entry["partial"]:
            uncertainty.append("partial telemetry:" + path.parent.name)
        for key, (fingerprint, row) in entry["rows"].items():
            if key in seen:
                if seen[key] != fingerprint:
                    uncertainty.append("conflicting trace span:" + key[1])
                continue
            seen[key] = fingerprint
            rows.append(row)
    rows.sort(key=lambda row: (int(row["end_time_unix_nano"]), row["trace_id"], row["span_id"]))
    return len(rows), [dict(row) for row in rows[-200:]], uncertainty


def snapshot(app):
    with app.projection_lock:
        from .application import Application

        current = Application(app.config_path, control_only=True)
        if current.root != app.root or current.config.repository != app.config.repository:
            raise EvidenceError(
                "Projection storage identity changed; reopen with the current configuration"
            )
        current.trace_cache = app.trace_cache
        return capture(current)


def capture(app):
    items = app.provider.snapshot()
    provider_as_of = utcnow()
    invocations, uncertainty = [], []
    receipts = []
    for intent in sorted((app.root / "runs").glob("*/operations/*/invocations/*/intent.json")):
        value = EvidenceStore.reconcile(intent.parent)
        events, _, partial = read_jsonl(intent.parent / "events.jsonl")
        session_path = intent.parent / "session.json"
        session = json.loads(session_path.read_text()) if session_path.exists() else None
        invocations.append(
            {
                "invocation_id": value["invocation_id"],
                "role": value["binding"]["role"],
                "item_id": value["item_id"],
                "outcome": value["outcome"],
                "observed_at": events[-1]["at"] if events else None,
                "portable_session_id": session["session_id"] if session else None,
                "native_session_id": session["native_session_id"] if session else None,
                "adapter": value["binding"]["adapter"],
                "cli_name": value["binding"]["cli_name"],
                "profile_name": value["binding"]["profile_name"],
                "config_digest": value["config_digest"],
                "recent_event_ids": [e["event_id"] for e in events[-20:] if e.get("event_id")],
            }
        )
        if partial or value["outcome"] in {"requested", "unresolved"}:
            uncertainty.append(value["invocation_id"])
        if value["outcome"] == "returned":
            report = intent.parent / "telemetry-report.json"
            metadata = intent.parent / "telemetry.json"
            if not report.exists() or not metadata.exists():
                uncertainty.append("missing telemetry receipt:" + value["invocation_id"])
            elif json.loads(report.read_text()).get("rejected_exports", 0):
                uncertainty.append("rejected telemetry:" + value["invocation_id"])
            else:
                path = Path(json.loads(metadata.read_text())["path"])
                if not path.resolve().is_relative_to(app.root.resolve()) or not path.is_file():
                    uncertainty.append("missing telemetry file:" + value["invocation_id"])
                else:
                    receipts.append((path, json.loads(report.read_text()), value["invocation_id"]))
    count, traces, trace_uncertainty = trace_snapshot(app)
    for path, report, invocation_id in receipts:
        entry = app.trace_cache.get(path)
        if entry is None:
            raise EvidenceError("Telemetry receipt points outside the projection inventory")
        fingerprint = digest(sorted((key, value[0]) for key, value in entry["rows"].items()))
        if report["span_count"] != len(entry["rows"]) or (
            report.get("evidence_sha256") and report["evidence_sha256"] != fingerprint
        ):
            raise EvidenceError(
                "Telemetry receipt mismatch; reconcile before publishing trace data"
            )
        for row in traces:
            if row["invocation_id"] == invocation_id:
                row["receipt_binding"] = (
                    "content_verified" if report.get("evidence_sha256") else "legacy_count_only"
                )
    uncertainty.extend(trace_uncertainty)
    rows = [
        {
            "item_id": item.item_id,
            "state": item.state,
            "owner": item.owner,
            "revision": item.revision,
            "usage": app.usage_view(item.item_id, observed_item=item),
            "question": app.provider.question(item),
        }
        for item in items
    ]
    after = app.provider.snapshot()
    if {(i.item_id, i.revision) for i in after} != {(i.item_id, i.revision) for i in items}:
        raise EvidenceError("Provider changed during projection; refresh the observation")
    last = max((r["observed_at"] for r in invocations if r["observed_at"]), default=None)
    stale = (
        last is None
        or (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
        > app.config.data["runtime_observation_stale_seconds"]
    )
    run_path, block_path = app.root / "run.json", app.root / "scheduling-blocks.json"
    run = (
        json.loads(run_path.read_text())
        if run_path.exists()
        else {"state": "unknown", "admission_open": None}
    )
    blocks = json.loads(block_path.read_text()) if block_path.exists() else {}
    blockers = [
        {"item_id": item.item_id, **blocks[item.item_id]}
        for item in items
        if item.item_id in blocks
        and blocks[item.item_id].get("premise") == digest([item.revision, app.config.file_digest])
        and item.state != "Completed"
    ]
    value = {
        "version": 1,
        "as_of": utcnow(),
        "read_only": True,
        "provider_snapshot": {
            "identity": digest(sorted((i.item_id, i.revision) for i in items)),
            "as_of": provider_as_of,
        },
        "run_observation": run,
        "capability_blockers": blockers,
        "generation_configuration_error": app.config.data.get("generation_configuration_error"),
        "items": rows,
        "counts": dict(Counter(i.state for i in items)),
        "invocations": invocations,
        "runtime_observed_at": last,
        "runtime_freshness": "stale" if stale else "fresh",
        "trace_span_count": count,
        "telemetry_receipt_binding": "legacy_count_only"
        if any(not report.get("evidence_sha256") for _, report, _ in receipts)
        else "content_verified",
        "traces": traces,
        "uncertainty": uncertainty,
    }
    return {"revision": digest(value), **value}
