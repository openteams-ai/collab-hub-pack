"""The Track conformance suite: every store passes it, and a pre-v1 Track still reads.

Append, replay from a sequence, a live stream, status derivation, payloads by
reference, one submission per run, and the reader that lifts events written
before schema version 1 — the same assertions against the in-memory, SQLite and
Postgres stores. Postgres needs ``TEST_POSTGRES_URL``, as the adapter tests do.
"""

import gc
import json
import os
import sqlite3
import threading
import warnings
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from collab_hub_execution import (
    SCHEMA_VERSION,
    InMemoryTrackStore,
    OneSubmissionPerRun,
    PostgresTrackStore,
    RunState,
    SqliteTrackStore,
    TrackEvent,
    derive_run_status,
    upgrade,
)

TEST_PG = os.environ.get("TEST_POSTGRES_URL")


_NEEDS_PG = pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run the suite against Postgres")


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres", marks=_NEEDS_PG),
                        # The hub's shared pool returns mappings, not tuples (frames/db.py).
                        pytest.param("postgres-dict-rows", marks=_NEEDS_PG)])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryTrackStore()
    elif request.param == "sqlite":
        path = tmp_path / "track.sqlite"
        SqliteTrackStore.ensure_schema(path)
        yield SqliteTrackStore(path)
    else:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        kwargs = {"row_factory": dict_row} if request.param == "postgres-dict-rows" else {}
        pool = ConnectionPool(TEST_PG, min_size=1, max_size=4, open=True, kwargs=kwargs)
        with pool.connection() as conn:
            for statement in ("DROP TABLE IF EXISTS collab_track_payloads", "DROP TABLE IF EXISTS collab_track_events",
                              "DROP SEQUENCE IF EXISTS collab_track_event_sequence"):
                conn.execute(statement)
            PostgresTrackStore.ensure_schema(conn)
        yield PostgresTrackStore(pool)
        pool.close()


def _event(kind, run_id="r", **payload):
    return TrackEvent(run_id=run_id, event_type=kind, payload=payload, schema=SCHEMA_VERSION)


# --- append, replay, stream --------------------------------------------------------------


def test_append_assigns_increasing_sequences_and_replay_keeps_them(store):
    first = store.append(_event("op_submitted", op={"steps": []}))
    second = store.append(_event("run_picked_up"))
    other = store.append(_event("op_submitted", run_id="other"))
    third = store.append(_event("completed"))
    assert first.sequence < second.sequence < other.sequence < third.sequence
    assert [e.event_id for e in store.replay("r")] == [first.event_id, second.event_id, third.event_id]
    assert [e.event_type for e in store.replay("r", after_sequence=second.sequence)] == ["completed"]
    assert store.replay("nobody") == ()


def test_a_replayed_event_is_the_event_that_was_appended(store):
    when = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    stored = store.append(TrackEvent(run_id="r", event_type="op_submitted", payload={"op": {"n": 1, "t": [1, 2]}},
                                     occurred_at=when, event_id="evt-1", schema=SCHEMA_VERSION))
    [read] = store.replay("r")
    assert read == stored
    assert read.payload == {"op": {"n": 1, "t": [1, 2]}} and read.occurred_at == when and read.schema == SCHEMA_VERSION


def test_the_store_assigns_sequences_and_refuses_a_second_copy_of_an_event(store):
    with pytest.raises(ValueError):
        store.append(TrackEvent(run_id="r", event_type="op_submitted", sequence=7))
    first = store.append(_event("op_submitted"))
    with pytest.raises(Exception):  # the same event id twice
        store.append(TrackEvent(run_id="r", event_type="run_picked_up", event_id=first.event_id))


def test_a_run_is_submitted_once(store):
    store.append(_event("op_submitted"))
    with pytest.raises(OneSubmissionPerRun):
        store.append(_event("op_submitted"))
    store.append(_event("op_submitted", run_id="another"))  # a different run is fine


