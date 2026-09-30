"""Create isolated, non-replacing SOLO and MULTITASK acceptance providers."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import yaml

from backlog_harness.provider import git

CASES = {
    "greeting": (
        "greeting.py",
        "Implement greet(name): strip whitespace and return 'Hello, NAME!'; blank names use 'world'.",
        "from greeting import greet\nclass Greeting(unittest.TestCase):\n def test_named(self): self.assertEqual(greet(' Ada '), 'Hello, Ada!')\n def test_empty(self): self.assertEqual(greet('  '), 'Hello, world!')\n",
    ),
    "slug": (
        "slugify.py",
        "Implement slugify(text): lowercase ASCII text, replace every run of characters outside a-z and 0-9 with a hyphen, remove leading/trailing hyphens. Empty text returns empty text.",
        "from slugify import slugify\nclass Slug(unittest.TestCase):\n def test_text(self): self.assertEqual(slugify(' Hello,   World! '), 'hello-world')\n def test_empty(self): self.assertEqual(slugify(''), '')\n def test_digits(self): self.assertEqual(slugify('V2_release'), 'v2-release')\n",
    ),
    "clamp": (
        "clamp.py",
        "Implement clamp(value, lower, upper): return value bounded inclusively by lower and upper. If lower > upper, raise ValueError. Inputs in scope are numbers.",
        "from clamp import clamp\nclass Clamp(unittest.TestCase):\n def test_bounds(self): self.assertEqual(clamp(20, 0, 10), 10); self.assertEqual(clamp(-2, 0, 10), 0)\n def test_inside(self): self.assertEqual(clamp(3, 0, 10), 3)\n def test_invalid(self):\n  with self.assertRaises(ValueError): clamp(1, 4, 2)\n",
    ),
}


def create(root, mode, codex, methodology):
    destination = root / mode.lower()
    if destination.exists():
        raise ValueError(
            f"Fixture already exists; reconcile it instead of replacing it: {destination}"
        )
    primary = destination / "provider"
    primary.mkdir(parents=True)
    git(primary, "init", "-b", "main")
    git(primary, "config", "user.name", "Harness Acceptance")
    git(primary, "config", "user.email", "acceptance@example.invalid")
    project = {
        "execution_mode": mode,
        "project_setup": {"concurrent_tasking": mode == "MULTITASK"},
        "resource_coordination": {"selected": "none"},
        "workflow_selection": {
            "canonical_primary_branch": "main",
            "persistence": {"default": "file"},
            "commit": {"default": "main-branch"},
        },
    }
    (primary / "PROJECT.yaml").write_text(yaml.safe_dump(project))
    (primary / ".gitignore").write_text("__pycache__/\n")
    (primary / "backlog/feature-backlog").mkdir(parents=True)
    (primary / "tests").mkdir()
    items = {}
    for name, (source, objective, test) in CASES.items():
        item_id = "dummy-" + name
        test_name = "test_" + name + ".py"
        (primary / "tests" / test_name).write_text("import unittest\n" + test)
        (primary / "backlog/feature-backlog" / (item_id + ".md")).write_text(
            f"# {name.title()}\n\nWork Item ID: {item_id}\nProvider: file\nStatus: Ready\nOwner: Unowned\nOriginal High Generated Tokens: 5000\nDependencies: none\n\n## Objective\n{objective}\n\n## Acceptance\nCreate only {source}. Existing tests are authoritative and must remain unchanged. Run python3 -m unittest discover -s tests -p {test_name} -v. Arrange fresh independent review and request completion with the accepted candidate.\n"
        )
        items[item_id] = {
            "allowed_paths": [source],
            "checks": [
                ["python3", "-m", "unittest", "discover", "-s", "tests", "-p", test_name, "-v"]
            ],
        }
    git(primary, "add", "--", "PROJECT.yaml", ".gitignore", "tests", "backlog")
    git(primary, "commit", "-m", "Seed three acceptance work items")
    template = destination / "workspace-template"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(primary), str(template)],
        check=True,
        capture_output=True,
    )
    clones = destination / "candidates"
    clones.mkdir()
    profiles = {}
    agents = {}
    for role in ("coordinator", "orchestrator", "reviewer"):
        profiles[role] = {
            "role": "dev_" + role,
            "model": "gpt-6-astra",
            "effort": "low",
            "skills": [],
            "tools": ["native"],
            "permissions": ["workspace-write" if role == "orchestrator" else "read"],
        }
        agents[role] = {"cli": "codex", "profile": role}
    config = {
        "version": 1,
        "repository": str(primary),
        "workspace": str(template),
        "candidate_root": str(clones),
        "methodology_root": str(methodology),
        "provider": "file",
        "operational_root": str(destination / "evidence"),
        "poll_seconds": 0.5,
        "runtime_observation_stale_seconds": 30,
        "max_active_invocations": 2,
        "coordinator_limits": {"turns": 1, "generated_tokens": 1000},
        "administrative_review_limits": {"turns": 1, "generated_tokens": 1000},
        "agent_clis": {
            "codex": {
                "adapter": "codex",
                "executable": str(codex),
                "auth_profile": "chatgpt",
                "adapter_options": {"native_max_threads": 2},
            }
        },
        "profiles": profiles,
        "agents": agents,
        "workflow": {
            "completion": "main-branch",
            "mode": mode,
            "primary_branch": "main",
            "allowed_paths": [c[0] for c in CASES.values()],
            "checks": [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]],
            "items": items,
        },
    }
    config_path = destination / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    (destination / "fixture.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "seed_commit": git(primary, "rev-parse", "HEAD"),
                "items": list(items),
                "config": str(config_path),
            },
            indent=2,
        )
        + "\n"
    )
    return str(config_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--methodology-root", type=Path, required=True)
    parser.add_argument("--codex", type=Path, default=Path(shutil.which("codex") or "codex"))
    parser.add_argument("--mode", choices=("SOLO", "MULTITASK", "both"), default="both")
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.methodology_root = args.methodology_root.resolve()
    args.codex = args.codex.absolute()
    print(
        json.dumps(
            [
                create(args.root, mode, args.codex, args.methodology_root)
                for mode in (["SOLO", "MULTITASK"] if args.mode == "both" else [args.mode])
            ],
            indent=2,
        )
    )
