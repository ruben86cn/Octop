"""Every gateway test gets private storage, including legacy tests using defaults."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_gateway(tmp_path, monkeypatch):
    import server

    for key in list(os.environ):
        if key.startswith("OCTOP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OCTOP_GATEWAY_KEY", "test-only-key-not-a-real-credential-" + "x" * 32)
    monkeypatch.setenv("OCTOP_GATEWAY_AGENTS", "A")
    monkeypatch.setenv("OCTOP_GATEWAY_DATA", str(tmp_path))
    # unittest cases using patch.dict(clear=True) must also stay isolated.
    monkeypatch.setattr(server, "data_path", lambda name: tmp_path / name)
    yield
