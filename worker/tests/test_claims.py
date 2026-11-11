"""The keyed claim as the SDK honours it: reserve before acting, commit what acted, release what did not."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from collab_hub_cog_worker import (
    Cancelled,
    ClaimClient,
    ClaimUnavailable,
    Harness,
    Interaction,
    Task,
    Worker,
    WorkerError,
)
from collab_hub_cog_worker.claims import HUB_ENV
from collab_hub_cog_worker.envelope import status_of

TOKEN = "run-token"


class FakeHub:
    """The hub's claim routes, scripted: what each key answers, and every call made. The key travels in the
    body (or the query), as the hub takes it."""

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
                action = self.path.rsplit("/", 1)[1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = body["key"]
                hub.calls.append((action, key))
                if action == "reserve":
                    claim = hub.claims.get(key)
                    if claim is None:
                        hub.claims[key] = claim = {"key": key, "state": "reserved", "yours": True, "envelope": None}
                        return self._answer(201, claim)
                    return self._answer(200, {**claim, "yours": claim["state"] == "reserved" and claim["yours"]})
                if action == "release":
                    claim = hub.claims.get(key)
                    if claim is None or claim["state"] != "reserved":
                        return self._answer(409, {"error": {"code": "claim_refused", "message": "not held"}})
                    del hub.claims[key]
                    self.send_response(204)
                    self.end_headers()
                    return None
                if hub.refuse_commit:
                    return self._answer(409, {"error": {"code": "claim_refused", "message": "not yours"}})
                hub.claims[key] = {"key": key, "state": "committed", "yours": False, "envelope": body["envelope"]}
                self._answer(200, hub.claims[key])

            def do_GET(self):  # noqa: N802
                key = parse_qs(urlsplit(self.path).query)["key"][0]
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
    assert hub.calls == [("reserve", "r:s:0"), ("commit", "r:s:0")]
    assert hub.claims["r:s:0"]["state"] == "committed"
    assert hub.claims["r:s:0"]["envelope"]["payload"] == {"sent": {"to": "a"}}


def test_a_key_of_any_step_name_is_sent_as_it_is(hub):
    # In the body, never the path: a slash, a colon or a long name changes nothing.
    worker = _worker(hub.url)
    key = "r:fetch/data%3A" + "x" * 400 + ":0"
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key=key)
    assert status == 200 and hub.calls == [("reserve", key), ("commit", key)]
    assert ClaimClient(hub.url, TOKEN).get(key).state == "committed"


def test_a_replay_of_a_committed_key_is_answered_with_its_envelope_and_acts_on_nothing(hub):
    worker = _worker(hub.url)
    _, first = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    status, again = worker.invoke("send", {"to": "b"}, idempotency_key="r:s:0")
    assert status == 200 and again == first  # the first invocation's envelope, whatever this input said
    assert worker.sent == ["r:s:0"]
    assert hub.calls[-1] == ("reserve", "r:s:0")  # no commit after a replay


def test_a_key_reserved_by_a_lost_worker_fails_the_step_outcome_unknown_and_acts_on_nothing(hub):
    hub.claims["r:s:0"] = {"key": "r:s:0", "state": "outcome_unknown", "yours": False, "envelope": None}
    worker = _worker(hub.url)
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 409 and not envelope["ok"] and envelope["error"]["code"] == "outcome-unknown"
    assert "reconciles" in envelope["error"]["detail"] and worker.sent == []
    assert envelope["task"] == "send" and envelope["cog"]["id"] == "sender"
    assert hub.calls == [("reserve", "r:s:0")]  # neither committed nor released: it is not this worker's


def test_a_committed_failure_is_answered_with_its_own_status(hub):
    committed = {
        "envelope": 1, "cog": {"id": "sender", "version": "1.0"}, "task": "send", "ok": False,
        "error": {"code": "model-call-failed", "detail": "it failed"}, "payload": None, "raw": None,
        "problems": [], "binding": None, "usage": None}
    hub.claims["r:s:0"] = {"key": "r:s:0", "state": "committed", "yours": False, "envelope": committed}
    worker = _worker(hub.url)
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 502 and envelope["error"]["code"] == "model-call-failed" and worker.sent == []
    # The one status table, the envelope module's: what a committed envelope is answered with.
    assert status_of(committed) == 502 and status_of({**committed, "ok": True, "error": None}) == 200
    assert status_of({"ok": False, "error": {"code": "outcome-unknown", "detail": ""}}) == 409
    assert status_of({"ok": False, "error": {"code": "not-a-code", "detail": ""}}) == 502
    assert status_of({"ok": False, "error": None}) == 502


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


# --- what is committed: an envelope that means the step acted ------------------------------------------


def test_a_failure_the_entry_point_raised_is_committed_since_it_acted(hub):
    worker = _worker(hub.url)

    @worker.task("flaky")
    def flaky(task: Task, harness: Harness):
        worker.sent.append(task.idempotency_key)  # the side effect happened...
        raise WorkerError("model-call-failed", "...and then the model call failed")

    @worker.task("broken")
    def broken(task: Task, harness: Harness):
        raise RuntimeError("a bug after acting")

    status, envelope = worker.invoke("flaky", None, idempotency_key="r:f:0")
    assert status == 502 and envelope["error"]["code"] == "model-call-failed"
    assert hub.claims["r:f:0"]["state"] == "committed" and hub.claims["r:f:0"]["envelope"] == envelope
    status, envelope = worker.invoke("broken", None, idempotency_key="r:b:0")
    assert status == 502 and "a bug after acting" in envelope["error"]["detail"]
    assert hub.claims["r:b:0"]["state"] == "committed"
    # A replay of either is answered with the committed failure: the step acted, and is not run again under
    # this attempt.
    assert worker.invoke("flaky", None, idempotency_key="r:f:0")[1] == hub.claims["r:f:0"]["envelope"]
    assert worker.sent == ["r:f:0"]


def test_a_task_refused_before_the_entry_point_was_called_releases_the_key(hub, capsys):
    worker = _worker(hub.url)
    # No such entry point: invalid-input, and nothing acted.
    status, envelope = worker.invoke("nothing", None, idempotency_key="r:n:0")
    assert status == 422 and envelope["error"]["code"] == "invalid-input"
    assert hub.calls == [("reserve", "r:n:0"), ("release", "r:n:0")] and "r:n:0" not in hub.claims
    assert "released; nothing acted" in capsys.readouterr().err
    # A harness that cannot start: the entry point never ran.
    def no_harness(context):
        raise WorkerError("model-unavailable", "the model is down")

    down = _worker(hub.url, harness=no_harness)
    status, envelope = down.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 503 and envelope["error"]["code"] == "model-unavailable" and down.sent == []
    assert hub.calls[-2:] == [("reserve", "r:s:0"), ("release", "r:s:0")] and "r:s:0" not in hub.claims
    # The next worker on the attempt reserves the key as new, and acts.
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:s:0")
    assert status == 200 and worker.sent == ["r:s:0"] and hub.claims["r:s:0"]["state"] == "committed"


def test_a_harness_cancelled_while_it_started_releases_the_key(hub):
    # The worker was stopped before its harness was up: the entry point never ran, and nothing acted.
    def cancelled_start(context):
        raise Cancelled()

    worker = _worker(hub.url, harness=cancelled_start)
    status, envelope = worker.invoke("send", {"to": "a"}, idempotency_key="r:c:0")
    assert status == 502 and "cancelled" in envelope["error"]["detail"] and worker.sent == []
    assert hub.calls == [("reserve", "r:c:0"), ("release", "r:c:0")] and "r:c:0" not in hub.claims


def test_an_entry_point_cancelled_while_it_ran_commits_since_it_may_have_acted(hub):
    # The ACP adapter raises Cancelled mid-prompt, after the agent may have run its tools: the step acted, and
    # the failure is committed, so the retry does not act a second time under this attempt.
    worker = _worker(hub.url)

    @worker.task("stopped")
    def stopped(task: Task, harness: Harness):
        worker.sent.append(task.idempotency_key)  # the side effect happened...
        raise Cancelled()  # ...and the run was terminated, or the worker stopped, before the answer

    status, envelope = worker.invoke("stopped", None, idempotency_key="r:c:0")
    assert status == 502 and "cancelled" in envelope["error"]["detail"]
    assert hub.calls == [("reserve", "r:c:0"), ("commit", "r:c:0")]
    assert hub.claims["r:c:0"]["state"] == "committed" and hub.claims["r:c:0"]["envelope"] == envelope
    assert worker.invoke("stopped", None, idempotency_key="r:c:0")[1] == envelope and worker.sent == ["r:c:0"]


def test_a_session_already_held_releases_the_key_while_a_session_that_opened_commits(hub):
    worker = _worker(hub.url, harness=lambda context: _Adapter())
    worker.session("talk")
    # A session that opened and ended on a farewell: it acted (its turns did), and commits.
    opened = threading.Thread(target=lambda: worker.invoke("talk", None, idempotency_key="r:t:0"))
    opened.start()
    for _ in range(200):
        if worker._session is not None and worker._session.opened.is_set():
            break
        threading.Event().wait(0.01)
    assert worker.turn("hello")[0] == 200
    assert worker.turn("bye")[0] == 200
    opened.join(5)
    assert hub.claims["r:t:0"]["state"] == "committed" and hub.claims["r:t:0"]["envelope"]["payload"] == {"turns": 1}
    # A session asked for while one is held: refused before anything opened, and released.
    worker._session = None
    holding = threading.Thread(target=lambda: worker.invoke("talk", None, idempotency_key="r:t:1"))
    holding.start()
    for _ in range(200):
        if worker._session is not None and worker._session.opened.is_set():
            break
        threading.Event().wait(0.01)
    status, envelope = worker.invoke("talk", None, idempotency_key="r:t:2")
    assert status == 422 and "already holds a session" in envelope["error"]["detail"]
    assert "r:t:2" not in hub.claims and ("release", "r:t:2") in hub.calls
    worker.turn("bye")
    holding.join(5)
    assert hub.claims["r:t:1"]["state"] == "committed"


class _Adapter:
    """A harness that answers every prompt and never dies."""

    name = "fake"

    def start(self, context):
        pass

    def interact(self, prompt, context, signal):
        return Interaction(text=f"you said {prompt.text}")

    def alive(self):
        return True

    def cancel(self):
        pass

    def close(self):
        pass


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
    client.release("r:s:0")
    assert client.get("r:s:0") is None
    with pytest.raises(ClaimUnavailable, match="HTTP 409"):
        client.release("r:s:0")  # not held
    client.reserve("r:s:0")
    committed = client.commit("r:s:0", {"envelope": 1, "ok": True, "payload": 1})
    assert committed.state == "committed" and committed.envelope["payload"] == 1
    assert client.get("r:s:0").state == "committed"
    with pytest.raises(ClaimUnavailable, match="HTTP 401"):
        ClaimClient(hub.url, "not-the-token").reserve("r:s:0")
    assert ClaimClient.from_environment({}) is None
    assert ClaimClient.from_environment({HUB_ENV: hub.url, "COLLAB_RUN_TOKEN": TOKEN}).hub_url == hub.url
