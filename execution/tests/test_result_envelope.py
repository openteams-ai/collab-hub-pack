"""The result envelope at the worker seam: parsing, and what the engine does with it."""

import httpx
import pytest

from collab_hub_execution import (
    ERROR_CODES,
    EnvelopeError,
    EnvelopeInvalid,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    Problem,
    ResultEnvelope,
    RunBudget,
    RunState,
    intents,
)
from collab_hub_execution.envelope import CODE_FOR_STATUS, STATUS_FOR_CODE
from collab_hub_execution.kubernetes import _KubernetesWorker


class LocalExecutor:
    def __init__(self, worker):
        self.worker = worker

    def materialize(self, cog, run_id, instance=""):
        return self.worker

    def teardown(self, worker):
        pass


def op(run_id="env", count=1):
    return OpDefinition(run_id, tuple(OpStep(f"s{i}", "c", "run", "draft") for i in range(count)))


def events(track, run_id, kind):
    return [e for e in track.replay(run_id) if e.event_type == kind]


# --- parsing -----------------------------------------------------------------


def test_minimal_envelope_parses_with_defaults():
    envelope = ResultEnvelope.parse({"envelope": 1, "ok": True})
    assert envelope == ResultEnvelope.success()
    assert envelope.payload is None and envelope.problems == () and envelope.usage is None


def test_unknown_fields_are_ignored_at_every_level():
    envelope = ResultEnvelope.parse({
        "envelope": 1, "ok": True, "payload": {"x": 1}, "future_field": "ignored",
        "problems": [{"check": "schema", "detail": "d", "severity": "warn", "extra": True}],
        "usage": {"tokens": 3, "vendor_specific": 9},
    })
    assert envelope.payload == {"x": 1}
    assert envelope.problems == (Problem("schema", "d", "warn"),)
    assert envelope.usage == {"tokens": 3, "vendor_specific": 9}  # the engine picks tokens/cost out


@pytest.mark.parametrize("data", [
    {"ok": True},                              # no version
    {"envelope": 2, "ok": True},               # a version this hub does not read
    {"envelope": True, "ok": True},            # a bool is not a version
    {"envelope": "1", "ok": True},
    {"envelope": 1},                           # ok missing
    {"envelope": 1, "ok": "yes"},
    {"envelope": 1, "ok": False},              # a failure with no reason
    {"envelope": 1, "ok": False, "error": {"detail": "no code"}},
    {"envelope": 1, "ok": False, "error": {"code": "invalid-input"}},  # no detail
    {"envelope": 1, "ok": False, "error": {"code": "invalid-input", "detail": None}},
    {"envelope": 1, "ok": False, "error": {"code": "made-up", "detail": "x"}},  # outside the closed set
    {"envelope": 1, "ok": True, "error": {"code": "invalid-input", "detail": "x"}},  # a contradiction
    {"envelope": 1, "ok": True, "problems": {"check": "schema"}},
    {"envelope": 1, "ok": True, "problems": [{"detail": "no check"}]},
    {"envelope": 1, "ok": True, "problems": [{"check": "schema", "severity": "fatal"}]},
    {"envelope": 1, "ok": True, "binding": "not-an-object"},
    "not an object",
    None,
])
def test_non_envelopes_are_refused(data):
    with pytest.raises(EnvelopeInvalid):
        ResultEnvelope.parse(data)


@pytest.mark.parametrize("build", [
    lambda: ResultEnvelope(ok=False),                                              # a failure with no reason
    lambda: ResultEnvelope(ok=True, error=EnvelopeError("invalid-input", "x")),   # a contradiction
    lambda: ResultEnvelope(ok=True, envelope=2),
    lambda: ResultEnvelope(ok="yes"),
    lambda: ResultEnvelope(ok=True, problems=(object(),)),
    lambda: ResultEnvelope(ok=True, binding="not-an-object"),
    lambda: ResultEnvelope.failure("made-up", "x"),
    lambda: EnvelopeError("invalid-input", None),
    lambda: Problem("schema", "d", "fatal"),
    lambda: Problem("", "d"),
])
def test_a_directly_built_envelope_obeys_the_same_invariants_as_a_parsed_one(build):
    with pytest.raises(EnvelopeInvalid):
        build()


