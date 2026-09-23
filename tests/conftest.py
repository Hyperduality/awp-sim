from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SPEC = Path(__file__).resolve().parent.parent / "spec"
TRACES = SPEC / "examples" / "v0.1" / "traces"


def load_trace(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (TRACES / name).read_text().splitlines() if line.strip()]


@pytest.fixture(scope="session")
def spec_dir() -> Path:
    if not (SPEC / "schemas").is_dir():
        pytest.skip("spec submodule not checked out")
    return SPEC
