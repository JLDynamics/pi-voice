"""run-browser.sh decides whether to reuse a running service from this probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "service_state.py"
spec = importlib.util.spec_from_file_location("service_state", SCRIPT)
assert spec and spec.loader
service_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_state)

CHECKOUT = "/Users/me/chatbot"
CURRENT = {"ready": True, "server_tools": True, "fingerprint": "abc", "stale": False, "source": CHECKOUT}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (CURRENT, "current"),
        ({**CURRENT, "ready": False}, "current"),  # still loading models, but current code
        ({**CURRENT, "ready": False, "server_tools": False}, "current"),  # LLM handler not set up yet
        ({**CURRENT, "stale": True}, "stale"),
        ({**CURRENT, "server_tools": False}, "current"),  # ready without Luna tools is still this checkout
        ({**CURRENT, "source": "/Users/me/chatbot-refactor"}, "foreign"),
        ({"status": "ok", "ready": True}, "unknown"),  # pre-fingerprint backend
        ({**CURRENT, "fingerprint": None}, "unknown"),
        ([1, 2], "unknown"),
    ],
)
def test_classify(payload, expected):
    assert service_state.classify(payload, CHECKOUT) == expected


def test_a_payload_without_server_tools_is_still_current():
    """The fingerprint contract is what decides, not which tools a build has."""
    health = {"ready": True, "fingerprint": "abc", "stale": False, "source": CHECKOUT}
    assert service_state.classify(health, CHECKOUT) == "current"


@pytest.fixture
def health_server():
    payloads: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = payloads.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, payloads
    finally:
        server.shutdown()
        server.server_close()


def test_probe_reads_live_health(health_server):
    port, payloads = health_server
    payloads["/health"] = {**CURRENT, "stale": True}
    assert service_state.probe(f"http://127.0.0.1:{port}/health", CHECKOUT) == "stale"
    payloads["/health"] = CURRENT
    assert service_state.probe(f"http://127.0.0.1:{port}/health", CHECKOUT) == "current"
    assert service_state.probe(f"http://127.0.0.1:{port}/missing", CHECKOUT) == "unreachable"


def test_probe_reports_unreachable_when_nothing_listens():
    with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as spare:
        port = spare.server_port
    assert service_state.probe(f"http://127.0.0.1:{port}/health", CHECKOUT, timeout=0.5) == "unreachable"


def test_command_line_prints_one_word(health_server):
    port, payloads = health_server
    payloads["/api/config"] = {"chatbotUrl": "ws://x", "fingerprint": "abc", "stale": False, "source": CHECKOUT}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), f"http://127.0.0.1:{port}/api/config", CHECKOUT],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "current"
    usage = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert usage.returncode == 2
    assert "HEALTH_URL" in usage.stderr
