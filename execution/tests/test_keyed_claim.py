"""The keyed claim from the controller's side: the hub a worker claims against, and a real worker claiming.

The store and its stores are ``test_claims_conformance.py``; what a replay does
to a run is in the lifecycle and durability suites. Here: the hub's URL reaches
a local and a remote worker's environment, the controller takes it as an option,
and the reference worker of ``dev/cogs`` reserves and commits its key over HTTP
with the run token the executor minted. That a token reaches its own step
attempt's key and no other is the hub's routes' to refuse, asserted against them
in the API suite (``api/tests/test_claims_api.py``).
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from locations_support import local_executor

from collab_hub_execution import (
    InMemoryClaimStore,
    InMemoryTrackStore,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    RunState,
    run_tokens,
)
from collab_hub_execution.claims import run_of
from collab_hub_execution.controller import parse_args
from collab_hub_execution.kubernetes import KubernetesCogExecutor


class _Hub:
    """A stand-in for the hub's claim routes: the same contract, over an in-memory store, verifying the run
    token against the Track as the API does."""

    def __init__(self, track):
        self.track = track
        self.store = InMemoryClaimStore()
        self.calls: list[tuple[str, str, int]] = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _claim(self, action):
                if action == "get":
                    key = parse_qs(urlsplit(self.path).query)["key"][0]
                    body = {}
                else:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    key = body["key"]
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                run_id = run_of(key)
                started = run_tokens.verify(hub.track.replay(run_id), token) if run_id else None
                if started is None:
                    hub.calls.append((action, key, 401))
                    return self._answer(401, {"error": {"code": "run_token_invalid", "message": "no"}})
                worker = started["run_token_sha256"]
                if action == "reserve":
                    claim, acts = hub.store.reserve(run_id, key, worker=worker)
                    code = 201 if acts else 200
                elif action == "commit":
                    claim, code = hub.store.commit(run_id, key, worker=worker, envelope=body["envelope"]), 200
                elif action == "release":
                    hub.store.release(run_id, key, worker=worker)
                    hub.calls.append((action, key, 204))
                    return self._answer(204, {})
                else:
                    claim = hub.store.get(run_id, key)
                    if claim is None:
                        hub.calls.append((action, key, 404))
                        return self._answer(404, {"error": {"code": "claim_not_found", "message": "no"}})
                    code = 200
                hub.calls.append((action, key, code))
                self._answer(code, {"key": key, "run_id": run_id, "state": claim.state, "yours": claim.held_by(worker),
                                    "envelope": claim.envelope})

            def do_POST(self):  # noqa: N802
                self._claim(self.path.rsplit("/", 1)[1])

            def do_GET(self):  # noqa: N802
                self._claim("get")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hub():
    track = InMemoryTrackStore()
    stand_in = _Hub(track)
    yield stand_in, track
    stand_in.close()


def _stderr(track, run_id):
    [started] = [e.payload for e in track.replay(run_id) if e.event_type == "worker_started"]
    return (Path(started["logs"]) / "stderr.log").read_text()


def test_a_local_worker_reserves_and_commits_its_key_over_http_with_the_token_the_executor_minted(tmp_path, hub):
    stand_in, track = hub
    executor = local_executor(tmp_path, hub_url=stand_in.url)
    runner = LifecycleRunner(executor=executor, track=track)
    assert runner.submit(OpDefinition("r", (OpStep("s", "echo", "run", {"n": 1}),))) is RunState.COMPLETED
    claim = stand_in.store.get("r", "r:s:0")
    assert claim is not None and claim.state == "committed" and claim.envelope["payload"] == {"echo": {"n": 1}}
    [started] = [e.payload for e in track.replay("r") if e.event_type == "worker_started"]
    assert claim.worker == started["run_token_sha256"]  # reserved with the token the executor minted
    assert [c for c in stand_in.calls] == [("reserve", "r:s:0", 201), ("commit", "r:s:0", 200)]
    log = _stderr(track, "r")
    assert "claim r:s:0: reserved" in log and "claim r:s:0: committed" in log


def test_a_step_named_with_a_slash_is_claimed_like_any_other(tmp_path, hub):
    # The key travels in the request body, never the path: a step name is any text.
    stand_in, track = hub
    runner = LifecycleRunner(executor=local_executor(tmp_path, hub_url=stand_in.url), track=track)
    assert runner.submit(OpDefinition("r", (OpStep("fetch/data:1", "echo", "run", {"n": 1}),))) is RunState.COMPLETED
    key = "r:fetch/data%3A1:0"
    assert stand_in.store.get("r", key).state == "committed"
    assert [c for c in stand_in.calls] == [("reserve", key, 201), ("commit", key, 200)]


def test_a_replay_of_a_committed_key_is_answered_from_the_claim_and_the_worker_acts_on_nothing(tmp_path, hub):
    stand_in, track = hub
    executor = local_executor(tmp_path, hub_url=stand_in.url)
    runner = LifecycleRunner(executor=executor, track=track)
    runner.submit(OpDefinition("r", (OpStep("s", "echo", "run", {"n": 1}),)))
    # The same attempt invoked again, by a new worker, as a retry or a resume would: the envelope comes back.
    worker = executor.materialize("echo", "r", "s:0")
    try:
        runner._append("r", "worker_started", {"step": "s", "attempt": 0, "instance": "s:0",
                                                **worker.details, "run_token_sha256": worker.run_token_digest})
        envelope = worker.interact("run", {"n": 2}, idempotency_key="r:s:0")
    finally:
        executor.teardown(worker)
    assert envelope.ok and envelope.payload == {"echo": {"n": 1}}  # the first invocation's, not this input's
    assert stand_in.calls[-1] == ("reserve", "r:s:0", 200)
    assert "committed already; answering with its envelope" in (Path(worker.details["logs"]) / "stderr.log").read_text()


def test_a_key_reserved_by_a_lost_worker_fails_the_replay_outcome_unknown(tmp_path, hub):
    stand_in, track = hub
    executor = local_executor(tmp_path, hub_url=stand_in.url)
    runner = LifecycleRunner(executor=executor, track=track)
    # A worker that reserved and was lost: its reservation stands under its own token, now expired.
    runner._append("r", "op_submitted", {"op": {"run_id": "r", "steps": []}})
    runner._append("r", "run_picked_up", {})
    lost = run_tokens.mint()
    runner._append("r", "worker_started", {"step": "s", "attempt": 0, "instance": "lost",
                                            "run_token_sha256": run_tokens.digest(lost)})
    stand_in.store.reserve("r", "r:s:0", worker=run_tokens.digest(lost))
    runner._append("r", "worker_stopped", {"step": "s", "attempt": 0, "instance": "lost"})
    worker = executor.materialize("echo", "r", "s:0")
    try:
        runner._append("r", "worker_started", {"step": "s", "attempt": 0, "instance": "s:0",
                                                **worker.details, "run_token_sha256": worker.run_token_digest})
        envelope = worker.interact("run", {"n": 1}, idempotency_key="r:s:0")
    finally:
        executor.teardown(worker)
    assert not envelope.ok and envelope.error.code == "outcome-unknown"
    assert stand_in.store.get("r", "r:s:0").state == "outcome_unknown"


def test_a_worker_given_no_hub_claims_nothing(tmp_path, hub):
    stand_in, track = hub
    runner = LifecycleRunner(executor=local_executor(tmp_path), track=track)
    assert runner.submit(OpDefinition("r", (OpStep("s", "echo", "run", {"n": 1}),))) is RunState.COMPLETED
    assert stand_in.calls == [] and stand_in.store.get("r", "r:s:0") is None
    assert "claim" not in _stderr(track, "r")


def test_the_hub_url_reaches_a_local_and_a_remote_workers_environment(tmp_path):
    executor = local_executor(tmp_path, hub_url="http://hub.example/")
    worker = executor.materialize("env", "r", "s:0")
    try:
        env = worker.interact("run").payload["env"]
    finally:
        executor.teardown(worker)
    assert env["COLLAB_HUB_URL"] == "http://hub.example"
    remote = KubernetesCogExecutor(runner_image="img", insecure_skip_network_policy=True, api=object(),
                                   hub_url="http://collab-hub-api.collab-hub.svc/")
    env = {e["name"]: e["value"] for e in remote._environment("hermes", "r")}
    assert env["COLLAB_HUB_URL"] == "http://collab-hub-api.collab-hub.svc"
    without = KubernetesCogExecutor(runner_image="img", insecure_skip_network_policy=True, api=object())
    assert "COLLAB_HUB_URL" not in {e["name"] for e in without._environment("hermes", "r")}


def test_the_controller_takes_the_hub_url_as_an_option_or_from_its_variable(monkeypatch, tmp_path):
    base = ["--track", str(tmp_path / "t.sqlite"), "--work-dir", str(tmp_path / "runs"), "--packages", "/a"]
    assert parse_args(base).hub_url is None
    monkeypatch.setenv("COLLAB_CONTROLLER_HUB_URL", "http://127.0.0.1:8000")
    assert parse_args(base).hub_url == "http://127.0.0.1:8000"
    assert parse_args([*base, "--hub-url", "http://hub:80"]).hub_url == "http://hub:80"
    monkeypatch.delenv("COLLAB_CONTROLLER_HUB_URL")
    assert os.environ.get("COLLAB_CONTROLLER_HUB_URL") is None
