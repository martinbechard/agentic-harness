"""Bind integration to retained source authority; never infer missing proof coverage."""

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

from .contracts import digest
from .evidence import component
from .native_evidence import verify_native_review
from .provider import blob, git
from .workflow import require, validate_candidate


def _read(path):
    require(path.is_file() and not path.is_symlink(), "Required integration evidence is missing")
    return json.loads(path.read_text())


def validate_integration_instruction(app, item_id, instruction):
    required = {
        "item_id",
        "original_candidate",
        "primary",
        "source_reference",
        "authorization",
        "prospective_estimate",
    }
    require(
        isinstance(instruction, dict)
        and required <= set(instruction)
        and set(instruction) <= required | {"generated_paths", "generators", "generator_inputs"},
        "Integration instruction fields differ",
    )
    require(instruction["item_id"] == item_id, "Integration instruction names another item")
    for key in required - {"prospective_estimate"}:
        require(
            isinstance(instruction[key], str) and instruction[key].strip(),
            "Empty integration authority",
        )
    estimate = instruction["prospective_estimate"]
    require(
        isinstance(estimate, dict)
        and set(estimate) == {"remaining_high"}
        and type(estimate["remaining_high"]) is int
        and estimate["remaining_high"] > 0,
        "Positive prospective remaining estimate required",
    )
    stage = lambda name: app._stage_path(item_id, name)
    acceptance = _read(stage("accept"))
    app.validate_invocation_result(acceptance)
    accepted = app.result_json(acceptance)
    require(
        acceptance.get("role") == "orchestrator"
        and accepted.get("item_id") == item_id
        and accepted.get("accepted") is True,
        "Original canonical acceptance differs",
    )
    require(
        app.provider.item(item_id).owner == acceptance["session"]["session_id"],
        "Original canonical owner changed",
    )
    assignment = _read(stage("assignment"))
    workflow = app.item_workflow(item_id)
    require(assignment.get("workflow") == workflow, "Frozen integration workflow differs")
    recovery = app.recovery_record(item_id)
    base = recovery["packet"]["candidate"]["base"] if recovery else _read(stage("base"))["commit"]
    candidate = instruction["original_candidate"]
    repo = Path(app.candidate_repository(item_id)).resolve()
    matches = {}
    for path in stage("unused").parent.glob("*.json"):
        envelope = _read(path)
        if not isinstance(envelope, dict) or envelope.get("role") != "orchestrator":
            continue
        if envelope.get("session") != acceptance["session"] or not envelope.get("text"):
            continue
        value = app.result_json(envelope)
        if (
            isinstance(value, dict)
            and value.get("item_id") == item_id
            and value.get("candidate") == candidate
            and (
                value.get("request_completion") is True
                or value.get("status") == "awaiting_exact_candidate_approval"
            )
        ):
            matches[digest(envelope)] = envelope
    require(len(matches) == 1, "Unique original canonical candidate result required")
    produced = next(iter(matches.values()))
    app.validate_invocation_result(produced)
    value = app.result_json(produced)
    review = verify_native_review(
        produced["session"]["native_session_id"],
        value.get("reviewer_session"),
        candidate,
        app.native_sessions_root(produced["binding"]),
    )
    checks = _read(stage("source-checks"))
    require(
        [row.get("argv") for row in checks] == [list(argv) for argv in workflow["checks"]],
        "Original check coverage differs from frozen workflow",
    )
    validate_candidate(
        repo,
        candidate,
        base,
        workflow["allowed_paths"],
        produced["session"]["native_session_id"],
        review,
        checks,
        preserved=bool(recovery),
    )
    generated = instruction.get("generated_paths", [])
    generators = instruction.get("generators", [])
    inputs = instruction.get("generator_inputs", {})
    require(isinstance(inputs, dict), "Generator source hashes must be a mapping")
    if generators:
        runtimes = {command[0] for command in workflow["checks"]}
        require(
            inputs
            and all(
                isinstance(command, list)
                and len(command) >= 2
                and command[0] in runtimes
                and command[1] in inputs
                for command in generators
            ),
            "Generators require an existing configured runtime and bound tracked script",
        )
        for name, expected in inputs.items():
            path = Path(name)
            require(
                not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts,
                "Generator source path escapes repository",
            )
            for repository, revision in (
                (repo, candidate),
                (app.config.repository, instruction["primary"]),
            ):
                require(
                    git(repository, "ls-tree", revision, "--", name).startswith(
                        ("100644 ", "100755 ")
                    )
                    and sha256(blob(repository, revision, name)).hexdigest() == expected,
                    "Generator source/config changed or was newly introduced",
                )
    else:
        require(not inputs and not generated, "Generator inputs/outputs require a command")
    require(
        isinstance(generated, list)
        and set(generated) <= set(workflow["allowed_paths"])
        and isinstance(generators, list)
        and all(
            isinstance(argv, list) and argv and all(isinstance(s, str) and s for s in argv)
            for argv in generators
        ),
        "Integration generator scope is invalid",
    )
    primary = instruction["primary"]
    require(
        git(app.config.repository, "rev-parse", primary + "^{commit}") == primary,
        "Exact primary commit required",
    )
    # HEAD may advance through delivery after authorization; the delivery gate owns that check.
    git(app.config.repository, "merge-base", "--is-ancestor", base, primary)
    workspace = repo.parent / ".integration-workspaces" / component(item_id)
    require(workspace.resolve() == workspace, "Integration workspace redirected")
    return {
        "workspace": workspace,
        "candidate_repository": repo,
        "original_candidate": candidate,
        "base": base,
        "primary": primary,
        "allowed_paths": workflow["allowed_paths"],
        "generated_paths": generated,
        "generators": generators,
        "authority": {
            **{key: instruction[key] for key in ("source_reference", "authorization")},
            "basis": "retained canonical task acceptance and exact candidate completion request",
            "item_id": item_id,
            "accepted_assignment_digest": digest(assignment),
            "canonical_result_digest": digest(produced),
            "original_candidate": candidate,
            "primary": primary,
            "allowed_paths": list(workflow["allowed_paths"]),
        },
        "prospective_estimate": estimate,
        "original_evidence": {
            "acceptance": digest(acceptance),
            "assignment": digest(assignment),
            "produced": digest(produced),
            "review": digest(review),
            "checks": digest(checks),
            "recovery": digest(recovery),
        },
    }


