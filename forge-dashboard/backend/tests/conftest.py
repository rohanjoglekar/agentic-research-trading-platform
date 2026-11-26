"""Shared fixtures for the backend tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def api_server():
    """The FastAPI app module (main.py).

    Not every checkout carries the server: the public repository has the
    research modules and the gateway policy, but not the API that also serves
    the owner's account. Tests that need the server skip there. Where the file
    exists, a broken import still fails loudly.
    """
    if not (BACKEND / "main.py").exists():
        pytest.skip("needs the API server (main.py), which this checkout does not carry")
    pytest.importorskip("httpx")                  # TestClient's transport
    import main
    return main
