"""Invocation-authenticated OTLP/HTTP JSON receiver inside the foreground process."""

from __future__ import annotations

import json
import re
import secrets
import threading
import zlib
from copy import deepcopy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .contracts import digest
from .evidence import EvidenceError, JsonlWriter, component, read_jsonl

MAX_BODY = 8 * 1024 * 1024
RESERVED = {
    "harness.run.id",
    "harness.invocation.id",
    "harness.work_item.id",
    "harness.provider.id",
    "harness.agent.role",
    "harness.agent.adapter",
    "harness.session.id",
}


@dataclass(frozen=True)
class TelemetryDestination:
    invocation_id: str
    endpoint: str
    token: str = field(repr=False)
    path: Path


class InvalidPayload(ValueError):
    pass


class TooLarge(ValueError):
    pass


class StorageFailure(EvidenceError):
    pass


def _list(value, name):
    if not isinstance(value, list):
        raise InvalidPayload(f"{name} must be an array")
    return value


def normalize(payload, correlation):
    """Keep the native OTLP tree; add only trusted application span attributes."""
    if not isinstance(payload, dict) or set(payload) - {"resourceSpans"}:
        raise InvalidPayload("Expected ExportTraceServiceRequest")
    value = deepcopy(payload)
    for resource in _list(value.get("resourceSpans", []), "resourceSpans"):
        if not isinstance(resource, dict):
            raise InvalidPayload("Invalid ResourceSpans")
        for scope in _list(resource.get("scopeSpans", []), "scopeSpans"):
            if not isinstance(scope, dict):
                raise InvalidPayload("Invalid ScopeSpans")
            for span in _list(scope.get("spans", []), "spans"):
                if not isinstance(span, dict):
                    raise InvalidPayload("Invalid Span")
                for name, length in [("traceId", 32), ("spanId", 16)]:
                    identity = span.get(name)
                    if (
                        not isinstance(identity, str)
                        or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", identity)
                        or int(identity, 16) == 0
                    ):
                        raise InvalidPayload(f"Invalid {name}")
                parent = span.get("parentSpanId")
                if parent and (
                    not isinstance(parent, str) or not re.fullmatch("[0-9a-fA-F]{16}", parent)
                ):
                    raise InvalidPayload("Invalid parentSpanId")
                attrs = _list(span.setdefault("attributes", []), "attributes")
                seen = set()
                for attr in attrs:
                    if not isinstance(attr, dict) or not isinstance(attr.get("key"), str):
                        raise InvalidPayload("Invalid attribute")
                    key = attr["key"]
                    if key in RESERVED:
                        if (
                            key in seen
                            or key not in correlation
                            or attr.get("value") != {"stringValue": correlation[key]}
                        ):
                            raise InvalidPayload("Conflicting reserved correlation")
                        seen.add(key)
                attrs.extend(
                    {"key": key, "value": {"stringValue": val}}
                    for key, val in correlation.items()
                    if key not in seen
                )
    return value


def spans(payload):
    for resource in payload.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                yield resource, scope, span


class Sink:
    def __init__(self, path, correlation):
        self.path, self.correlation = path, correlation
        self.writer = JsonlWriter(path)
        self.lock = threading.Lock()
        self.seen = {}
        self.failures = 0
        self.storage_failures = 0
        self.pending_exports = set()
        prior, _, partial = read_jsonl(path)
        if partial:
            raise EvidenceError("Telemetry ends in an incomplete line")
        for payload in prior:
            self._remember(payload)

    @staticmethod
    def entries(payload):
        for resource, scope, span in spans(payload):
            # Metadata conflicts are evidence conflicts too.
            value = {
                "resource": {k: v for k, v in resource.items() if k != "scopeSpans"},
                "scope": {k: v for k, v in scope.items() if k != "spans"},
                "span": span,
            }
            yield (span["traceId"].lower(), span["spanId"].lower()), digest(value)

    def _remember(self, payload):
        for identity, fingerprint in self.entries(payload):
            if identity in self.seen and self.seen[identity] != fingerprint:
                raise InvalidPayload("Conflicting native span identity")
            self.seen[identity] = fingerprint

    def evidence_digest(self):
        return digest(sorted(self.seen.items()))

    def write(self, payload):
        value = normalize(payload, self.correlation)
        export_fingerprint = digest(value)
        with self.lock:
            additions = {}
            for identity, fingerprint in self.entries(value):
                prior = additions.get(identity, self.seen.get(identity))
                if prior is not None and prior != fingerprint:
                    raise InvalidPayload("Conflicting native span identity")
                additions[identity] = fingerprint
            if additions and all(key in self.seen for key in additions):
                self.pending_exports.discard(export_fingerprint)
                return
            # Retain full valid export framing; consumers deduplicate overlapping batches.
            try:
                self.writer.append(value)
            except (OSError, EvidenceError) as exc:
                self.storage_failures += 1
                self.pending_exports.add(export_fingerprint)
                raise StorageFailure("Export persistence failed") from exc
            self.seen.update(additions)
            self.pending_exports.discard(export_fingerprint)