def _tree_inputs(repository, candidate):
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", candidate],
        env=env,
        capture_output=True,
        check=True,
    )
    entries = {}
    for entry in filter(None, result.stdout.split(b"\0")):
        identity, name = entry.split(b"\t", 1)
        entries[os.fsdecode(name)] = digest(identity.decode())
    return entries


def verify_integration_proof(app, item_id, record, instruction):
    """Validate exact proof inputs; scoped applicability requires fresh integration review."""
    stage = lambda name: app._stage_path(item_id, name)
    require(
        record["original_candidate"] == instruction["original_candidate"],
        "Integration proof original candidate differs",
    )
    assignment = _read(stage("assignment"))
    recovery = app.recovery_record(item_id)
    if not stage("artifact-proof-request").exists():
        require(
            recovery is None
            and not stage("proof-continuation").exists()
            and not stage("proof-review").exists(),
            "Original proof requirement is ambiguous; fresh verified proof required",
        )
        require(assignment.get("workflow", {}).get("checks"), "Original check contract missing")
        return {
            "disposition": "not-required",
            "basis": "plain configured checks and native source review",
            "assignment_digest": digest(assignment),
            "recovery_digest": digest(None),
            "artifact_proof_request_absent": True,
        }
    from .recovery_flow import validated_auxiliary_proof

    acceptance = _read(stage("accept"))
    request, result, value = validated_auxiliary_proof(app, item_id, acceptance)
    require(
        request["candidate"] == record["original_candidate"], "Original proof candidate differs"
    )
    review = _read(stage("proof-review"))
    verified = verify_native_review(
        acceptance["session"]["native_session_id"],
        review.get("reviewer_task", review.get("reviewer_session")),
        request["candidate"],
        app.native_sessions_root(acceptance["binding"]),
    )
    require(
        verified.get("proof_result_digest") == digest(result), "Original proof review is unbound"
    )
    declarations = [
        payload["proof_dependencies"]
        for artifact in value["artifacts"]
        if isinstance(payload := _read(Path(artifact["path"])), dict)
        and "proof_dependencies" in payload
    ]
    if not declarations:
        return _scoped_package(
            value, record, app.candidate_repository(item_id), item_id, result, verified
        )
    require(
        len(declarations) == 1, "Complete original proof declaration absent; fresh proof required"
    )
    declared = declarations[0]
    original = _tree_inputs(app.candidate_repository(item_id), request["candidate"])
    require(
        declared == {"scope": "all-tracked-files", "complete": True, "inputs": original},
        "Original proof declaration does not cover the authoritative full tree",
    )
    merged = _tree_inputs(record["workspace"], record["candidate"])
    require(original == merged, "Proof dependencies changed; fresh verified proof required")
    return {
        "disposition": "reused",
        "original_proof_digest": digest(result),
        "declaration_digest": digest(declared),
        "review_digest": digest(verified),
        "candidate": record["candidate"],
    }


