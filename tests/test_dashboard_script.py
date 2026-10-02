"""Exercise dashboard behavior and require coverage of every V8-reported production block."""

import json
import os
import shutil
import subprocess
from pathlib import Path


def test_dashboard_javascript_coverage(tmp_path):
    node = shutil.which("node")
    assert node, "Node.js is required to verify the production dashboard script"
    root = Path(__file__).resolve().parents[1]
    coverage = tmp_path / "javascript-coverage"
    result = subprocess.run(
        [node, "--test", str(root / "tests/dashboard.test.cjs")],
        env={**os.environ, "NODE_V8_COVERAGE": str(coverage)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    script = root / "src/backlog_harness/static/dashboard.js"
    counts = {}
    for record in coverage.glob("*.json"):
        for source in json.loads(record.read_text())["result"]:
            if source["url"] not in {str(script), script.as_uri()}:
                continue
            for function in source["functions"]:
                assert function["isBlockCoverage"], function
                for block in function["ranges"]:
                    key = (function["functionName"], block["startOffset"], block["endOffset"])
                    counts[key] = counts.get(key, 0) + block["count"]
    assert counts, "No production dashboard coverage was collected"
    assert all(count > 0 for count in counts.values()), {
        key: count for key, count in counts.items() if count == 0
    }