def test_a_worker_that_builds_an_invalid_envelope_fails_durably_not_crashing_the_engine():
    class Worker:
        def interact(self, entry_point, input=None, idempotency_key=None):
            return ResultEnvelope(ok=False)  # would have reached error.code with error None

    track = InMemoryTrackStore()
    assert LifecycleRunner(executor=LocalExecutor(Worker()), track=track).submit(op()) is RunState.FAILED
    failed = events(track, "env", "failed")[-1].payload
    assert failed["error"] == "EnvelopeInvalid" and "ok: false requires error" in failed["reason"]


def test_to_dict_round_trips_through_parse():
    envelope = ResultEnvelope.failure(
        "model-call-failed", "upstream 500", usage={"tokens": 5},
        problems=[Problem("identity", "model echoed another id", "warn")],
        binding={"model": "m1"}, raw="...", cog={"id": "openteams/x", "version": "0.1.0"}, task="ask",
        timing={"latency_s": 0.2},
    )
    assert ResultEnvelope.parse(envelope.to_dict()) == envelope
    assert envelope.to_dict()["error"] == {"code": "model-call-failed", "detail": "upstream 500"}


def test_every_documented_code_has_a_status_and_every_status_a_code():
    assert set(STATUS_FOR_CODE) == ERROR_CODES
    # A bare status names a code only where the status alone says enough: never 409, whose code, outcome-unknown,
    # no retry runs again on its own, so only an envelope that says so records it.
    assert set(CODE_FOR_STATUS) == set(STATUS_FOR_CODE.values()) - {409}
    assert STATUS_FOR_CODE["outcome-unknown"] == 409


def test_the_hub_and_the_worker_sdk_write_and_read_one_status_table():
    # The hub never imports the SDK (test_worker_sdk_boundary.py); the two tables are held equal here instead.
    import importlib
    import sys
    from pathlib import Path

    sdk = Path(__file__).resolve().parents[2] / "worker" / "src"
    sys.path.insert(0, str(sdk))
    try:
        written = importlib.import_module("collab_hub_cog_worker.envelope")
    finally:
        sys.path.remove(str(sdk))
    assert written.STATUS_FOR_CODE == STATUS_FOR_CODE and written.ERROR_CODES == ERROR_CODES


# --- the engine's reading ----------------------------------------------------


def test_ok_with_warnings_completes_the_step_and_records_them():
    # Under the default Gate a `warn` problem passes with problems; an `error` one escalates (test_gates.py).
    problems = [Problem("grounding", "quote not verbatim", "warn")]
    executor = InMemoryCogExecutor({
        "c": lambda e, v: ResultEnvelope.success(
            {"answer": v}, usage={"tokens": 1}, problems=problems, binding={"model": "m1"},
        ),
    })
    track = InMemoryTrackStore()
    assert LifecycleRunner(executor=executor, track=track).submit(op()) is RunState.COMPLETED
    (completed,) = events(track, "env", "step_completed")
    assert completed.payload["payload"] == {"answer": "draft"}
    assert completed.payload["problems"] == [
        {"check": "grounding", "detail": "quote not verbatim", "severity": "warn"},
    ]
    assert completed.payload["binding"] == {"model": "m1"}
    assert not events(track, "env", "failed")


