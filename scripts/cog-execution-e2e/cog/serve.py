"""The kind E2E's fake Cog worker, served from its own published artifact. Standard library only.

Answers ``run`` with its input, the send back's findings, and the binding the
hub delivered (the file ``COLLAB_BINDING_FILE`` names, the key's presence and
nothing of its value), ``resolve`` with the first satisfier of each required
capability, and ``check`` with the binding it sees. It listens where the
controller says and answers only the bearer of its run token.
"""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COG = os.environ.get("COLLAB_COG_ID", "e2e/echo")
TOKEN = os.environ.get("COLLAB_RUN_TOKEN", "")
_SEEN: dict[str, dict] = {}


def binding() -> dict | None:
    path = os.environ.get("COLLAB_BINDING_FILE")
    if not path:
        return None
    with open(path) as handle:
        document = json.load(handle)
    auth_ref = document.get("auth_ref")
    document["key_present"] = bool(auth_ref) and bool(os.environ.get(str(auth_ref)[4:]))
    return document


def envelope(payload, task):
    return {"envelope": 1, "cog": {"id": COG, "version": "0.1.0"}, "task": task, "ok": True, "error": None,
            "payload": payload, "raw": None, "problems": [], "binding": binding(), "usage": {"tokens": 10}}


def answer(request: dict) -> dict:
    entry, value = request.get("entry_point"), request.get("input")
    key = request.get("idempotency_key")
    if key is not None and key in _SEEN:
        return _SEEN[key]
    if entry == "resolve":
        chosen = {}
        for requirement in (value or {}).get("requires", []):
            for satisfier in (value or {}).get("inventory", []):
                if requirement["capability"] in satisfier.get("provides", []):
                    chosen.setdefault(requirement["capability"], satisfier["id"])
        result = envelope({"selections": chosen}, entry)
    elif entry == "check":
        result = envelope({"checked": True, "binding": binding()}, entry)
    else:
        names = sorted(k for k in os.environ if k.startswith("COLLAB_"))
        result = envelope({"cog": COG, "entry_point": entry, "echo": value, "signal": request.get("signal"),
                           "binding": binding(), "environment": names}, entry)
    if key is not None:
        _SEEN[key] = result
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200 if self.path == "/healthz" else 404, {"ok": self.path == "/healthz", "cog": COG})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invoke":
            return self._send(404, {"error": "not found"})
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not TOKEN or not hmac.compare_digest(presented.encode(), TOKEN.encode()):
            return self._send(401, {"error": "not the run token of this worker"})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self._send(200, answer(request))

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    host, port = os.environ.get("COLLAB_COG_HOST", "0.0.0.0"), int(os.environ.get("COLLAB_COG_PORT", "8080"))
    print(f"{COG}: serving on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
