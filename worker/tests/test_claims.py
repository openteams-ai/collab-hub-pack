"""The keyed claim as the SDK honours it: reserve before acting, commit after, and never act twice."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from collab_hub_cog_worker import ClaimClient, ClaimUnavailable, Harness, Task, Worker
from collab_hub_cog_worker.claims import HUB_ENV

TOKEN = "run-token"


class FakeHub:
    """The hub's claim routes, scripted: what each key answers, and every call made."""

    def __init__(self):
        self.claims: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.refuse_commit = False
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._answer(401, {"error": {"code": "run_token_invalid", "message": "no"}})
                _, _, _, key, action = self.path.split("/")
                hub.calls.append((action, key))
                if action == "reserve":
                    claim = hub.claims.get(key)
                    if claim is None:
                        hub.claims[key] = claim = {"key": key, "state": "reserved", "yours": True, "envelope": None}
                        return self._answer(201, claim)
                    return self._answer(200, {**claim, "yours": claim["state"] == "reserved" and claim["yours"]})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if hub.refuse_commit:
                    return self._answer(409, {"error": {"code": "claim_refused", "message": "not yours"}})
                hub.claims[key] = {"key": key, "state": "committed", "yours": False, "envelope": body["envelope"]}
                self._answer(200, hub.claims[key])

            def do_GET(self):  # noqa: N802
                key = self.path.rsplit("/", 1)[1]
                hub.calls.append(("get", key))
                claim = hub.claims.get(key)
                if claim is None:
                    return self._answer(404, {"error": {"code": "claim_not_found", "message": "no"}})
                self._answer(200, claim)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hub():
    fake = FakeHub()
    yield fake
    fake.close()


def _worker(hub_url=None, **kwargs):
    environ = {"COLLAB_RUN_TOKEN": TOKEN, "COLLAB_COG_ID": "sender", "COLLAB_RUN_ID": "r"}
    if hub_url:
        environ[HUB_ENV] = hub_url
    worker = Worker("sender", "1.0", environ=environ, **kwargs)
    worker.sent = []

    @worker.task("send")
    def send(task: Task, harness: Harness):
        worker.sent.append(task.idempotency_key)
        return {"sent": task.input}

    return worker


def test_a_key_is_reserved_before_the_entry_point_acts_and_committed_after(hub):
    worker = _worker(hub.url)
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 200 and envelope["ok"] and envelope["payload"] == {"sent": {"to": "a"}}
    assert worker.sent == ["r:s:0"]
    assert hub.calls == [("reserve", "r%3As%3A0"), ("commit", "r%3As%3A0")]  # the key, percent-escaped in the path
    assert hub.claims["r%3As%3A0"]["state"] == "committed"
    assert hub.claims["r%3As%3A0"]["envelope"]["payload"] == {"sent": {"to": "a"}}


def test_a_replay_of_a_committed_key_is_answered_with_its_envelope_and_acts_on_nothing(hub):
    worker = _worker(hub.url)
    _, first = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    status, again = worker.invoke("send", {"to": "b"}, idempotency_key="r:s:0")
    assert status == 200 and again == first  # the first invocation's envelope, whatever this input said
    assert worker.sent == ["r:s:0"]
    assert hub.calls[-1] == ("reserve", "r%3As%3A0")  # no commit after a replay


def test_a_key_reserved_by_a_lost_worker_fails_the_step_outcome_unknown_and_acts_on_nothing(hub):
    hub.claims["r%3As%3A0"] = {"key": "r:s:0", "state": "outcome_unknown", "yours": False, "envelope": None}
    worker = _worker(hub.url)
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 409 and not envelope["ok"] and envelope["error"]["code"] == "outcome-unknown"
    assert "reconciles" in envelope["error"]["detail"] and worker.sent == []
    assert envelope["task"] == "send" and envelope["cog"]["id"] == "sender"


def test_a_committed_failure_is_answered_with_its_own_status(hub):
    hub.claims["r%3As%3A0"] = {"key": "r:s:0", "state": "committed", "yours": False, "envelope": {
        "envelope": 1, "cog": {"id": "sender", "version": "1.0"}, "task": "send", "ok": False,
        "error": {"code": "model-call-failed", "detail": "it failed"}, "payload": None, "raw": None,
        "problems": [], "binding": None, "usage": None}}
    worker = _worker(hub.url)
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 502 and envelope["error"]["code"] == "model-call-failed" and worker.sent == []


def test_a_hub_that_cannot_be_reached_fails_the_step_before_anything_acts():
    worker = _worker("http://127.0.0.1:1")  # nothing listens there
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 503 and envelope["error"]["code"] == "hub-unavailable"
    assert "could not be reserved" in envelope["error"]["detail"] and worker.sent == []


def test_a_commit_the_hub_refuses_is_logged_and_the_envelope_answered_all_the_same(hub, capsys):
    worker = _worker(hub.url)
    hub.refuse_commit = True
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 200 and envelope["payload"] == {"sent": {"to": "a"}} and worker.sent == ["r:s:0"]
    assert "could not be committed" in capsys.readouterr().err


def test_without_a_hub_or_a_key_nothing_is_claimed(hub):
    worker = _worker()  # no COLLAB_HUB_URL
    assert worker.claims is None
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 200 and worker.sent == ["r:s:0"] and hub.calls == []
    with_hub = _worker(hub.url)
    status, envelope = with_hub.invoke("send", {"to": "a"})  # a task without a key: nothing to claim
    assert status == 200 and with_hub.sent == [None] and hub.calls == []


def test_the_client_reads_a_key_and_reports_what_the_hub_said(hub):
    client = ClaimClient(hub.url, TOKEN)
    assert client.get("r:s:0") is None
    reserved = client.reserve("r:s:0")
    assert reserved.state == "reserved" and reserved.yours and reserved.envelope is None
    committed = client.commit("r:s:0", {"envelope": 1, "ok": True, "payload": 1})
    assert committed.state == "committed" and committed.envelope["payload"] == 1
    assert client.get("r:s:0").state == "committed"
    with pytest.raises(ClaimUnavailable, match="HTTP 401"):
        ClaimClient(hub.url, "not-the-token").reserve("r:s:0")
    assert ClaimClient.from_environment({}) is None
    assert ClaimClient.from_environment({HUB_ENV: hub.url, "COLLAB_RUN_TOKEN": TOKEN}).hub_url == hub.url
