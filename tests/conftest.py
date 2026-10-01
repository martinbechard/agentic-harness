import sys

import pytest
import yaml


@pytest.fixture
def config_file(tmp_path):
    root = tmp_path / "methodology"
    (root / "skills").mkdir(parents=True)
    repo = tmp_path / "project"
    repo.mkdir()
    candidate = tmp_path / "candidate"
    (candidate / ".git").mkdir(parents=True)
    data = {
        "version": 1,
        "repository": str(repo),
        "workspace": str(candidate),
        "workflow": {
            "completion": "main-branch",
            "mode": "SOLO",
            "primary_branch": "main",
            "allowed_paths": ["answer.py"],
            "checks": [[sys.executable, "-m", "unittest"]],
        },
        "methodology_root": str(root),
        "provider": "file",
        "provider_interaction": "direct",
        "poll_seconds": 1,
        "runtime_observation_stale_seconds": 30,
        "max_active_invocations": 1,
        "coordinator_limits": {"turns": 1, "generated_tokens": 1000},
        "administrative_review_limits": {"turns": 1, "generated_tokens": 1000},
        "agent_clis": {
            "primary": {"adapter": "codex", "executable": sys.executable, "auth_profile": "chatgpt"}
        },
        "profiles": {
            "worker": {
                "role": "dev_orchestrator",
                "model": "test",
                "effort": "low",
                "skills": [],
                "tools": ["native"],
                "permissions": ["workspace-write"],
            }
        },
        "agents": {
            "orchestrator": {"cli": "primary", "profile": "worker"},
            "coordinator": {"cli": "primary", "profile": "control"},
        },
    }
    data["profiles"]["control"] = {
        **data["profiles"]["worker"],
        "role": "dev_coordinator",
        "permissions": ["read"],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, data


from backlog_harness.provider import FileProvider, git


@pytest.fixture
def provider(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "PROJECT.yaml").write_text(
        "workflow_selection:\n  canonical_primary_branch: main\n  persistence: {default: file}\n  commit: {default: main-branch}\nresource_coordination: {selected: none}\nexecution_mode: SOLO\n"
    )
    path = repo / "backlog/feature-backlog/item-one.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# One\n\nWork Item ID: item-one\nProvider: file\nStatus: Ready\nOwner: Unowned\nOriginal High Generated Tokens: 100\n\n## Objective\nOne item.\n"
    )
    git(repo, "add", "--", "PROJECT.yaml", "backlog/feature-backlog/item-one.md")
    git(repo, "commit", "-m", "Initial")
    return FileProvider(repo, tmp_path / "evidence")