def _proof_refs(value):
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            yield value
        for child in value.values():
            yield from _proof_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _proof_refs(child)


def _scoped_package(value, record, repository, item_id, result, verified):
    """Validate retained evidence structurally; leave semantic adequacy to native review."""
    roots = [
        Path(row["path"])
        for row in value["artifacts"]
        if Path(row["path"]).name == "proof-result.json"
    ]
    require(len(roots) == 1, "Complete original proof declaration absent; fresh proof required")
    root = roots[0].parent
    package = _read(roots[0])
    candidate = record["original_candidate"]
    require(
        package.get("candidate") == candidate
        and package.get("item_id") == item_id
        and package.get("status") == "evidence-ready"
        and package.get("blockers") == [],
        "Original scoped proof package identity/status differs",
    )
    files = {}

    def reference(row):
        require(
            isinstance(row, dict)
            and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str),
            "Malformed proof package reference",
        )
        path = Path(row["path"])
        require(
            path.is_absolute()
            and path.is_relative_to(root)
            and path.resolve() == path
            and path.is_file(),
            "Proof package reference escapes or is missing",
        )
        require(
            sha256(path.read_bytes()).hexdigest() == row["sha256"],
            "Proof package artifact hash differs",
        )
        name = str(path.relative_to(root))
        require(
            name not in files or files[name]["sha256"] == row["sha256"],
            "Proof package conflicting reference",
        )
        files[name] = {"path": str(path), "sha256": row["sha256"]}
        return name

    for row in value["artifacts"]:
        reference(row)
    rows = package.get("artifacts")
    require(isinstance(rows, list) and rows, "Proof package artifacts missing")
    names = [reference(row) for row in rows]
    require(len(names) == len(set(names)), "Duplicate proof package artifacts")
    required = {
        "candidate-binding.json",
        "committed-input-binding.json",
        "proof-input-inventory.json",
        "independent-result-review.json",
    }
    require(required <= set(names), "Scoped proof package declarations missing")
    # Every recursive path/hash edge must resolve to an explicitly retained package file.
    declared = dict(files)
    for name in list(declared):
        if not name.endswith(".json"):
            continue
        payload = _read(root / name)
        for row in _proof_refs(payload):
            child = reference(row)
            require(
                child in declared and files[child] == declared[child],
                "Proof reference omitted from complete package",
            )
        if isinstance(payload, dict) and "sha256_by_relative_path" in payload:
            hashes = payload["sha256_by_relative_path"]
            require(isinstance(hashes, dict), "Malformed proof hash inventory")
            for relative, expected in hashes.items():
                require(
                    relative in declared and declared[relative]["sha256"] == expected,
                    "Reviewed proof hash inventory differs",
                )
    binding, committed, inventory, review = (
        _read(root / name)
        for name in (
            "candidate-binding.json",
            "committed-input-binding.json",
            "proof-input-inventory.json",
            "independent-result-review.json",
        )
    )
    require(
        all(
            payload.get("candidate") == candidate
            for payload in (binding, committed, inventory, review)
        ),
        "Proof declarations name different candidate",
    )
    require(
        review.get("verdict") == "ACCEPT"
        and review.get("blockers") == []
        and package.get("independent_result_review", {}).get("verdict") == "ACCEPT",
        "Original scoped package review did not accept",
    )
    review_ref = package["independent_result_review"]
    require(
        reference(review_ref) == "independent-result-review.json",
        "Proof package review reference differs",
    )
    require(
        all(
            review.get("sha256_by_relative_path", {}).get(name) == files[name]["sha256"]
            for name in required - {"independent-result-review.json"}
        ),
        "Dependency declarations were not bound by original package review",
    )
    inventory_rows = inventory.get("artifacts")
    require(isinstance(inventory_rows, list) and inventory_rows, "Proof input inventory missing")
    inventory_names = [reference(row) for row in inventory_rows]
    require(
        len(inventory_names) == len(set(inventory_names))
        and set(inventory_names) <= set(names)
        and "candidate-binding.json" in inventory_names,
        "Proof input inventory coverage differs",
    )

    def dependencies(payload, key):
        rows = payload.get("checks")
        require(isinstance(rows, list) and rows, "Proof dependency declaration missing")
        output = {}
        for row in rows:
            name = row.get("path")
            require(
                isinstance(name, str)
                and name
                and not Path(name).is_absolute()
                and ".." not in Path(name).parts
                and ".git" not in Path(name).parts
                and str(Path(name)) == name
                and name not in output,
                "Invalid or duplicate proof dependency path",
            )
            expected = row.get(key)
            require(
                isinstance(expected, str) and len(expected) == 64, "Proof dependency digest missing"
            )
            output[name] = expected
        return output

    inputs = dependencies(binding, "candidate_sha256")
    require(
        committed.get("all_match") is True
        and all(
            row.get("matches_bound_candidate_bytes") is True for row in committed.get("checks", [])
        )
        and inputs == dependencies(committed, "committed_sha256"),
        "Proof dependency declarations omit or disagree on inputs",
    )
    judges = package.get("independent_judges")
    require(
        isinstance(judges, list)
        and judges
        and judges == review.get("independent_judges")
        and all(
            row.get("verdict") == "ACCEPT"
            and row.get("identity")
            and isinstance(row.get("case"), str)
            and row["case"]
            for row in judges
        ),
        "Original proof case/Judge scope differs",
    )
    cases = [row["case"] for row in judges]
    require(len(cases) == len(set(cases)), "Duplicate proof case scope")
    scope = binding.get("scope")
    require(isinstance(scope, str) and scope.strip(), "Original dependency scope missing")
    original_tree = _tree_inputs(repository, candidate)
    merged_tree = _tree_inputs(record["workspace"], record["candidate"])
    dependencies_out = []
    for name, expected in sorted(inputs.items()):
        require(name in original_tree, "Original proof dependency missing")
        original_bytes = blob(repository, candidate, name)
        require(
            sha256(original_bytes).hexdigest() == expected, "Original proof dependency hash differs"
        )
        original_mode = git(
            repository, "--literal-pathspecs", "ls-tree", candidate, "--", name
        ).split()[0]
        require(original_mode in {"100644", "100755"}, "Original proof dependency is not regular")
        merged = None
        if name in merged_tree:
            merged = {
                "mode": git(
                    record["workspace"],
                    "--literal-pathspecs",
                    "ls-tree",
                    record["candidate"],
                    "--",
                    name,
                ).split()[0],
                "sha256": sha256(blob(record["workspace"], record["candidate"], name)).hexdigest(),
            }
        dependencies_out.append(
            {
                "path": name,
                "original": {"mode": original_mode, "sha256": expected},
                "merged": merged,
            }
        )
    context = {
        "kind": "scoped-proof-reuse-v1",
        "item_id": item_id,
        "original_candidate": candidate,
        "candidate": record["candidate"],
        "original_proof_digest": digest(result),
        "review_digest": digest(verified),
        "package_digest": digest(files),
        "package_files": files,
        "dependency_manifest_digest": digest({"binding": binding, "committed": committed}),
        "dependencies": dependencies_out,
        "changed_dependencies": [
            row["path"] for row in dependencies_out if row["original"] != row["merged"]
        ],
        "scope": scope,
        "cases": cases,
        "proof_type": package.get("proof_type"),
        "adequacy": "requires-independent-equivalence-review",
    }
    return {"disposition": "review-required", "context": context, "context_digest": digest(context)}
