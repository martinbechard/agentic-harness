"""Bounded, nonrepeating native exporter acceptance: one transient 503 and shutdown flush."""

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from backlog_harness.adapters.registry import AdapterRegistry
from backlog_harness.contracts import digest, freeze, load_config, plain, utcnow
from backlog_harness.evidence import (
    EvidenceStore,
    JsonlWriter,
    atomic_json,
    operation_lock,
    read_jsonl,
)
from backlog_harness.runtime import AgentRequest
from backlog_harness.telemetry import TelemetryReceiver, spans


async def main(config, output_root):
    snapshot = load_config(config)
    output_root = output_root.resolve()
    data = plain(snapshot.data)
    data["operational_root"] = str(output_root)
    snapshot = replace(snapshot, data=freeze(data))
    binding = snapshot.binding("coordinator")
    prompt = 'This is a bounded telemetry capability check. Read no files, use no tools, and delegate no work. Return only JSON {"telemetry_probe":"ok"}.'
    run_id, operation = "native-exporter-acceptance", "retry-and-shutdown"
    store = EvidenceStore(output_root, run_id)
    report_path = output_root / "native-exporter-report.json"
    with operation_lock(output_root / "probe.lock"):
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report["binding_digest"] != binding.relevant_digest:
                raise ValueError(
                    "Retained capability binding differs; do not overwrite its evidence"
                )
            return report
        prior = list(store.run.glob("operations/*/invocations/*/intent.json"))
        if prior:
            raise ValueError("A probe intent exists; reconcile it rather than launch another probe")
        invocation = str(uuid4())
        path = store.begin(
            operation,
            invocation,
            snapshot,
            binding,
            action="capability",
            request_digest=digest(prompt),
        )
        attempts = JsonlWriter(output_root / "export-attempts.jsonl")
        with TelemetryReceiver(output_root) as receiver:
            dest = receiver.register(
                run_id=run_id, invocation_id=invocation, role="coordinator", adapter="codex"
            )
            sink = receiver.sinks[dest.token]
            native_append = sink.writer.append
            count = 0

            def controlled_append(value):
                nonlocal count
                count += 1
                fault = count == 1
                attempts.append(
                    {
                        "at": utcnow(),
                        "payload_digest": digest(value),
                        "span_count": sum(1 for _ in spans(value)),
                        "attempt": count,
                        "response": 503 if fault else 200,
                    }
                )
                if fault:
                    raise OSError("Intentional first-export storage failure")
                native_append(value)

            sink.writer.append = controlled_append
            request = AgentRequest(
                operation,
                invocation,
                snapshot,
                binding,
                prompt,
                path,
                dest,
                timeout_seconds=90,
                capability_probe=True,
            )
            adapter = AdapterRegistry().resolve("codex")
            capability = await asyncio.to_thread(adapter.validate_profile, request)
            if not capability["production_ready"]:
                raise ValueError("Codex preflight failed")
            result = await adapter.start_session(request)
            exited_at = utcnow()
            at_exit = receiver.report(dest)
            await asyncio.sleep(2)
            report = receiver.report(dest)
            payloads, _, partial = read_jsonl(dest.path)
            native_spans = [span for payload in payloads for _, _, span in spans(payload)]
            output = 0
            for span in native_spans:
                attrs = {a["key"]: a["value"] for a in span.get("attributes", [])}
                if "gen_ai.usage.output_tokens" in attrs:
                    output += int(attrs["gen_ai.usage.output_tokens"]["intValue"])
            turns = [
                e["usage"]["output_tokens"] for e in result.events if e["type"] == "turn.completed"
            ]
            exports = read_jsonl(output_root / "export-attempts.jsonl")[0]
            retried = any(
                v["response"] == 200 and v["payload_digest"] == exports[0]["payload_digest"]
                for v in exports[1:]
            )
            record = {
                "status": "accepted"
                if result.outcome == "returned"
                and retried
                and not partial
                and report["rejected_exports"] == 0
                and output == (turns[-1] if turns else None)
                and report["span_count"] == at_exit["span_count"]
                else "unverified",
                "cli_version": capability["cli_version"],
                "binding_digest": binding.relevant_digest,
                "invocation_id": invocation,
                "native_session_id": result.session.native_session_id if result.session else None,
                "outcome": result.outcome,
                "native_retry_verified": retried,
                "process_exited_at": exited_at,
                "spans_at_process_exit": at_exit["span_count"],
                "telemetry": report,
                "root_output_tokens": turns[-1] if turns else None,
                "otel_output_tokens": output,
                "span_names": sorted({s["name"] for s in native_spans}),
                "attribute_keys": sorted(
                    {a["key"] for s in native_spans for a in s.get("attributes", [])}
                ),
                "evidence_path": str(path),
                "telemetry_path": str(dest.path),
                "export_attempts_path": str(output_root / "export-attempts.jsonl"),
            }
            atomic_json(path / "telemetry-report.json", report, exclusive=True)
            atomic_json(report_path, record, exclusive=True)
            return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main(args.config, args.output_root)), indent=2))