def test_a_clean_envelope_records_no_problems_key():
    track = InMemoryTrackStore()
    executor = InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v)})
    LifecycleRunner(executor=executor, track=track).submit(op())
    (completed,) = events(track, "env", "step_completed")
    assert "problems" not in completed.payload and "binding" not in completed.payload


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_each_error_code_fails_the_step_and_keeps_the_code(code):
    attempts = {"n": 0}

    def handler(entry, value):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ResultEnvelope.failure(code, f"{code} happened", usage={"tokens": 7})
        return ResultEnvelope.success("recovered", usage={"tokens": 1})

    executor = InMemoryCogExecutor({"c": handler})
    track = InMemoryTrackStore()
    engine = LifecycleRunner(executor=executor, track=track, budget=RunBudget(max_tokens=100))
    assert engine.submit(op()) is RunState.FAILED
    (failed,) = events(track, "env", "failed")
    assert failed.payload["error"] == code
    assert failed.payload["reason"] == f"{code} happened"
    assert failed.payload["step"] == "s0"
    # the failed call's spending is on the Track, and counts on retry
    assert events(track, "env", "interaction_usage")[-1].payload["usage"] == {"tokens": 7}
    if code == "outcome-unknown":
        # The one failure a retry never runs again on its own: a person settles what the lost worker did first.
        with pytest.raises(ValueError, match="reconciled explicitly"):
            engine.retry("env")
        intents.reconcile_outcome(track, "env", step="s0", actor="ops")
    assert engine.retry("env") is RunState.COMPLETED
    assert engine.budget_tracker(track.replay("env")).tokens == 8


def test_an_error_envelope_with_problems_records_them_on_the_failure():
    executor = InMemoryCogExecutor({
        "c": lambda e, v: ResultEnvelope.failure("invalid-input", "bad", problems=[Problem("input", "x")]),
    })
    track = InMemoryTrackStore()
    assert LifecycleRunner(executor=executor, track=track).submit(op()) is RunState.FAILED
    (failed,) = events(track, "env", "failed")
    assert failed.payload["problems"] == [{"check": "input", "detail": "x", "severity": "error"}]


def test_missing_usage_under_a_budget_fails_even_in_a_valid_envelope():
    executor = InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v)})
    track = InMemoryTrackStore()
    engine = LifecycleRunner(executor=executor, track=track, budget=RunBudget(max_tokens=10))
    assert engine.submit(op()) is RunState.FAILED
    assert events(track, "env", "failed")[-1].payload["error"] == "UsageUnavailable"


def test_in_memory_handler_may_return_an_envelope_shaped_mapping_or_a_raw_value():
    executor = InMemoryCogExecutor({
        "c": lambda e, v: {"envelope": 1, "ok": True, "payload": v,
                           "problems": [{"check": "schema", "detail": "d", "severity": "warn"}]},
        "raw": lambda e, v: {"answer": v},  # no envelope key: a plain payload
    })
    track = InMemoryTrackStore()
    definition = OpDefinition("mix", (OpStep("a", "c", "run", "x"), OpStep("b", "raw", "run", "y")))
    assert LifecycleRunner(executor=executor, track=track).submit(definition) is RunState.COMPLETED
    first, second = events(track, "mix", "step_completed")
    assert first.payload["payload"] == "x" and first.payload["problems"][0]["check"] == "schema"
    assert second.payload["payload"] == {"answer": "y"} and "problems" not in second.payload


def test_a_worker_returning_something_else_fails_durably_as_envelope_invalid():
    class Worker:
        def interact(self, entry_point, input=None, idempotency_key=None):
            return {"output": "done"}  # the pre-envelope shape

    track = InMemoryTrackStore()
    assert LifecycleRunner(executor=LocalExecutor(Worker()), track=track).submit(op()) is RunState.FAILED
    failed = events(track, "env", "failed")[-1].payload
    assert failed["error"] == "EnvelopeInvalid"
    assert failed["reason"] == "interact() must return a ResultEnvelope"
    assert events(track, "env", "interaction_usage")[-1].payload["usage"] is None


# --- over HTTP ---------------------------------------------------------------


