"""Append-only amendments bind generator authority to a complete failed check."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_integration_authority import case  # noqa: F401

from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json
from backlog_harness.integration_flow import apply_amendment, check_feedback, validate_checks
from backlog_harness.provider import TransitionBlocked


@pytest.fixture
def amendment_case(tmp_path):
    instruction = {"item_id": "item", "primary": "primary", "original_candidate": "original"}
    record = {"candidate": "failed", "primary": "primary"}
    checks = [
        {
            "argv": ["python", "generate.py", "--check"],
            "candidate": "failed",
            "returncode": 1,
            "output": "missing generated output",
            "evidence_sha256": sha256(b"missing generated output").hexdigest(),
        }
    ]
    stage = lambda item, name: tmp_path / (name + ".json")
    atomic_json(stage("item", "integration-candidate"), record)
    atomic_json(stage("item", "integration-checks"), checks)
    seen = []

    def validate(item, value):
        seen.append(value)
        return value

    app = SimpleNamespace(
        _stage_path=stage,
        item_workflow=lambda item: {"checks": [["python", "generate.py", "--check"]]},
        validate_check_execution=lambda *args: None,
        validate_integration_instruction=validate,
    )
    amendment = {
        "item_id": "item",
        "instruction_digest": digest(instruction),
        "failed_attempt": 1,
        "failed_candidate": "failed",
        "primary": "primary",
        "next_attempt": 2,
        "purpose": "reproduce-failed-required-check",
        "required_check": ["python", "generate.py", "--check"],
        "generators": [["python", "generate.py"]],
        "generated_paths": ["output"],
        "generator_inputs": {"generate.py": "hash"},
    }
    return (
        app,
        instruction,
        amendment,
        check_feedback(record, checks),
        {"primary": "primary", "prospective_estimate": {"remaining_high": 1}},
        seen,
    )


def test_amendment_is_additive_and_preserves_original_instruction(amendment_case):
    app, instruction, amendment, feedback, context, seen = amendment_case
    original = deepcopy(instruction)
    result = apply_amendment(app, "item", instruction, amendment, feedback, context)
    assert instruction == original
    assert result["prospective_estimate"] == context["prospective_estimate"]
    assert seen == [
        {
            **instruction,
            **{
                key: amendment[key] for key in ("generators", "generated_paths", "generator_inputs")
            },
        }
    ]
    assert apply_amendment(app, "item", instruction, amendment, feedback, context) == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("instruction_digest", "changed"),
        ("failed_candidate", "other"),
        ("primary", "other"),
        ("next_attempt", 3),
        ("failed_attempt", True),
        ("required_check", ["unconfigured"]),
        ("allowed_paths", ["new"]),
        ("prospective_estimate", {"remaining_high": 999}),
    ],
)
def test_amendment_rejects_unbound_or_widened_fields(amendment_case, field, value):
    app, instruction, amendment, feedback, context, seen = amendment_case
    amendment[field] = value
    with pytest.raises(TransitionBlocked):
        apply_amendment(app, "item", instruction, amendment, feedback, context)
    assert not seen


@pytest.mark.parametrize("mutation", ["missing", "candidate", "hash", "argv", "signal", "boolean"])
def test_only_complete_terminal_checks_are_feedback(amendment_case, mutation):
    app, _instruction, _amendment, feedback, _context, _seen = amendment_case
    rows = deepcopy(feedback["checks"])
    if mutation == "missing":
        rows.clear()
    elif mutation == "candidate":
        rows[0]["candidate"] = "wrong"
    elif mutation == "hash":
        rows[0]["evidence_sha256"] = "wrong"
    elif mutation == "argv":
        rows[0]["argv"] = ["other"]
    elif mutation == "signal":
        rows[0]["returncode"] = -9
    else:
        rows[0]["returncode"] = True
    with pytest.raises(TransitionBlocked):
        validate_checks(rows, "failed", app.item_workflow("item")["checks"], allow_failure=True)


@pytest.mark.parametrize("fault", ["new-script", "outside-output", "runtime", "changed-input"])
def test_amendment_reuses_real_generator_authority(case, fault, monkeypatch):  # noqa: F811
    from backlog_harness.integration_authority import validate_integration_instruction

    app, instruction, _review = case
    monkeypatch.setattr("backlog_harness.integration_authority.blob", lambda *args: b"candidate")
    amended = {
        **instruction,
        "generators": [["check", "a"]],
        "generated_paths": ["a"],
        "generator_inputs": {"a": sha256(b"candidate").hexdigest()},
    }
    if fault == "new-script":
        amended["generators"] = [["check", "new.py"]]
        amended["generator_inputs"] = {"new.py": sha256(b"new").hexdigest()}
    elif fault == "outside-output":
        amended["generated_paths"] = ["outside"]
    elif fault == "runtime":
        amended["generators"] = [["unconfigured", "a"]]
    elif fault == "changed-input":
        amended["generator_inputs"] = {"a": "wrong"}
    with pytest.raises(TransitionBlocked):
        validate_integration_instruction(app, "item", amended)


@pytest.mark.parametrize("executed", ["generate.py", "other.py"])
def test_amendment_binds_executed_script_after_bytecode_flag(amendment_case, executed):
    app, instruction, amendment, feedback, context, seen = amendment_case
    command = ["python", "-B", executed, "generate.py", "--check"]
    amendment["required_check"] = command
    feedback["checks"][0]["argv"] = command
    feedback["checks_digest"] = digest(feedback["checks"])
    atomic_json(app._stage_path("item", "integration-checks"), feedback["checks"])
    app.item_workflow = lambda item: {"checks": [command]}
    if executed == "other.py":
        with pytest.raises(TransitionBlocked, match="does not reproduce"):
            apply_amendment(app, "item", instruction, amendment, feedback, context)
        assert seen == []
    else:
        assert apply_amendment(app, "item", instruction, amendment, feedback, context)
