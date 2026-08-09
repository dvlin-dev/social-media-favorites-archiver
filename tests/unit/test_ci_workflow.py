from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_is_valid_yaml_with_expected_jobs() -> None:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    jobs = payload["jobs"]
    assert isinstance(jobs, dict)
    assert {
        "quality",
        "unit-tests",
        "contract-tests",
        "integration-tests",
        "privacy-tests",
        "coverage",
        "package-and-licenses",
    }.issubset(jobs)
