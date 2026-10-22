"""The seam as the SDK serves it: /healthz, /invoke, /turn, the run token, and what a Worker's entry points get."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import fake_agent_command

from collab_hub_cog_worker import AcpHarnessAdapter, Context, Harness, OpenAICompatAdapter, Task, Worker, WorkerError

TOKEN = "run-token"


def _worker(environ=None, **kwargs) -> Worker:
    environ = {"COLLAB_RUN_TOKEN": TOKEN, "COLLAB_COG_ID": "ask-me", "COLLAB_RUN_ID": "r-1", **(environ or {})}
    worker = Worker("ask-me", "0.3.0", environ=environ,
                    harness=kwargs.pop("harness", lambda context: AcpHarnessAdapter(fake_agent_command())), **kwargs)

    @worker.task("ask")
    def ask(task: Task, harness: Harness):
        if not isinstance(task.input, dict) or "prompt" not in task.input:
            raise WorkerError("invalid-input", "ask takes {prompt}")
        harness.problem("length", "short answer", "warn")
        return {"answer": harness.interact(str(task.input["prompt"])).text, "frames": [f.id for f in task.frames]}

    @worker.task("plain")
    def plain(task: Task, harness: Harness):
        return harness.interact("plain")

    worker.session("session")
    return worker


@pytest.fixture
def served():
    worker = _worker()
    server = worker.server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield worker, f"http://127.0.0.1:{server.server_address[1]}"
    worker.stop()
    server.shutdown()


def _post(url, path, body, token=TOKEN):
    request = urllib.request.Request(f"{url}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_entry_points_a_worker_declares_are_served_as_the_seam(served):
    worker, url = served
    with urllib.request.urlopen(f"{url}/healthz") as health:
        assert health.status == 200 and json.loads(health.read()) == {"ok": True, "cog": "ask-me"}
    status, answer = _post(url, "/invoke", {"entry_point": "ask", "input": {"prompt": "hi"}, "idempotency_key": "k",
                                            "frames": [{"id": "f1", "text": "Be brief."}]})
    assert status == 200 and answer["ok"] and answer["envelope"] == 1
    assert answer["cog"] == {"id": "ask-me", "version": "0.3.0"} and answer["task"] == "ask"
    assert answer["payload"]["answer"].startswith("agent heard: hi") and "Be brief." in answer["payload"]["answer"]
    assert answer["payload"]["frames"] == ["f1"]
    assert answer["problems"] == [{"check": "length", "detail": "short answer", "severity": "warn"}]
    assert answer["usage"]["tokens"] == 10 and answer["timing"]["latency_s"] >= 0 and answer["raw"]
    assert answer["binding"] is None  # nothing was delivered to this worker


def test_an_interaction_returned_from_an_entry_point_is_its_answer(served):
    _, url = served
    status, answer = _post(url, "/invoke", {"entry_point": "plain"})
    assert status == 200 and answer["payload"] == {"answer": "agent heard: plain"}


def test_without_the_run_token_nothing_is_answered(served):
    _, url = served
    assert _post(url, "/invoke", {"entry_point": "ask"}, token="wrong")[0] == 401
    assert _post(url, "/turn", {"text": "hi"}, token="")[0] == 401


def test_an_unknown_entry_point_and_a_bad_input_are_invalid_input(served):
    _, url = served
    status, answer = _post(url, "/invoke", {"entry_point": "nope"})
    assert status == 422 and answer["error"]["code"] == "invalid-input"
    assert "ask, plain, session" in answer["error"]["detail"]
    status, answer = _post(url, "/invoke", {"entry_point": "ask", "input": "not an object"})
    assert status == 422 and answer["error"] == {"code": "invalid-input", "detail": "ask takes {prompt}"}
    assert answer["ok"] is False and answer["payload"] is None


def test_a_failure_of_the_harness_is_answered_with_its_codes_status(served):
    _, url = served
    status, answer = _post(url, "/invoke", {"entry_point": "ask", "input": {"prompt": "fail"}})
    assert status == 502 and answer["error"]["code"] == "model-call-failed"
    status, answer = _post(url, "/invoke", {"entry_point": "ask", "input": {"prompt": "overflow"}})
    assert status == 422 and answer["error"]["code"] == "context-exhausted"


def test_a_session_answers_turns_until_a_farewell_and_completes_with_the_count(served):
    worker, url = served
    outcome = []
    opening = threading.Thread(target=lambda: outcome.append(_post(url, "/invoke", {"entry_point": "session"})))
    opening.start()
    # A turn asked before the session is open waits for it rather than being refused.
    assert _post(url, "/turn", {"text": "first"}) == (200, {"text": "agent heard: first"})
    assert _post(url, "/turn", {"text": "second"})[1]["text"] == "agent heard: second"
    assert _post(url, "/turn", {"text": "bye"})[1]["text"].startswith("Bye!")
    opening.join(timeout=10)
    [(status, answer)] = outcome
    assert status == 200 and answer["payload"] == {"turns": 2} and answer["usage"]["interactions"] == 2
    assert _post(url, "/turn", {"text": "more"}) == (404, {"error": "no session is open"})


def test_a_session_whose_harness_exits_ends_failed(served):
    worker, url = served
    outcome = []
    opening = threading.Thread(target=lambda: outcome.append(_post(url, "/invoke", {"entry_point": "session"})))
    opening.start()
    status, body = _post(url, "/turn", {"text": "exit"})
    assert status == 502 and "exited" in body["error"]
    opening.join(timeout=10)
    [(status, answer)] = outcome
    assert status == 502 and answer["error"]["code"] == "model-call-failed"
    assert "exited (code 3) during the session, after 1 turns" in answer["error"]["detail"]


def test_a_stop_closes_every_harness_including_one_still_starting():
    worker = _worker(harness=lambda context: AcpHarnessAdapter(fake_agent_command("--slow-start")))
    outcome = []
    opening = threading.Thread(target=lambda: outcome.append(worker.invoke("session")))
    opening.start()
    deadline = time.monotonic() + 5
    while not worker._live and time.monotonic() < deadline:
        time.sleep(0.02)
    [harness] = list(worker._live)
    worker.stop()
    opening.join(timeout=15)
    assert not opening.is_alive() and not harness.alive() and not worker._live


def test_the_authentication_hook_replaces_the_run_token_check():
    worker = _worker(authenticate=lambda headers: headers.get("X-Secret") == "open sesame")
    server = worker.server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _post(url, "/invoke", {"entry_point": "plain"})[0] == 401
        request = urllib.request.Request(f"{url}/invoke", data=b'{"entry_point": "plain"}', method="POST",
                                         headers={"X-Secret": "open sesame"})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200
    finally:
        worker.stop()
        server.shutdown()


def test_the_binding_delivered_to_the_worker_reaches_its_harness_and_the_envelope(fake_model_url):
    environ = {"COLLAB_RUN_TOKEN": TOKEN, "COLLAB_MODEL_BASE_URL": fake_model_url, "COLLAB_MODEL_NAME": "fake-model",
               "COLLAB_MODEL_API_KEY": "k", "COLLAB_MODEL_CONTEXT_WINDOW": "4000"}
    worker = Worker("plain-cog", environ=environ, harness=lambda context: OpenAICompatAdapter())

    @worker.task("ask")
    def ask(task, harness):
        return harness.interact(task.input["prompt"])

    status, answer = worker.invoke("ask", {"prompt": "hello"})
    assert status == 200 and answer["payload"] == {"answer": "The fake model heard: hello"}
    assert answer["binding"] == {"provider": "openai-compatible", "endpoint": fake_model_url, "model": "fake-model",
                                 "context_window": 4000, "max_output_tokens": None, "resolved_from": "environment"}
    assert answer["usage"]["prompt_tokens"] == 10


def test_a_binding_that_cannot_be_read_fails_the_step_as_binding_invalid():
    worker = Worker("c", environ={"COLLAB_RUN_TOKEN": TOKEN, "COLLAB_MODEL_CONTEXT_WINDOW": "lots"},
                    harness=lambda context: OpenAICompatAdapter())
    worker.task("ask")(lambda task, harness: harness.interact("x"))
    status, answer = worker.invoke("ask")
    assert status == 503 and answer["error"]["code"] == "binding-invalid"
    assert "COLLAB_MODEL_CONTEXT_WINDOW" in answer["error"]["detail"]


def test_a_worker_without_a_harness_serves_entry_points_that_need_none():
    worker = Worker("hello", environ={"COLLAB_RUN_TOKEN": TOKEN})
    worker.task("run")(lambda task, harness: {"greeting": f"Hello, {(task.input or {}).get('name', 'world')}!"})
    assert worker.invoke("run", {"name": "Ada"}) == (200, worker.invoke("run", {"name": "Ada"})[1])
    assert worker.invoke("run", {"name": "Ada"})[1]["payload"] == {"greeting": "Hello, Ada!"}
    assert Context(binding=worker.binding()).binding.bound is False
