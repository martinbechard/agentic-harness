"""External effects journal using the harness EvidenceStore, not workflow position."""

import json
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from configuration import reload_config
from native import NativeClient

from backlog_harness.contracts import digest
from backlog_harness.evidence import EvidenceStore, atomic_json, component, operation_lock


class Held(RuntimeError):
    """An uncertain external effect requires evidence, never an automatic replay."""


def read(path):
    return json.loads(Path(path).read_text())


class Invocations:
    def __init__(self, root, execution_id, config_path, factory=NativeClient):
        self.root, self.execution_id = Path(root), execution_id
        self.config_path, self.factory = Path(config_path), factory
        self.store = EvidenceStore(self.root, execution_id)

    def path(self, operation_id):
        invocation_id = str(uuid5(NAMESPACE_URL, self.execution_id + ":" + operation_id))
        return (
            self.store.run
            / "operations"
            / component(operation_id)
            / "invocations"
            / component(invocation_id)
        )

    def hold(self, path, reason):
        record = read(path / "intent.json")
        hold = {
            "operation_id": record["operation_id"],
            "invocation_id": record["invocation_id"],
            "request_digest": record["request_digest"],
            "reason": reason,
            "recovery": "Inspect original native session/invocation; no new submission authorized",
        }
        if not (path / "hold.json").exists():
            atomic_json(path / "hold.json", hold, exclusive=True)
        raise Held(reason + ": " + str(path))

    def result(self, reference):
        path = Path(reference["path"]).resolve()
        if not path.is_relative_to(self.store.run.resolve()):
            raise ValueError("Invocation reference outside execution evidence")
        value = read(path / "result.json")
        intent = EvidenceStore.reconcile(path)
        session = read(path / "session.json")
        if (
            digest(value) != reference["digest"]
            or value["invocation_id"] != intent["invocation_id"]
            or value["request_digest"] != intent["request_digest"]
            or value["session_id"] != session["session_id"]
            or value["role"] != intent["binding"]["role"]
            or digest(read(path / "request.json")) != intent["request_digest"]
        ):
            raise ValueError("Invocation result binding changed")
        return value

    def reconcile_observation(self, path):
        """Recover only a retained transport-completed observation, never infer completion."""
        intent = EvidenceStore.reconcile(path)
        session = read(path / "session.json")
        observed = read(path / "observed-response.json")
        if (
            observed.get("invocation_id") != intent["invocation_id"]
            or observed.get("request_digest") != intent["request_digest"]
            or observed.get("session_id") != session["session_id"]
            or observed.get("role") != intent["binding"]["role"]
            or observed.get("response", {}).get("response", {}).get("stopReason") != "end_turn"
        ):
            self.hold(path, "Transport observation does not prove this invocation completed")
        atomic_json(path / "result.json", observed, exclusive=True)
        EvidenceStore.outcome(path, "returned", reconciled_observation_digest=digest(observed))

    async def invoke(
        self, operation_id, role, prompt, *, session=None, producer_id=None, candidate=None
    ):
        snapshot = reload_config(self.config_path)
        binding = snapshot.binding(role)
        path = self.path(operation_id)
        request = {
            "role": role,
            "prompt": prompt,
            "session": session,
            "producer_id": producer_id,
            "candidate": candidate,
        }
        request_digest = digest(request)
        with operation_lock(
            self.root / "locks" / (component(self.execution_id + operation_id) + ".lock")
        ):
            if (path / "intent.json").exists():
                intent = EvidenceStore.reconcile(path)
                if intent["request_digest"] != request_digest:
                    raise ValueError("Stable operation was reused with a different request")
                if (path / "observed-response.json").exists() and not (
                    path / "result.json"
                ).exists():
                    self.reconcile_observation(path)
                if (path / "result.json").exists():
                    result = read(path / "result.json")
                    reference = {"path": str(path), "digest": digest(result)}
                    self.result(reference)
                    return reference
                if (path / "requested.json").exists():
                    self.hold(path, "Prompt may have reached Codex; completed result unavailable")
                if (path / "session-requested.json").exists() and not (
                    path / "session.json"
                ).exists():
                    self.hold(path, "Session creation/load outcome unknown")
                # A never-submitted retry cannot silently change its frozen invocation settings.
                if intent["config_digest"] != snapshot.file_digest or intent["binding"] != asdict(
                    binding
                ):
                    self.hold(
                        path,
                        "Never-submitted invocation configuration or binding changed; explicit reconciliation required",
                    )
            else:
                self.store.begin(
                    operation_id,
                    str(uuid5(NAMESPACE_URL, self.execution_id + ":" + operation_id)),
                    snapshot,
                    binding,
                    action="acp-prompt",
                    request_digest=request_digest,
                )
                atomic_json(path / "request.json", request, exclusive=True)
            if session and session["permission_digest"] != binding.permission_digest:
                self.hold(path, "Session permissions changed; original identity cannot be replaced")
            if (path / "session.json").exists() and read(path / "session.json")[
                "binding"
            ] != asdict(binding):
                self.hold(path, "Retained session binding changed before retry")
            async with self.factory(snapshot, role) as client:
                if (path / "session.json").exists():
                    saved = read(path / "session.json")
                    await client.load_session(saved["session_id"])
                else:
                    method = "session/load" if session else "session/new"
                    atomic_json(
                        path / "session-requested.json",
                        {
                            "method": method,
                            "session_id": session["session_id"] if session else None,
                            "invocation_id": read(path / "intent.json")["invocation_id"],
                        },
                        exclusive=True,
                    )
                    if session:
                        await client.load_session(session["session_id"])
                        identity = session["session_id"]
                    else:
                        identity = await client.new_session()
                    EvidenceStore.session(
                        path,
                        {
                            "session_id": identity,
                            "method": method,
                            "permission_digest": binding.permission_digest,
                            "native_exchange": client.session_exchange,
                            "binding": asdict(binding),
                            "creation": session["creation"] if session else str(path),
                        },
                    )
                    saved = read(path / "session.json")
                if role == "reviewer" and saved["session_id"] == producer_id:
                    self.hold(path, "Reviewer session is the producer session")
                EvidenceStore.requested(path)
                response = await client.prompt(saved["session_id"], prompt)
                result = {
                    "invocation_id": read(path / "intent.json")["invocation_id"],
                    "request_digest": request_digest,
                    "role": role,
                    "session_id": saved["session_id"],
                    "response": response,
                }
                # Retain transport completion before promoting the graph-facing result.
                atomic_json(path / "observed-response.json", result, exclusive=True)
                self.reconcile_observation(path)
            return {"path": str(path), "digest": digest(result)}

    def session(self, reference):
        self.result(reference)
        value = read(Path(reference["path"]) / "session.json")
        return {key: value[key] for key in ("session_id", "permission_digest", "creation")}

    def verify_review(self, reference, candidate, producer_id):
        result = self.result(reference)
        session = read(Path(reference["path"]) / "session.json")
        creation = Path(session["creation"]).resolve()
        if not creation.is_relative_to(self.store.run.resolve()):
            raise ValueError("Review creation belongs to another execution")
        created = read(creation / "session.json")
        requested = read(creation / "session-requested.json")
        intent = EvidenceStore.reconcile(creation)
        assignment = read(Path(reference["path"]) / "request.json")
        if (
            created["method"] != "session/new"
            or requested["method"] != "session/new"
            or requested["invocation_id"] != intent["invocation_id"]
            or intent["binding"]["role"] != "reviewer"
            or created["session_id"] != result["session_id"]
            or created["native_exchange"]["method"] != "session/new"
            or created["native_exchange"]["response"]["sessionId"] != created["session_id"]
            or assignment["candidate"] != candidate
            or result["session_id"] == producer_id
            or assignment["producer_id"] != producer_id
            or result["role"] != "reviewer"
        ):
            raise ValueError("Independent review creation evidence is invalid")
        verdict = parse_json(result["response"]["text"])
        if (
            verdict.get("verdict") != "ACCEPT"
            or verdict.get("candidate") != candidate
            or not isinstance(verdict.get("evidence"), str)
            or not verdict["evidence"].strip()
        ):
            raise ValueError("Review did not accept exact candidate with supporting evidence")
        return verdict


def parse_json(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("Structured agent object required")
    return value


def reconcile_provider_receipt(app, requested_path, observed_path):
    """Reuse the existing committed-effect verifier; absence never authorizes another mutation."""
    requested = read(requested_path)
    if not Path(observed_path).exists():
        raise Held(
            "Provider outcome unknown; reconcile original operation " + requested["stage_operation"]
        )
    return app.verify_provider_receipt(requested, read(observed_path))
