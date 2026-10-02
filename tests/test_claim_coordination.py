import asyncio
import json
from dataclasses import asdict
from hashlib import sha256

import pytest
import yaml

from backlog_harness.adapters.registry import AdapterRegistry
from backlog_harness.application import Application
from backlog_harness.contracts import digest, load_config
from backlog_harness.evidence import EvidenceStore, atomic_json
from backlog_harness.native_evidence import coordination_instructions
from backlog_harness.provider import TransitionBlocked


def configured_app(config_file, provider, *, selected, exempt=False):
    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    config.write_text(yaml.safe_dump(data))
    project = provider.repository / "PROJECT.yaml"
    project_data = yaml.safe_load(project.read_text())
    project_data["resource_coordination"] = {"selected": selected}
    project.write_text(yaml.safe_dump(project_data))
    app = Application(config)
    item = provider.item("item-one")
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": "PROJECT.yaml",
                "sha256": sha256(project.read_bytes()).hexdigest(),
                "excerpt": "execution_mode: SOLO",
                "supports": ["mode", "admission"],
            }
        ],
    }
    if exempt:
        authority = provider.repository / "coordination.md"
        authority.write_text("The active recovery epoch prohibits every claim operation.\n")
        policy.update(
            claims_required=False,
            claim_exemption="Active recovery epoch prohibits every claim operation",
        )
        policy["evidence"].append(
            {
                "path": "coordination.md",
                "sha256": sha256(authority.read_bytes()).hexdigest(),
                "excerpt": authority.read_text().strip(),
                "supports": ["coordination"],
            }
        )
    atomic_json(
        app.provider.cache_path,
        {
            "items": [asdict(item)],
            "dependencies": {item.item_id: []},
            "policy": policy,
            "source_revision": app.provider.source_revision(),
            "source_manifest": app.provider.source_manifest(),
        },
    )
    return app, policy


@pytest.mark.parametrize(
    ("selected", "exempt", "required"),
    [("resource-claim", False, True), ("resource-claim", True, False), ("none", False, False)],
)
def test_coordination_context_preserves_current_policy(config_file, provider, selected, exempt, required):
    app, policy = configured_app(config_file, provider, selected=selected, exempt=exempt)
    context = app.claim_coordination_context(load_config(app.config_path))
    assert context["resource_coordination"] == selected
    assert context["claims_required"] is required
    assert context["policy_digest"] == digest(policy)
    assert context["evidence"] == policy["evidence"]
    assert ("claim_exemption" in context) is exempt
    instructions = coordination_instructions(context)
    assert ("Claims are required" in instructions) is required
    assert ("Claims are prohibited" in instructions) is (not required)


def test_new_invocation_binds_context_to_intent_and_prompt(config_file, provider, monkeypatch):
    app, _ = configured_app(config_file, provider, selected="resource-claim", exempt=True)
    requests = []

    class Inspected(Exception):
        pass

    class Adapter:
        def validate_profile(self, request):
            requests.append(request)
            raise Inspected

    monkeypatch.setattr(AdapterRegistry, "resolve", lambda *_: Adapter())
    with pytest.raises(Inspected):
        asyncio.run(
            app.invoke(
                "item-one",
                "produce-review",
                "orchestrator",
                "Produce and arrange review.",
                read_only=False,
                coordination=True,
            )
        )
    request = requests[0]
    intent = json.loads((request.evidence_path / "intent.json").read_text())
    context = json.loads((request.evidence_path / "coordination-context.json").read_text())
    assert intent["coordination_digest"] == digest(context)
    assert context["config_digest"] == intent["config_digest"]
    assert "Claims are prohibited" in request.prompt
    assert json.dumps(context, sort_keys=True, separators=(",", ":")) in request.prompt
    assert not (request.evidence_path / "requested.json").exists()


