from __future__ import annotations

import json

from awp import schema
from awp_sim.cli import main


def test_manifest(capsys):
    assert main(["manifest", "--mode", "lockstep"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert schema.errors("world-manifest", manifest, sender=True) == []


def test_scenarios_list_run_and_write(capsys, tmp_path):
    assert main(["scenarios", "--list"]) == 0
    assert "quiet-agent" in capsys.readouterr().out
    assert main(["scenarios", "limit-exceeded", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "limit-exceeded--agent.jsonl").exists()
    assert json.loads((tmp_path / "report.json").read_text())["passed"] is True
    assert main(["scenarios", "no-such-scenario"]) == 2


def test_serve_refuses_an_unauthenticated_public_bind(capsys):
    assert main(["serve", "--host", "0.0.0.0", "--port", "0"]) == 2
    assert "AWP-SEC-002" in capsys.readouterr().err