class TelemetryReceiver:
    def __init__(self, root: Path):
        self.root = root
        self.sinks = {}
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                if status == 503:
                    self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                if self.path != "/v1/traces":
                    return self.respond(404)
                token = self.headers.get("x-harness-invocation", "")
                with receiver.lock:
                    sink = receiver.sinks.get(token)
                if sink is None:
                    return self.respond(403)
                try:
                    self.connection.settimeout(5)
                    if self.headers.get("Transfer-Encoding"):
                        raise InvalidPayload("Transfer encoding is unsupported")
                    length = int(self.headers.get("Content-Length", "-1"))
                    if length < 0:
                        raise InvalidPayload("Content-Length required")
                    if length > MAX_BODY:
                        raise TooLarge()
                    if (
                        self.headers.get("Content-Type", "").split(";")[0].strip()
                        != "application/json"
                    ):
                        raise InvalidPayload("JSON required")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise InvalidPayload("Incomplete request")
                    encoding = self.headers.get("Content-Encoding", "identity")
                    if encoding == "gzip":
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        raw = decoder.decompress(raw, MAX_BODY + 1)
                        if len(raw) > MAX_BODY or decoder.unconsumed_tail:
                            raise TooLarge()
                        if not decoder.eof or decoder.unused_data:
                            raise InvalidPayload("Invalid gzip framing")
                    elif encoding != "identity":
                        raise InvalidPayload("Unsupported content encoding")

                    def invalid_constant(_):
                        raise InvalidPayload("Nonfinite JSON value")

                    payload = json.loads(raw, parse_constant=invalid_constant)
                    sink.write(payload)
                except TooLarge:
                    sink.failures += 1
                    return self.respond(413)
                except (ValueError, UnicodeError, TypeError, KeyError, zlib.error):
                    sink.failures += 1
                    return self.respond(400)
                except StorageFailure:
                    return self.respond(503)
                except (OSError, EvidenceError):
                    sink.failures += 1
                    return self.respond(503)
                self.respond(200)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def register(self, *, run_id, invocation_id, role, adapter, item_id=None, provider_id=None):
        correlation = {
            "harness.run.id": run_id,
            "harness.invocation.id": invocation_id,
            "harness.agent.role": role,
            "harness.agent.adapter": adapter,
        }
        if item_id is not None:
            if not provider_id:
                raise ValueError("Item telemetry requires provider identity")
            correlation.update(
                {"harness.work_item.id": item_id, "harness.provider.id": provider_id}
            )
            base = self.root / "telemetry/work-items" / component(provider_id + "\0" + item_id)
        else:
            base = self.root / "telemetry/runs" / component(run_id)
        path = base / component(invocation_id) / "spans.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_urlsafe(32)
        with self.lock:
            if any(s.path == path for s in self.sinks.values()):
                raise ValueError("Invocation already registered")
            self.sinks[token] = Sink(path, correlation)
        return TelemetryDestination(
            invocation_id, f"http://127.0.0.1:{self.server.server_port}/v1/traces", token, path
        )

    def report(self, destination):
        with self.lock:
            sink = self.sinks[destination.token]
        with sink.lock:
            return {
                "span_count": len(sink.seen),
                "evidence_sha256": sink.evidence_digest(),
                "rejected_exports": sink.failures + len(sink.pending_exports),
                "storage_failures": sink.storage_failures,
                "unresolved_retries": sorted(sink.pending_exports),
                "coverage": "observed_unverified" if sink.seen else "unknown",
            }

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
