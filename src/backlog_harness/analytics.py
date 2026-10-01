"""Measured usage accounting, keeping missing and conflicting evidence explicit."""

from dataclasses import dataclass

from .contracts import digest


@dataclass(frozen=True)
class UsageEvidence:
    event_id: str
    item_id: str
    scope: str
    generated_tokens: int | None
    form: str = "delta"
    trustworthy: bool = True


class UsageLedger:
    def __init__(self):
        self.events = {}
        self.counters = {}
        self.totals = {}
        self.unknown = set()

    def add(self, event: UsageEvidence):
        fingerprint = digest(vars(event))
        if event.event_id in self.events:
            if self.events[event.event_id] != fingerprint:
                self.unknown.add(event.item_id)
            return
        self.events[event.event_id] = fingerprint
        amount = event.generated_tokens
        if (
            not event.trustworthy
            or type(amount) is not int
            or amount < 0
            or event.form not in {"delta", "cumulative"}
        ):
            self.unknown.add(event.item_id)
            return
        key = event.item_id, event.scope
        if event.form == "cumulative":
            previous = self.counters.get(key, 0)
            if amount < previous:
                self.unknown.add(event.item_id)
                return
            self.counters[key] = amount
            amount -= previous
        self.totals[event.item_id] = self.totals.get(event.item_id, 0) + amount

    def guard(
        self, item_id, original_high, *, multiplier=2.0, reviewed_ceiling=None, complete=False
    ):
        total = self.totals.get(item_id)
        if (
            not complete
            or item_id in self.unknown
            or total is None
            or type(original_high) is not int
            or original_high <= 0
        ):
            return {"status": "unknown", "may_generate": False, "generated_tokens": None}
        ceiling = original_high * multiplier if reviewed_ceiling is None else reviewed_ceiling
        if type(ceiling) not in (int, float) or ceiling < original_high * multiplier:
            raise ValueError("Reviewed ceiling cannot reduce the original baseline")
        crossed = total >= ceiling
        return {
            "status": "crossed" if crossed else "below",
            "may_generate": not crossed,
            "generated_tokens": total,
            "original_high": original_high,
            "ceiling": ceiling,
            "overshoot": max(0, total - ceiling),
        }


def invocation_usage(result, previous_session_output=0, *, child_outputs=0):
    """Reconcile request-level native span deltas against session accounting.

    Codex 0.159.2 turn.completed output_tokens is cumulative across resume.
    handle_responses gen_ai.usage.output_tokens is per response; native child
    responses share the invocation exporter. Never add both measurements.
    """
    counters = [e["usage"].get("output_tokens") for e in result["events"] if e.get("usage")]
    if (
        not counters
        or type(counters[-1]) is not int
        or result["telemetry"]["rejected_exports"]
        or counters[-1] < previous_session_output
    ):
        return None
    total = observed_invocation_usage(result)
    expected = counters[-1] - previous_session_output + child_outputs
    return total if total is not None and total == expected else None


def observed_invocation_usage(result):
    """Deduplicated measured output; unmatched native counters do not make it an exact total."""
    from pathlib import Path

    from .evidence import read_jsonl
    from .telemetry import spans

    rows, _, partial = read_jsonl(Path(result["telemetry_path"]))
    if partial or result["telemetry"]["rejected_exports"]:
        return None
    seen, total, found = {}, 0, False
    for payload in rows:
        for _, _, span in spans(payload):
            identity = span["traceId"], span["spanId"]
            fingerprint = digest(span)
            if identity in seen:
                if seen[identity] != fingerprint:
                    return None
                continue
            seen[identity] = fingerprint
            attrs = {a["key"]: a["value"] for a in span.get("attributes", [])}
            if "gen_ai.usage.output_tokens" in attrs:
                value = attrs["gen_ai.usage.output_tokens"].get("intValue")
                if not isinstance(value, (int, str)) or not str(value).isdigit():
                    return None
                total += int(value)
                found = True
    return total if found else None