def test_a_live_stream_delivers_events_appended_while_it_waits(store):
    store.append(_event("op_submitted"))

    def later():
        store.append(_event("run_picked_up"))
        store.append(_event("completed"))

    timer = threading.Timer(0.2, later)
    timer.start()
    try:
        seen = []
        for event in store.stream("r", timeout_seconds=3):
            seen.append(event.event_type)
            if event.event_type == "completed":
                break
    finally:
        timer.join()
    assert seen == ["op_submitted", "run_picked_up", "completed"]


def test_a_stream_with_no_timeout_returns_what_is_there(store):
    store.append(_event("op_submitted"))
    assert [e.event_type for e in store.stream("r")] == ["op_submitted"]


def test_the_runs_on_a_track_are_listed_in_the_order_they_were_submitted(store):
    assert store.run_ids() == ()
    store.append(_event("op_submitted", run_id="b"))
    store.append(_event("op_submitted", run_id="a"))
    store.append(_event("run_picked_up", run_id="b"))
    store.append(_event("step_started", run_id="orphan"))  # never submitted: not a run
    # Submitted before schema v1, where the event was `submitted`: listed too, once, in its place.
    store.append(TrackEvent(run_id="old", event_type="submitted", payload={}, schema=0))
    store.append(TrackEvent(run_id="old", event_type="step_started", payload={"step": "s"}, schema=0))
    assert store.run_ids() == ("b", "a", "old")


# --- status from the Track ----------------------------------------------------------------


def test_status_is_the_track_replayed_through_the_run_machine(store):
    assert derive_run_status(store.replay("r")) is None
    store.append(_event("op_submitted", op={"steps": []}))
    assert derive_run_status(store.replay("r")) is RunState.SUBMITTED
    store.append(_event("run_picked_up"))
    store.append(_event("gate_escalated", step="s", reason="review", escalation="esc-1"))
    assert derive_run_status(store.replay("r")) is RunState.WAITING_AT_GATE
    store.append(_event("gate_decided", step="s", outcome="approve", escalation="esc-1", actor="alice"))
    store.append(_event("step_completed", step="s", attempt=0, payload={"ok": True}))
    store.append(_event("completed"))
    assert derive_run_status(store.replay("r")) is RunState.COMPLETED


# --- payloads by reference ----------------------------------------------------------------


def test_a_payload_kept_by_reference_comes_back_as_it_was(store):
    payload = {"answer": "x" * 100, "items": [1, 2, {"deep": None}]}
    store.put_payload("org/team-42", "org%2Fteam-42:s:0", json.dumps(payload))
    assert store.get_payload("org%2Fteam-42:s:0") == payload
    # The same reference is rewritten, not added: recovering an attempt leaves one row.
    store.put_payload("org/team-42", "org%2Fteam-42:s:0", json.dumps({"answer": "y"}))
    assert store.get_payload("org%2Fteam-42:s:0") == {"answer": "y"}
    with pytest.raises(KeyError):
        store.get_payload("org%2Fteam-42:s:1")


