from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from awp_sim import scenarios

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def results():
    return scenarios.run_all()


def test_every_scenario_passes(results):
    failed = {
        r.name: [c for c, ok in r.checks if not ok] or r.error for r in results if not r.passed
    }
    assert failed == {}


def test_traces_pass_the_spec_checker(results, tmp_path, spec_dir):
    if shutil.which("node") is None or shutil.which("npm") is None:
        pytest.skip("node is required for the spec's trace checker")
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_traces

    scenarios.write(results, tmp_path)
    assert check_traces.check(sorted(tmp_path.glob("*.jsonl"))) == 0


def test_the_spec_checked_against_is_the_revision_awp_python_targets(spec_dir):
    import awp

    tag = subprocess.run(
        ["git", "-C", str(spec_dir), "describe", "--tags", "--exact-match"],
        capture_output=True,
        text=True,
    )
    if tag.returncode != 0:
        pytest.skip("spec checkout is not at a tag")
    assert tag.stdout.strip() == f"spec-v{awp.SPEC_REVISION}"
