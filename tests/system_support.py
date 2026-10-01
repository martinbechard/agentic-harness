"""Installed-wheel subprocess fixtures; no production validator substitutions."""

import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import yaml


def command(argv, *, cwd=None, env=None, timeout=120):
    return subprocess.run(
        list(map(str, argv)),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def install_wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-wheel")
    source = Path(__file__).resolve().parents[1]
    source_before = {
        str(p.relative_to(source)): sha256(p.read_bytes()).hexdigest()
        for p in sorted((source / "src").rglob("*.py"))
    }
    source_head = command(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    build = command(["uv", "build", "--wheel", "--out-dir", root / "dist"], cwd=source)
    assert build.returncode == 0, build.stderr
    create = command(["uv", "venv", "--python", sys.executable, root / "venv"])
    assert create.returncode == 0, create.stderr
    python = root / "venv/bin/python"
    install = command(
        ["uv", "pip", "install", "--python", python, *list((root / "dist").glob("*.whl"))]
    )
    assert install.returncode == 0, install.stderr
    wheel = next((root / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = {
            name: sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.endswith(".py")
        }
    (root / "installation.json").write_text(
        json.dumps(
            {
                "wheel": str(wheel),
                "wheel_sha256": sha256(wheel.read_bytes()).hexdigest(),
                "source_head": source_head,
                "source_before": source_before,
                "wheel_modules": wheel_files,
                "wheel_matches_source_before": all(
                    source_before.get("src/" + name) == value for name, value in wheel_files.items()
                ),
                "source_files": {
                    str(p.relative_to(source)): sha256(p.read_bytes()).hexdigest()
                    for p in sorted((source / "src").rglob("*.py"))
                },
            },
            sort_keys=True,
        )
    )
    return python


@dataclass
class InstalledHarness:
    config_path: Path
    repo: Path
    candidate: Path
    native_home: Path
    scenario: str
    python: Path
    env: dict

    @classmethod
    def create(cls, root, wheel_python, scenario="legacy", dispatcher=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        repo, candidate, home = root / "repo", root / "candidate", root / "native"
        repo.mkdir()
        home.mkdir()

        def git(*args):
            result = command(["git", "-C", repo, *args])
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        git("init", "-b", "main")
        git("config", "user.name", "System Test")
        git("config", "user.email", "system@example.invalid")
        (repo / "PROJECT.yaml").write_text(
            "workflow_selection:\n  canonical_primary_branch: main\n  persistence: {default: file}\n  commit: {default: main-branch}\nresource_coordination: {selected: none}\nexecution_mode: SOLO\n"
        )
        (repo / ".gitignore").write_text(".agent-ops/\n")
        path = repo / "backlog/feature-backlog/item-one.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# One\n\nWork Item ID: item-one\nProvider: file\nStatus: Ready\nOwner: Unowned\nOriginal High Generated Tokens: 1000\n\n## Objective\nWrite answer.txt with done.\n"
        )
        git("add", ".")
        git("commit", "-m", "Initial system fixture")
        clone = command(["git", "clone", repo, candidate])
        assert clone.returncode == 0, clone.stderr
        for key, value in [("user.name", "System Test"), ("user.email", "system@example.invalid")]:
            assert command(["git", "-C", candidate, "config", key, value]).returncode == 0
        methodology = root / "methodology"
        for name in ["manage-work-items", "manage-work-items-file"]:
            skill = methodology / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Use the authoritative file provider.\n")
        script = root / "agent"
        script.write_text(
            "#!"
            + str(wheel_python)
            + "\n"
            + (Path(__file__).parent / "fixtures/system_agent.py").read_text()
        )
        script.chmod(0o755)
        profile = {
            "role": "dev_orchestrator",
            "model": "fixture",
            "effort": "low",
            "skills": ["manage-work-items", "manage-work-items-file"],
            "tools": ["native"],
            "permissions": ["workspace-write"],
        }
        data = {
            "version": 1,
            "repository": str(repo),
            "workspace": str(candidate),
            "methodology_root": str(methodology),
            "provider": "file",
            "provider_interaction": "agent",
            "poll_seconds": 1,
            "runtime_observation_stale_seconds": 30,
            "max_active_invocations": 1,
            "invocation_timeout_seconds": 15,
            "coordinator_limits": {"turns": 1, "generated_tokens": 1000},
            "administrative_review_limits": {"turns": 1, "generated_tokens": 1000},
            "workflow": {
                "completion": "main-branch",
                "mode": "SOLO",
                "primary_branch": "main",
                "allowed_paths": ["answer.txt"],
                "checks": [
                    [
                        str(wheel_python),
                        "-c",
                        "from pathlib import Path; assert Path('answer.txt').read_text() == 'done\\n'",
                    ]
                ],
            },
            "agent_clis": {
                "primary": {
                    "adapter": "codex",
                    "executable": str(script),
                    "auth_profile": "chatgpt",
                    "adapter_options": {"codex_home": str(home)},
                }
            },
            "profiles": {"worker": profile, "control": {**profile, "role": "dev_coordinator"}},
            "agents": {
                "orchestrator": {"cli": "primary", "profile": "worker"},
                "coordinator": {"cli": "primary", "profile": "control"},
            },
        }
        config = root / "config.yaml"
        config.write_text(yaml.safe_dump(data))
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        env.update(
            GIT_AUTHOR_NAME="System Test",
            GIT_AUTHOR_EMAIL="system@example.invalid",
            GIT_COMMITTER_NAME="System Test",
            GIT_COMMITTER_EMAIL="system@example.invalid",
            HARNESS_SYSTEM_HELPERS=str(Path(__file__).parent / "fixtures"),
            HARNESS_SYSTEM_SCENARIO=scenario,
            HARNESS_SYSTEM_REPO=str(repo),
            HARNESS_SYSTEM_DISPATCHER=str(
                dispatcher or Path(__file__).parent / "fixtures/legacy_agent.py"
            ),
        )
        return cls(config, repo, candidate, home, scenario, Path(wheel_python), env)

    def run(self, *args, timeout=60):
        return command(
            [self.python.parent / "agentic-harness", "--config", self.config_path, *args],
            cwd=self.repo,
            env=self.env,
            timeout=timeout,
        )