@pytest.mark.parametrize("drift", ["config", "source"])
def test_coordination_drift_blocks_before_dispatch(
    config_file, provider, monkeypatch, drift
):
    app, _ = configured_app(config_file, provider, selected="resource-claim", exempt=True)
    started = []

    class Adapter:
        def validate_profile(self, request):
            if drift == "config":
                data = yaml.safe_load(app.config_path.read_text())
                data["poll_seconds"] = 99
                app.config_path.write_text(yaml.safe_dump(data))
            else:
                authority = provider.repository / "coordination.md"
                authority.write_text("The recovery epoch ended.\n")
            return {"production_ready": True}

        async def start_session(self, request):
            started.append(request)

    monkeypatch.setattr(AdapterRegistry, "resolve", lambda *_: Adapter())
    with pytest.raises(TransitionBlocked):
        asyncio.run(
            app.invoke(
                "item-one",
                "produce-review",
                "orchestrator",
                "Produce and arrange review.",
                read_only=False,
                coordination=True,
            )
        )
    (intent,) = app.root.glob("runs/*/operations/*/invocations/*/intent.json")
    assert not (intent.parent / "requested.json").exists()
    assert started == []


def test_prechange_requested_invocation_keeps_original_prompt_hash(
    config_file, provider
):
    app, _ = configured_app(config_file, provider, selected="resource-claim", exempt=True)
    prompt = (
        "Continue recovery. Follow current claim-free crisis authority; do not invoke claims. "
        "Preserve historical evidence."
    )
    stage = "produce-review"
    snapshot = load_config(app.config_path)
    store = EvidenceStore(app.root, "item:item-one")
    path = store.begin(
        "item-one:" + stage,
        "legacy",
        snapshot,
        snapshot.binding("orchestrator"),
        action=stage,
        item_id="item-one",
        request_digest=digest(prompt),
    )
    EvidenceStore.requested(path)
    atomic_json(
        app._stage_path("item-one", stage),
        {"request_digest": digest(prompt), "outcome": "unresolved"},
    )
    with pytest.raises(TransitionBlocked, match="Previous invocation is unresolved"):
        asyncio.run(
            app.invoke(
                "item-one",
                stage,
                "orchestrator",
                prompt,
                read_only=False,
                coordination=True,
            )
        )


def test_unsubmitted_prechange_intent_cannot_launch_without_context(config_file, provider):
    app, _ = configured_app(config_file, provider, selected="resource-claim", exempt=True)
    snapshot = load_config(app.config_path)
    EvidenceStore(app.root, "item:item-one").begin(
        "item-one:produce-review",
        "unsubmitted",
        snapshot,
        snapshot.binding("orchestrator"),
        action="produce-review",
        item_id="item-one",
        request_digest=digest("prompt"),
    )
    with pytest.raises(TransitionBlocked, match="Unsubmitted invocation lacks"):
        asyncio.run(
            app.invoke(
                "item-one",
                "produce-review",
                "orchestrator",
                "prompt",
                read_only=False,
                coordination=True,
            )
        )


def test_invocation_context_reader_rejects_tampering(config_file, provider):
    app, _ = configured_app(config_file, provider, selected="resource-claim", exempt=True)
    snapshot = load_config(app.config_path)
    context = app.claim_coordination_context(snapshot)
    store = EvidenceStore(app.root, "item:item-one")
    path = store.begin(
        "item-one:produce-review",
        "produced",
        snapshot,
        snapshot.binding("orchestrator"),
        action="produce-review",
        item_id="item-one",
        request_digest="request",
        coordination_digest=digest(context),
    )
    atomic_json(path / "coordination-context.json", context)
    EvidenceStore.requested(path)
    result = {
        "evidence_path": str(path),
        "invocation_id": "produced",
        "request_digest": "request",
    }
    assert app.invocation_coordination_context(result) == context
    context["claims_required"] = True
    atomic_json(path / "coordination-context.json", context)
    with pytest.raises(TransitionBlocked, match="differs from its intent"):
        app.invocation_coordination_context(result)