def http_worker(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return _KubernetesWorker("c", "w", "http://worker", client)


def test_http_200_envelope_is_parsed():
    worker = http_worker(lambda _: httpx.Response(200, json={
        "envelope": 1, "ok": True, "payload": {"a": 1}, "usage": {"tokens": 2}, "unknown": "ignored",
    }))
    assert worker.interact("run", "x") == ResultEnvelope.success({"a": 1}, usage={"tokens": 2})


@pytest.mark.parametrize("body", [{"output": "done"}, {"envelope": 1, "ok": False}, [], "text"])
def test_http_200_without_an_envelope_fails_the_step_as_envelope_invalid(body):
    worker = http_worker(lambda _: httpx.Response(200, json=body))
    track = InMemoryTrackStore()
    assert LifecycleRunner(executor=LocalExecutor(worker), track=track).submit(op()) is RunState.FAILED
    assert events(track, "env", "failed")[-1].payload["error"] == "EnvelopeInvalid"


@pytest.mark.parametrize("status,code", [
    (422, "invalid-input"), (502, "model-call-failed"), (502, "model-response-malformed"),
    (503, "model-unavailable"), (503, "binding-invalid"),
])
def test_http_error_statuses_carry_an_error_envelope_whose_code_is_kept(status, code):
    worker = http_worker(lambda _: httpx.Response(status, json={
        "envelope": 1, "ok": False, "error": {"code": code, "detail": "why"}, "usage": {"tokens": 4},
    }))
    envelope = worker.interact("run", "x")
    assert not envelope.ok and envelope.error.code == code and envelope.usage == {"tokens": 4}
    track = InMemoryTrackStore()
    assert LifecycleRunner(executor=LocalExecutor(worker), track=track).submit(op()) is RunState.FAILED
    assert events(track, "env", "failed")[-1].payload["error"] == code


@pytest.mark.parametrize("status", sorted(CODE_FOR_STATUS))
def test_http_error_status_without_an_envelope_body_still_names_the_failure(status):
    worker = http_worker(lambda _: httpx.Response(status, text="<html>gateway error</html>"))
    envelope = worker.interact("run", "x")
    assert not envelope.ok and envelope.error.code == CODE_FOR_STATUS[status]
    assert envelope.usage is None  # unknown, so a budgeted run fails as UsageUnavailable


def test_a_bare_409_fails_the_step_as_a_transport_error_and_only_an_envelope_records_outcome_unknown():
    # A worker that is not on the SDK, or a proxy, may answer 409 for reasons of its own: without an envelope
    # saying outcome-unknown, the step fails by the exception's name, and a retry runs it again.
    worker = http_worker(lambda _: httpx.Response(409, text="conflict"))
    with pytest.raises(httpx.HTTPStatusError):
        worker.interact("run", "x")
    said_so = http_worker(lambda _: httpx.Response(409, json=ResultEnvelope.failure(
        "outcome-unknown", "reserved by a lost worker").to_dict()))
    envelope = said_so.interact("run", "x")
    assert not envelope.ok and envelope.error.code == "outcome-unknown"


def test_http_error_status_with_an_ok_envelope_is_a_contradiction():
    worker = http_worker(lambda _: httpx.Response(422, json={"envelope": 1, "ok": True, "payload": "?"}))
    with pytest.raises(EnvelopeInvalid):
        worker.interact("run", "x")


@pytest.mark.parametrize("status", [404, 500, 201])
def test_other_statuses_are_not_envelopes(status):
    worker = http_worker(lambda _: httpx.Response(status, json={"envelope": 1, "ok": True}))
    with pytest.raises((httpx.HTTPStatusError, EnvelopeInvalid)):
        worker.interact("run", "x")


def test_http_envelope_with_an_unknown_pause_field_is_an_envelope_not_a_pause():
    worker = http_worker(lambda _: httpx.Response(200, json={"envelope": 1, "ok": True, "payload": "p", "pause": True}))
    assert worker.interact("run", "x") == ResultEnvelope.success("p")


def test_an_http_pause_answer_is_not_an_envelope_so_a_cog_cannot_pause_a_run():
    answer = {"pause": True, "reason": "review", "usage": {"tokens": 0}}
    worker = http_worker(lambda _: httpx.Response(200, json=answer))
    with pytest.raises(EnvelopeInvalid):
        worker.interact("run", "x")
