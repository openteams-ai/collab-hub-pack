"""What the SDK's tests run against: the fake ACP agent and the fake model under dev/, in this process or a child."""

from __future__ import annotations

import importlib.util
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
FAKE_AGENT = REPOSITORY / "dev" / "fake-agent" / "fake_acp_agent.py"
FAKE_MODEL = REPOSITORY / "dev" / "fake-model" / "fake_model.py"


def _module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def fake_model_url():
    """The fake OpenAI-compatible model, served from a thread of this process; its ``/v1`` base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _module(FAKE_MODEL).Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def fake_agent_command(*flags: str) -> list[str]:
    return [sys.executable, str(FAKE_AGENT), *flags]
