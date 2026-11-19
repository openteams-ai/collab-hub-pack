"""The claim conformance suite: every claim store passes it.

Reserve, commit, release, a replay of a committed key, a replay of a key
reserved by a worker that never committed, reconciliation and expiry, the same
assertions against the in-memory, SQLite and Postgres stores. Postgres needs
``TEST_POSTGRES_URL``, as the Track's suite does.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_execution import (
    ClaimRefused,
    InMemoryClaimStore,
    PostgresClaimStore,
    ResultEnvelope,
    SqliteClaimStore,
    SqliteTrackStore,
)
from collab_hub_execution.claims import (
    COMMITTED,
    OUTCOME_UNKNOWN,
    RECORDED,
    RESERVED,
    attempt_of,
    key_component,
    own_key,
    run_of,
)

TEST_PG = os.environ.get("TEST_POSTGRES_URL")
_NEEDS_PG = pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run the suite against Postgres")

ENVELOPE = ResultEnvelope.success({"sent": "the email"}, usage={"tokens": 3}).to_dict()


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres", marks=_NEEDS_PG),
                        pytest.param("postgres-dict-rows", marks=_NEEDS_PG)])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryClaimStore(retention=timedelta(days=1))
    elif request.param == "sqlite":
        # In the Track's own file, as dev level 1 and the desktop keep it.
        path = tmp_path / "track.sqlite"
        SqliteTrackStore.ensure_schema(path)
        SqliteClaimStore.ensure_schema(path)
        yield SqliteClaimStore(path, retention=timedelta(days=1))
    else:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        kwargs = {"row_factory": dict_row} if request.param == "postgres-dict-rows" else {}
        pool = ConnectionPool(TEST_PG, min_size=1, max_size=4, open=True, kwargs=kwargs)
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS collab_claims")
            PostgresClaimStore.ensure_schema(conn)
        yield PostgresClaimStore(pool, retention=timedelta(days=1))
        pool.close()


KEY = "r:send:0"


def test_a_key_is_reserved_once_and_the_reservation_is_its_workers(store):
    claim, acts = store.reserve("r", KEY, worker="w1")
    assert acts and claim.state == RESERVED and claim.worker == "w1" and claim.held_by("w1")
    assert claim.run_id == "r" and claim.key == KEY and claim.envelope is None and claim.committed_at is None
    assert store.get("r", KEY) == claim
    # The same worker reserving again (its answer was lost) still acts: nothing moved.
    again, acts = store.reserve("r", KEY, worker="w1")
    assert acts and again == claim
    assert store.get("r", "nobody") is None and store.get("other", KEY) is None


def test_a_committed_key_answers_every_replay_with_its_envelope(store):
    store.reserve("r", KEY, worker="w1")
    committed = store.commit("r", KEY, worker="w1", envelope=ENVELOPE)
    assert committed.state == COMMITTED and committed.envelope == ENVELOPE and committed.committed_at is not None
    for worker in ("w1", "w2"):  # the worker that committed, and the one that replaced it
        claim, acts = store.reserve("r", KEY, worker=worker)
        assert not acts and claim.state == COMMITTED and claim.envelope == ENVELOPE
    # Committing again, by the worker that did (its answer was lost), changes nothing.
    assert store.commit("r", KEY, worker="w1", envelope=ENVELOPE) == committed
    assert store.get("r", KEY) == committed


def test_a_key_reserved_by_a_lost_worker_becomes_outcome_unknown_and_nothing_acts(store):
    reserved, _ = store.reserve("r", KEY, worker="w1")
    claim, acts = store.reserve("r", KEY, worker="w2")  # w1 is gone: the side effect may have happened
    assert not acts and claim.state == OUTCOME_UNKNOWN and claim.worker == "w1"
    assert claim.updated_at >= reserved.updated_at
    # Nobody acts on it again: not the replacement, and not the lost worker come back late.
    for worker in ("w2", "w3", "w1"):
        claim, acts = store.reserve("r", KEY, worker=worker)
        assert not acts and claim.state == OUTCOME_UNKNOWN
    with pytest.raises(ClaimRefused, match="cannot be committed"):
        store.commit("r", KEY, worker="w1", envelope=ENVELOPE)
    with pytest.raises(ClaimRefused):
        store.commit("r", KEY, worker="w2", envelope=ENVELOPE)


def test_a_person_reconciles_an_unknown_outcome(store):
    store.reserve("r", KEY, worker="w1")
    store.reserve("r", KEY, worker="w2")
    recorded = store.reconcile("r", KEY, actor="ops@example.com")
    assert recorded.state == RECORDED and recorded.actor == "ops@example.com"
    assert store.get("r", KEY) == recorded
    claim, acts = store.reserve("r", KEY, worker="w3")
    assert not acts and claim.state == RECORDED  # settled: still nothing acts under this key
    with pytest.raises(ClaimRefused):
        store.reconcile("r", KEY, actor="ops@example.com")  # once
    store.reserve("r", "r:other:0", worker="w1")
    with pytest.raises(ClaimRefused, match="cannot be reconciled"):
        store.reconcile("r", "r:other:0", actor="ops@example.com")  # a reservation has nothing to settle
    with pytest.raises(ClaimRefused, match="no claim"):
        store.reconcile("r", "r:never:0", actor="ops@example.com")


def test_a_worker_that_acted_on_nothing_releases_its_key_and_the_next_reserve_is_fresh(store):
    with pytest.raises(ClaimRefused, match="nothing to release"):
        store.release("r", KEY, worker="w1")
    store.reserve("r", KEY, worker="w1")
    with pytest.raises(ClaimRefused, match="another worker"):
        store.release("r", KEY, worker="w2")
    assert store.get("r", KEY).state == RESERVED  # the refusal moved nothing
    store.release("r", KEY, worker="w1")
    assert store.get("r", KEY) is None
    # As if never reserved: the replacement reserves it as new and acts, instead of finding a lost worker's.
    claim, acts = store.reserve("r", KEY, worker="w2")
    assert acts and claim.state == RESERVED and claim.worker == "w2"
    store.commit("r", KEY, worker="w2", envelope=ENVELOPE)
    with pytest.raises(ClaimRefused, match="cannot be released"):
        store.release("r", KEY, worker="w2")  # committed: the envelope stands
    store.reserve("r", "r:lost:0", worker="w1")
    store.reserve("r", "r:lost:0", worker="w2")
    with pytest.raises(ClaimRefused, match="cannot be released"):
        store.release("r", "r:lost:0", worker="w1")  # an unknown outcome is a person's to settle


def test_committing_a_key_the_worker_does_not_hold_is_refused(store):
    with pytest.raises(ClaimRefused, match="never reserved"):
        store.commit("r", KEY, worker="w1", envelope=ENVELOPE)
    store.reserve("r", KEY, worker="w1")
    with pytest.raises(ClaimRefused, match="another worker"):
        store.commit("r", KEY, worker="w2", envelope=ENVELOPE)
    assert store.get("r", KEY).state == RESERVED  # the refusal moved nothing
    store.commit("r", KEY, worker="w1", envelope=ENVELOPE)
    with pytest.raises(ClaimRefused, match="another worker|cannot be committed"):
        store.commit("r", KEY, worker="w2", envelope={**ENVELOPE, "payload": {"sent": "twice"}})
    assert store.get("r", KEY).envelope == ENVELOPE


def test_keys_are_scoped_to_their_run(store):
    # The same key text under two runs is two claims; the hub never asks for one with the other's run.
    store.reserve("r", KEY, worker="w1")
    claim, acts = store.reserve("other", KEY, worker="w2")
    assert acts and claim.run_id == "other" and claim.worker == "w2"
    assert store.get("r", KEY).worker == "w1" and store.get("other", KEY).worker == "w2"


def test_of_two_workers_reserving_one_key_at_once_exactly_one_acts(store):
    outcomes = {}
    start = threading.Barrier(2)

    def reserve(worker):
        start.wait()
        outcomes[worker] = store.reserve("r", "r:race:0", worker=worker)

    threads = [threading.Thread(target=reserve, args=(w,)) for w in ("w1", "w2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    acting = [worker for worker, (_, acts) in outcomes.items() if acts]
    assert len(acting) == 1
    # The loser found the key held by the winner, which has not committed: an unknown outcome, by the
    # claim's rule; the winner's commit is then refused, and the step is reconciled by a person. Two workers
    # on one attempt is what the controller's pickup prevents; the claim only makes sure neither acts twice.
    assert store.get("r", "r:race:0").worker == acting[0]


def test_claims_past_their_retention_are_pruned_once_settled_and_the_rest_kept(store):
    store.reserve("r", "r:old:0", worker="w1")
    store.commit("r", "r:old:0", worker="w1", envelope=ENVELOPE)
    store.reserve("r", "r:open:0", worker="w1")  # a reservation whose attempt has no outcome yet
    store.reserve("r", "r:new:0", worker="w1")
    settled = {"r:old:0", "r:new:0"}  # what the Track says of each attempt, as the hub asks it
    asked = []

    def on_track(claim):
        asked.append(claim.key)
        return claim.key in settled

    assert store.prune(on_track, now=datetime.now(UTC) + timedelta(hours=12)) == 0  # nothing a day old yet
    assert asked == []  # nothing past the retention was asked about
    assert store.get("r", "r:old:0") is not None and store.get("r", "r:new:0") is not None
    assert store.prune(on_track, now=datetime.now(UTC) + timedelta(days=2)) == 2
    assert sorted(asked) == ["r:new:0", "r:old:0", "r:open:0"]
    assert store.get("r", "r:old:0") is None and store.get("r", "r:new:0") is None
    # The open reservation is kept, however old: a retry of the interrupted run resumes that key, and finds
    # it reserved by the lost worker rather than acting a second time.
    claim = store.get("r", "r:open:0")
    assert claim is not None and claim.state == RESERVED
    replay, acts = store.reserve("r", "r:open:0", worker="w2")
    assert not acts and replay.state == OUTCOME_UNKNOWN
    # ...and is asked about again, and pruned once the Track says its attempt is settled.
    assert store.prune(on_track, now=datetime.now(UTC) + timedelta(days=4)) == 0
    settled.add("r:open:0")
    assert store.prune(on_track, now=datetime.now(UTC) + timedelta(days=4)) == 1
    assert store.get("r", "r:open:0") is None


def test_a_prune_judges_at_most_the_limit_oldest_first(store):
    for name in ("first", "second", "third"):
        store.reserve("r", f"r:{name}:0", worker="w1")
        store.commit("r", f"r:{name}:0", worker="w1", envelope=ENVELOPE)
    asked = []

    def noting(claim):
        asked.append(claim.key)
        return True

    later = datetime.now(UTC) + timedelta(days=2)
    assert store.prune(noting, now=later, limit=2) == 2
    assert asked == ["r:first:0", "r:second:0"] and store.get("r", "r:third:0") is not None
    assert store.prune(noting, now=later, limit=2) == 1 and store.get("r", "r:third:0") is None
    assert store.prune(noting, now=later, limit=0) == 0


def test_a_pruned_key_is_reserved_afresh(store):
    # After the retention, a settled key is a new key: what outlives an attempt's recorded outcome is history.
    store.reserve("r", KEY, worker="w1")
    store.commit("r", KEY, worker="w1", envelope=ENVELOPE)
    store.prune(lambda claim: True, now=datetime.now(UTC) + timedelta(days=2))
    claim, acts = store.reserve("r", KEY, worker="w2")
    assert acts and claim.state == RESERVED and claim.worker == "w2"


# --- the key ---------------------------------------------------------------------------------------


def test_the_run_is_read_from_the_key_and_escaped_components_round_trip():
    assert run_of("run-1:send:0") == "run-1"
    assert run_of(f"{key_component('a:b%c')}:s:0") == "a:b%c"
    assert run_of("no-delimiter") is None and run_of("") is None and run_of(":s:0") is None
    # Distinct (run, step) pairs stay distinct, however the ids are spelt.
    assert f"{key_component('a:b')}:{key_component('c')}:0" != f"{key_component('a')}:{key_component('b:c')}:0"
    # The whole key reads back, and a worker's own key is its run's component and the instance it was started
    # for, as `worker_started` records it.
    assert attempt_of("run-1:send:0") == ("run-1", "send", 0)
    assert attempt_of(f"{key_component('a:b')}:{key_component('fetch/data:x')}:12") == ("a:b", "fetch/data:x", 12)
    for text in ("", "run-1", "run-1:send", "run-1:send:x", "run-1::0", "run-1:send:0:more"):
        assert attempt_of(text) is None, text
    assert own_key("run-1", f"{key_component('a:b')}:3") == f"run-1:{key_component('a:b')}:3"
    assert own_key("a:b", "s:0") == f"{key_component('a:b')}:s:0"