def test_an_escalated_payload_s_digest_can_be_recomputed_from_the_kept_row(store):
    # Postgres keeps the row as jsonb, which reorders keys: the digest is over canonical JSON.
    import hashlib

    from collab_hub_execution import Gate, InMemoryCogExecutor, LifecycleRunner, OpDefinition, OpStep

    result = {"zeta": "x" * 200, "alpha": {"b": 1, "a": [2.5, None]}, "mid": "é"}
    engine = LifecycleRunner(executor=InMemoryCogExecutor({"c": lambda e, v: v}), track=store,
                                   payload_inline_max_bytes=100)
    engine.submit(OpDefinition("r", (OpStep("s", "c", "run", result, gate=Gate(escalate="always")),)))
    [escalated] = [e for e in store.replay("r") if e.event_type == "gate_escalated"]
    kept = store.get_payload(escalated.payload["payload_ref"])
    canonical = json.dumps(kept, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == escalated.payload["envelope"]["payload_digest"]


# --- a Track written before schema version 1 ----------------------------------------------


PRE_V1 = [
    ("submitted", {}),
    ("step_started", {"step": "draft", "cog": "writer", "digest": None, "attempt": 0}),
    ("materialized", {"cog": "writer", "digest": None}),
    ("ready", {"cog": "writer"}),
    ("interaction_started", {"step": "draft", "entry_point": "write"}),
    ("interaction_usage", {"step": "draft", "attempt": 0, "usage": {"tokens": 6}}),
    ("idle", {"step": "draft"}),
    ("teardown_started", {"step": "draft"}),
    ("paused", {"step": "draft", "reason": "writer awaiting approval"}),
    ("signal_received", {"step": "draft", "value": {"approved": True}}),
    ("step_started", {"step": "draft", "cog": "writer", "digest": None, "attempt": 1}),
    ("materialized", {"cog": "writer", "digest": None}),
    ("ready", {"cog": "writer"}),
    ("interaction_started", {"step": "draft", "entry_point": "write"}),
    ("interaction_usage", {"step": "draft", "attempt": 1, "usage": {"tokens": 6}}),
    ("idle", {"step": "draft"}),
    ("teardown_started", {"step": "draft"}),
    ("step_completed", {"step": "draft", "output": {"text": "v2"}, "usage": {"tokens": 6}}),
    ("completed", {}),
]


def _write_pre_v1(store, run_id="old"):
    for kind, payload in PRE_V1:
        store.append(TrackEvent(run_id=run_id, event_type=kind, payload=payload, schema=0))


def test_a_pre_v1_track_keeps_its_version_and_still_has_its_status(store):
    _write_pre_v1(store)
    events = store.replay("old")
    assert {e.schema for e in events} == {0}
    assert [e.event_type for e in events][:1] == ["submitted"]  # stored as written, never rewritten
    assert derive_run_status(events) is RunState.COMPLETED


def test_the_reader_lifts_pre_v1_events_into_the_v1_shape(store):
    _write_pre_v1(store)
    lifted = [upgrade(e) for e in store.replay("old")]
    assert all(e.schema == SCHEMA_VERSION for e in lifted)
    kinds = [e.event_type for e in lifted]
    assert kinds[0] == "op_submitted" and "paused" not in kinds and "signal_received" not in kinds
    [escalated] = [e for e in lifted if e.event_type == "gate_escalated"]
    assert escalated.payload == {"step": "draft", "reason": "writer awaiting approval"}
    [decided] = [e for e in lifted if e.event_type == "gate_decided"]
    assert decided.payload == {"step": "draft", "value": {"approved": True}, "outcome": "send_back"}
    [completed] = [e for e in lifted if e.event_type == "step_completed"]
    assert completed.payload["payload"] == {"text": "v2"} and "output" not in completed.payload
    # Identity, order and time survive the lift.
    original = store.replay("old")
    assert [(e.event_id, e.sequence, e.occurred_at) for e in lifted] == \
        [(e.event_id, e.sequence, e.occurred_at) for e in original]


def test_a_pre_v1_duration_stop_and_rejection_read_as_v1(store):
    for kind, payload in (("submitted", {}), ("step_started", {"step": "s"}),
                          ("timed_out", {"step": "s", "reason": "run duration budget exceeded"})):
        store.append(TrackEvent(run_id="stopped", event_type=kind, payload=payload, schema=0))
    assert derive_run_status(store.replay("stopped")) is RunState.BUDGET_EXCEEDED
    [stop] = [upgrade(e) for e in store.replay("stopped") if e.event_type == "timed_out"]
    assert stop.event_type == "budget_exceeded" and stop.payload["dimension"] == "duration"
    for kind, payload in (("submitted", {}), ("paused", {"step": "s"}), ("rejected", {"step": "s", "value": None})):
        store.append(TrackEvent(run_id="no", event_type=kind, payload=payload, schema=0))
    assert derive_run_status(store.replay("no")) is RunState.REJECTED


def test_a_v1_event_passes_through_the_reader_unchanged():
    event = _event("gate_decided", step="s", outcome="approve")
    assert upgrade(event) is event


def test_an_event_built_without_a_version_is_read_as_written_before_v1():
    # A pre-v1 event rebuilt from an export without its version must still be lifted, not taken for v1.
    rebuilt = TrackEvent(run_id="r", event_type="paused", payload={"step": "s"})
    assert rebuilt.schema == 0
    assert upgrade(rebuilt).event_type == "gate_escalated"
    track = [TrackEvent(run_id="r", event_type="submitted"), TrackEvent(run_id="r", event_type="paused",
                                                                         payload={"step": "s"})]
    assert derive_run_status(track) is RunState.WAITING_AT_GATE


# --- SQLite specifics ---------------------------------------------------------------------


def test_sqlite_keeps_the_whole_run_id_on_a_payload(tmp_path):
    path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(path)
    SqliteTrackStore(path).put_payload("org/team-42", "org%2Fteam-42:s:0", json.dumps({"a": 1}))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT run_id FROM collab_track_payloads").fetchall() == [("org/team-42",)]
    finally:
        connection.close()


def test_sqlite_closes_every_connection_it_opens(tmp_path):
    path = tmp_path / "track.sqlite"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        SqliteTrackStore.ensure_schema(path)
        store = SqliteTrackStore(path)
        store.append(_event("op_submitted"))
        store.put_payload("r", "r:s:0", "{}")
        store.get_payload("r:s:0")
        list(store.stream("r", timeout_seconds=0.2))  # a few polls
        gc.collect()
    unclosed = [w for w in caught if issubclass(w.category, ResourceWarning) and "sqlite3" in str(w.message)]
    assert unclosed == []


# --- a conditional append -----------------------------------------------------------------------


def test_append_if_appends_only_when_its_condition_holds_of_the_track(store):
    store.append(_event("op_submitted", op={}))
    seen = []

    def no_request_yet(events):
        seen.append([event.event_type for event in events])
        return not any(event.event_type == "cancel_requested" for event in events)

    first = store.append_if(_event("cancel_requested", actor="a"), no_request_yet)
    assert first.sequence is not None and first.payload == {"actor": "a"}
    assert store.append_if(_event("cancel_requested", actor="b"), no_request_yet) is None
    assert seen == [["op_submitted"], ["op_submitted", "cancel_requested"]]  # the whole run, as it stood
    assert [event.payload for event in store.replay("r")][1:] == [{"actor": "a"}]
    with pytest.raises(ValueError, match="assigns event sequences"):
        store.append_if(replace(_event("cancel_requested"), sequence=7), lambda events: True)


def test_append_if_is_one_step_so_racing_callers_cannot_all_pass_the_check(store):
    store.append(_event("op_submitted", op={}))
    start, stored = threading.Barrier(8), []

    def ask(n):
        start.wait()
        stored.append(store.append_if(
            _event("cancel_requested", actor=f"actor-{n}"),
            lambda events: not any(event.event_type == "cancel_requested" for event in events)))

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([event for event in stored if event is not None]) == 1
    assert [event.event_type for event in store.replay("r")] == ["op_submitted", "cancel_requested"]


def test_replay_reads_at_most_limit_events_after_a_sequence(store):
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

    for n in range(5):
        store.append(TrackEvent(run_id="limited", event_type="op_submitted" if n == 0 else "step_started",
                                payload={"n": n}, schema=SCHEMA_VERSION))
    every = store.replay("limited")
    assert [e.sequence for e in store.replay("limited", limit=2)] == [e.sequence for e in every[:2]]
    assert [e.sequence for e in store.replay("limited", after_sequence=every[1].sequence, limit=2)] == \
        [e.sequence for e in every[2:4]]
    assert len(store.replay("limited", limit=None)) == 5
