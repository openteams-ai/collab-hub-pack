"""The worker of a fake Cog: serves the seam for one `handle` function. Standard library only.

A fake Cog's `serve.py` defines `handle(entry_point, value, **feedback)`, which
returns a result envelope (`envelope()` below builds one), and ends with
`serve(handle)`. Served, it is what any Cog worker is to the controller:

    GET  /healthz   200 once it can answer
    POST /invoke    {entry_point, input, idempotency_key, signal?} -> a result envelope

It listens where the controller tells it to, `COLLAB_COG_HOST` and
`COLLAB_COG_PORT`, and answers `/invoke` only to the bearer of the run token in
its environment, `COLLAB_RUN_TOKEN`. `make op` without a location calls `handle`
in its own process instead, and never starts this server.

It is the reference worker for the keyed claim (docs/cog-execution/op-cog-seam.md,
"The keyed claim"), serving the seam without the SDK's `Worker`: given a hub
(`COLLAB_HUB_URL`), it reserves the task's idempotency key there before `handle`
acts, with its run token, and commits the envelope after; `handle` always acts,
so it never has a key to release. A key committed already is answered with its
envelope and `handle` never runs; a key reserved by a worker that was lost
before committing fails the step `outcome-unknown`, and nothing acts again.
What the hub answered is printed to stderr, which the run's log directory
keeps. Without a hub it acts on every invocation, as `make op OP=...
LOCATION=local` runs it. The envelope's status table is the SDK's
(`collab_hub_cog_worker.envelope`, standard library only), read from this
checkout so it is written once.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The SDK as this checkout has it; a copy of these fakes elsewhere places the package beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker" / "src"))

from collab_hub_cog_worker.envelope import status_of  # noqa: E402

COG = os.environ.get("COLLAB_COG_ID", "unknown")


def envelope(payload=None, *, ok=True, error=None, problems=(), usage=None):
    """A version-1 result envelope (docs/cog-execution/result-envelope.md)."""
    return {
        "envelope": 1, "cog": {"id": COG, "version": "0.0.0"}, "task": None, "ok": ok, "error": error,
        "payload": payload if ok else None, "raw": None, "problems": list(problems), "binding": None, "usage": usage,
    }


def failure(code, detail):
    return envelope(ok=False, error={"code": code, "detail": detail})


def log(message):
    print(f"{COG}: {message}", file=sys.stderr, flush=True)


class Claims:
    """The keyed claim over HTTP, with this worker's run token: reserve before acting, commit after."""

    def __init__(self, hub_url, token):
        self.hub_url = hub_url.rstrip("/")
        self.token = token

    def call(self, action, body):
        """`POST /v1/claims/{action}` with the key in the body, never the path: any step name fits."""
        request = urllib.request.Request(
            f"{self.hub_url}/v1/claims/{action}", method="POST", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the hub's URL
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}

    def reserve(self, key):
        """The envelope to answer with instead of acting, or None when this worker is to act."""
        try:
            status, claim = self.call("reserve", {"key": key})
        except (urllib.error.URLError, OSError) as exc:
            log(f"claim {key}: the hub at {self.hub_url} could not be reached, so nothing acted: {exc}")
            return failure("hub-unavailable", f"the key {key} could not be reserved: {exc}")
        if status == 201 and claim.get("yours"):
            log(f"claim {key}: reserved")
            return None
        if status == 200 and claim.get("state") == "committed" and claim.get("envelope"):
            log(f"claim {key}: committed already; answering with its envelope, acting on nothing")
            return claim["envelope"]
        if status == 200:
            log(f"claim {key}: {claim.get('state')}; nothing acts on it again")
            return failure("outcome-unknown", f"the key {key} is {claim.get('state')}: reserved by a worker that "
                                              f"never committed its result, so its side effect may have happened")
        log(f"claim {key}: the hub answered HTTP {status} {claim}; nothing acted")
        return failure("hub-unavailable", f"the key {key} could not be reserved: HTTP {status}")

    def commit(self, key, answer):
        try:
            status, claim = self.call("commit", {"key": key, "envelope": answer})
        except (urllib.error.URLError, OSError) as exc:
            log(f"claim {key}: the envelope could not be committed, and is answered all the same: {exc}")
            return
        log(f"claim {key}: {'committed' if status == 200 else f'commit refused with HTTP {status} {claim}'}")


def serve(handle) -> None:
    token = os.environ.get("COLLAB_RUN_TOKEN", "")
    hub_url = os.environ.get("COLLAB_HUB_URL", "")
    claims = Claims(hub_url, token) if hub_url and token else None

    def invoke(request):
        feedback = {"signal": request["signal"]} if "signal" in request else {}
        key = request.get("idempotency_key")
        if claims is None or not isinstance(key, str) or not key:
            return handle(request.get("entry_point"), request.get("input"), **feedback)
        instead = claims.reserve(key)
        if instead is not None:
            return instead
        answer = {**handle(request.get("entry_point"), request.get("input"), **feedback),
                  "task": request.get("entry_point")}
        claims.commit(key, answer)
        return answer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send(200, {"ok": True, "cog": COG})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/invoke":
                self._send(404, {"error": "not found"})
                return
            presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not token or not hmac.compare_digest(presented.encode(), token.encode()):
                self._send(401, {"error": "not the run token of this worker"})
                return
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            answer = invoke(request)
            self._send(status_of(answer), {**answer, "task": request.get("entry_point")})

        def log_message(self, *args) -> None:  # the access log is noise in a run's stderr
            pass

    host, port = os.environ.get("COLLAB_COG_HOST", "127.0.0.1"), int(os.environ["COLLAB_COG_PORT"])
    print(f"{COG}: serving on {host}:{port}" + (f", claiming on {hub_url}" if claims else ""), flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
