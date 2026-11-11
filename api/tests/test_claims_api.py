"""The keyed claim's routes (``/v1/claims``): a worker's run token reaches its own step attempt's key, and no other.

A worker is simulated by what the controller leaves on the Track: a
``worker_started`` naming the hash of its run token and the instance it was
started for. The routes verify a presented token against that record and
never ask the controller.
"""

# ruff: noqa: F811 - the fixtures imported from test_runs_api are used as parameters
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from collab_hub_execution import InMemoryCogExecutor, LifecycleRunner, ResultEnvelope, SqliteTrackStore, run_tokens
from collab_hub_execution.claims import SqliteClaimStore
from collab_hub_execution.controller import RunController
from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent
from httpx import ASGITransport, AsyncClient
from test_runs_api import ECHO, package, runs_client, runs_config, settle  # noqa: F401 - fixtures

from collab_hub_api.config import Config, recommended_path_rules
from collab_hub_api.core import make_app
from collab_hub_api.frames.auth import AuthContext, get_auth_context

ENVELOPE = ResultEnvelope.success({"sent": "once"}, usage={"tokens": 1}).to_dict()


def _append(track, run_id, kind, **payload):
    track.append(TrackEvent(run_id=run_id, event_type=kind, payload=payload, schema=SCHEMA_VERSION))


def _worker_of(track, run_id, step="s", attempt=0):
    """A worker the controller started on the run's step attempt: its token, with the hash the Track records."""
    token = run_tokens.mint()
    _append(
        track,
        run_id,
        "worker_started",
        step=step,
        attempt=attempt,
        instance=f"{step}:{attempt}",
        location="local",
        run_token_sha256=run_tokens.digest(token),
    )
    return token


async def _run(client, track):
    """A submitted run, picked up and with a step started, as a controller leaves it before the worker acts."""
    run_id = (await client.post("/v1/runs", json=ECHO)).json()["id"]
    _append(track, run_id, "run_picked_up", controller="c")
    _append(track, run_id, "step_started", step="s", cog="echo", digest=None, attempt=0)
    return run_id


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _reserve(client, key, token):
    return await client.post("/v1/claims/reserve", json={"key": key}, headers=_bearer(token))


async def _commit(client, key, token, envelope=ENVELOPE):
    return await client.post("/v1/claims/commit", json={"key": key, "envelope": envelope}, headers=_bearer(token))


async def _release(client, key, token):
    return await client.post("/v1/claims/release", json={"key": key}, headers=_bearer(token))


async def _get(client, key, token):
    return await client.get("/v1/claims", params={"key": key}, headers=_bearer(token))


async def test_a_worker_reserves_commits_and_reads_its_key_with_its_run_token(runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    token = _worker_of(track, run_id)
    key = f"{run_id}:s:0"
    reserved = await _reserve(runs_client, key, token)
    assert reserved.status_code == 201, reserved.text
    assert reserved.json()["state"] == "reserved" and reserved.json()["yours"] is True
    assert reserved.json()["run_id"] == run_id and reserved.json()["envelope"] is None
    # Reserving again, the answer having been lost, still says to act.
    again = await _reserve(runs_client, key, token)
    assert again.status_code == 201 and again.json()["yours"] is True
    committed = await _commit(runs_client, key, token)
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "committed" and committed.json()["envelope"] == ENVELOPE
    assert committed.json()["committed_at"] and committed.json()["yours"] is False
    read = await _get(runs_client, key, token)
    assert read.status_code == 200 and read.json()["state"] == "committed"
    # A replay, by the worker that replaced this one on the same attempt: the envelope, and nothing to act on.
    _append(track, run_id, "worker_stopped", step="s", attempt=0, instance="s:0", reaped=True)
    replacement = _worker_of(track, run_id)
    replay = await _reserve(runs_client, key, replacement)
    assert replay.status_code == 200 and replay.json()["state"] == "committed"
    assert replay.json()["yours"] is False and replay.json()["envelope"] == ENVELOPE
    # The claims sit in the Track's own file.
    assert SqliteClaimStore(runs_config.runs.track_path).get(run_id, key).state == "committed"


async def test_a_step_name_with_a_slash_a_colon_or_at_the_length_limit_is_claimed_like_any_other(
    runs_client, runs_config
):
    # The key travels in the body or the query, never the path: nothing in a step name can break the route.
    from collab_hub_execution.claims import key_component

    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    for step in ("fetch/data", "a:b%c", ":" * 128):
        token = _worker_of(track, run_id, step=key_component(step))
        key = f"{run_id}:{key_component(step)}:0"
        reserved = await _reserve(runs_client, key, token)
        assert reserved.status_code == 201, (step, reserved.text)
        assert (await _commit(runs_client, key, token)).status_code == 200
        read = await _get(runs_client, key, token)
        assert read.status_code == 200 and read.json()["key"] == key


async def test_a_run_token_reaches_its_own_step_attempts_key_and_no_other(runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    mine, other = await _run(runs_client, track), await _run(runs_client, track)
    token = _worker_of(track, mine)
    _worker_of(track, other)
    store = SqliteClaimStore(runs_config.runs.track_path)
    # Another run's key is refused like an unknown token, so the answer never says whether that run exists.
    refused = await _reserve(runs_client, f"{other}:s:0", token)
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "run_token_invalid"
    assert (await _get(runs_client, f"{other}:s:0", token)).status_code == 401
    assert store.get(other, f"{other}:s:0") is None  # nothing was written
    # Another step's key of its own run, or another attempt's, is not this worker's step attempt: 403, and
    # nothing is reserved, committed, released or read under it.
    _append(track, mine, "step_started", step="send", cog="echo", digest=None, attempt=0)
    send = _worker_of(track, mine, step="send")
    for key in (f"{mine}:send:0", f"{mine}:s:1"):
        for call in (_reserve, _commit, _release, _get):
            answered = await call(runs_client, key, token)
            assert answered.status_code == 403 and answered.json()["error"]["code"] == "claim_not_yours", (
                key,
                answered.text,
            )
            assert answered.json()["error"]["details"] == {"step": "s", "attempt": 0}
    assert store.get(mine, f"{mine}:send:0") is None
    # The step's own worker reserves and commits it; the other worker still cannot read its envelope.
    assert (await _reserve(runs_client, f"{mine}:send:0", send)).status_code == 201
    assert (await _commit(runs_client, f"{mine}:send:0", send)).status_code == 200
    assert (await _get(runs_client, f"{mine}:send:0", token)).status_code == 403
    assert (await _get(runs_client, f"{mine}:send:0", send)).json()["envelope"] == ENVELOPE


async def test_a_reserve_needs_a_worker_that_is_up_while_a_commit_needs_only_an_attempt_without_an_outcome(
    runs_client, runs_config
):
    # The controller died after its worker reserved; the restarted one recorded the run interrupted and the
    # worker stopped. The worker, still acting, commits what it produced: the retry is answered with it.
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    token = _worker_of(track, run_id)
    key = f"{run_id}:s:0"
    assert (await _reserve(runs_client, key, token)).status_code == 201
    _append(track, run_id, "interrupted", backend="none")
    _append(track, run_id, "worker_stopped", step="s", attempt=0, instance="s:0", reaped=False)
    stopped = await _reserve(runs_client, key, token)
    assert stopped.status_code == 401 and "that is up" in stopped.json()["error"]["message"]
    committed = await _commit(runs_client, key, token)
    assert committed.status_code == 200, committed.text
    assert (await _get(runs_client, key, token)).json()["state"] == "committed"
    # The retry's worker, on the same attempt, finds the envelope.
    _append(track, run_id, "retry_requested", from_status="INTERRUPTED", attempt="same", controller="c")
    replacement = _worker_of(track, run_id)
    replay = await _reserve(runs_client, key, replacement)
    assert replay.status_code == 200 and replay.json()["state"] == "committed"
    assert replay.json()["envelope"] == ENVELOPE
    # Once the attempt's outcome is on the Track, the token verifies no more: the claim has nothing to settle.
    _append(track, run_id, "step_completed", step="s", attempt=0, cog="echo", digest=None, payload={"sent": 1})
    assert (await _commit(runs_client, key, token)).status_code == 401
    assert (await _get(runs_client, key, replacement)).status_code == 401
    # The same holds for a run recorded cancelled while the worker acted.
    cancelled = await _run(runs_client, track)
    token = _worker_of(track, cancelled)
    assert (await _reserve(runs_client, f"{cancelled}:s:0", token)).status_code == 201
    _append(track, cancelled, "cancelled", actor="dev-user")
    assert (await _commit(runs_client, f"{cancelled}:s:0", token)).status_code == 200


async def test_a_key_reserved_by_a_worker_that_was_replaced_is_outcome_unknown(runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    key = f"{run_id}:s:0"
    lost = _worker_of(track, run_id)
    assert (await _reserve(runs_client, key, lost)).status_code == 201
    _append(track, run_id, "worker_stopped", step="s", attempt=0, instance="s:0", reaped=True)
    replacement = _worker_of(track, run_id)
    replay = await _reserve(runs_client, key, replacement)
    assert replay.status_code == 200 and replay.json()["state"] == "outcome_unknown"
    assert replay.json()["yours"] is False
    refused = await _commit(runs_client, key, replacement)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "claim_refused"


async def test_a_worker_that_acted_on_nothing_releases_its_key_and_a_replay_reserves_it_afresh(
    runs_client, runs_config
):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    key = f"{run_id}:s:0"
    token = _worker_of(track, run_id)
    never = await _release(runs_client, key, token)
    assert never.status_code == 409 and "nothing to release" in never.json()["error"]["message"]
    assert (await _reserve(runs_client, key, token)).status_code == 201
    released = await _release(runs_client, key, token)
    assert released.status_code == 204 and released.content == b""
    assert (await _get(runs_client, key, token)).status_code == 404
    # The next worker on the attempt reserves it as new, and acts; a committed key cannot be released.
    _append(track, run_id, "worker_stopped", step="s", attempt=0, instance="s:0", reaped=True)
    replacement = _worker_of(track, run_id)
    replay = await _reserve(runs_client, key, replacement)
    assert replay.status_code == 201 and replay.json()["yours"] is True
    assert (await _commit(runs_client, key, replacement)).status_code == 200
    held = await _release(runs_client, key, replacement)
    assert held.status_code == 409 and "cannot be released" in held.json()["error"]["message"]


async def test_nothing_is_read_from_the_track_before_the_token_and_the_key_have_a_workers_shape(
    runs_client, runs_config, monkeypatch
):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    token = _worker_of(track, run_id)
    service = runs_client._transport.app.state.run_service
    reads = []
    replay = service.track.replay
    monkeypatch.setattr(service.track, "replay", lambda *args, **kwargs: (reads.append(args), replay(*args))[1])
    for headers, code in (({}, "run_token_required"), (_bearer("not-a-token"), "run_token_invalid")):
        for call in (_reserve, _commit, _release, _get):
            answered = await call(runs_client, f"{run_id}:s:0", headers.get("Authorization", " ")[7:])
            assert answered.status_code == 401 and answered.json()["error"]["code"] == code, answered.text
    assert reads == []  # no bearer, and a bearer that is no run token, cost no Track read
    for key in ("no-run-here", ":s:0", "nobody", f"{run_id}:s", f"{run_id}:s:x", "has spaces:s:0"):
        answered = await _reserve(runs_client, key, token)
        assert answered.status_code == 422 and answered.json()["error"]["code"] == "claim_key_invalid", key
    assert reads == []  # nor did a key that names no run
    # A well-formed token of no run reads that run's Track once, and finds nothing; the run views stay as they
    # were, since the claim routes read the Track directly.
    kept = dict(service.views._kept)
    answered = await _reserve(runs_client, "nobody:s:0", run_tokens.mint())
    assert answered.status_code == 401 and reads == [("nobody",)]
    assert dict(service.views._kept) == kept


async def test_a_commit_is_refused_without_a_reservation_or_with_something_that_is_no_envelope(
    runs_client, runs_config
):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    token = _worker_of(track, run_id)
    key = f"{run_id}:s:0"
    never = await _commit(runs_client, key, token)
    assert never.status_code == 409 and "never reserved" in never.json()["error"]["message"]
    assert (await _get(runs_client, key, token)).status_code == 404
    await _reserve(runs_client, key, token)
    invalid = await _commit(runs_client, key, token, envelope={"pause": True})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "envelope_invalid"
    malformed = await runs_client.post(
        "/v1/claims/commit", json={"key": key, "result": ENVELOPE}, headers=_bearer(token)
    )
    assert malformed.status_code == 422
    assert (await _get(runs_client, key, token)).json()["state"] == "reserved"


async def test_a_reservation_whose_attempt_has_no_outcome_is_never_pruned(runs_client, runs_config, monkeypatch):
    # Thirty-one days on, the interrupted run's reservation is what its retry resumes: it stays. The claims of
    # attempts whose outcome is on the Track, and of runs no retry reaches, go.
    from collab_hub_api.routers import runs as runs_router

    track = SqliteTrackStore(runs_config.runs.track_path)
    service = runs_client._transport.app.state.run_service
    interrupted, done, cancelled = [await _run(runs_client, track) for _ in range(3)]
    tokens = {}
    for run_id in (interrupted, done, cancelled):
        tokens[run_id] = _worker_of(track, run_id)
        assert (await _reserve(runs_client, f"{run_id}:s:0", tokens[run_id])).status_code == 201
    assert (await _commit(runs_client, f"{done}:s:0", tokens[done])).status_code == 200
    _append(track, interrupted, "interrupted", backend="none")
    _append(track, done, "step_completed", step="s", attempt=0, cog="echo", digest=None, payload={})
    _append(track, done, "completed")
    _append(track, cancelled, "cancelled", actor="dev-user")
    store = SqliteClaimStore(runs_config.runs.track_path)
    store.retention = timedelta(0)
    service.claims = store  # the API prunes with its own store; this one has no retention
    service._pruned_at = None
    monkeypatch.setattr(runs_router, "CLAIM_PRUNE_INTERVAL", 0.0)
    # Any reserve starts a pass, off its own path; the test waits for the one it starts.
    later = await _run(runs_client, track)
    assert (await _reserve(runs_client, f"{later}:s:0", _worker_of(track, later))).status_code == 201
    service._pruned_at = None
    service.prune_claims(wait=True)
    assert store.get(interrupted, f"{interrupted}:s:0").state == "reserved"
    assert store.get(done, f"{done}:s:0") is None and store.get(cancelled, f"{cancelled}:s:0") is None
    # ...and a prune by age alone is not something the store offers.
    assert store.prune(lambda claim: True, now=datetime.now(UTC) + timedelta(days=1)) == 2  # later's, interrupted's
    assert store.prune(lambda claim: False, now=datetime.now(UTC) + timedelta(days=1)) == 0


@pytest_asyncio.fixture
async def hardened_client(runs_config, monkeypatch):
    """The run API under the hardened protection map: every path authenticated unless listed."""
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    monkeypatch.setenv("DEV_AUTH_WORKSPACE", "default")
    values = runs_config.model_dump(mode="json")
    values["security"] = {
        "paths": [rule.model_dump() for rule in recommended_path_rules()],
        "default_access": "authenticated",
    }
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_the_claim_routes_take_the_run_token_under_the_hardened_map_and_nothing_else(
    hardened_client, runs_config
):
    # The map's bearer auth would refuse a run token before the route saw it: the claim routes authenticate
    # themselves, outside it, and a request with no run token is still a 401 in the API's envelope.
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(hardened_client, track)
    token = _worker_of(track, run_id)
    reserved = await _reserve(hardened_client, f"{run_id}:s:0", token)
    assert reserved.status_code == 201, reserved.text
    refused = await hardened_client.post("/v1/claims/reserve", json={"key": f"{run_id}:s:0"})
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "run_token_required"


@pytest_asyncio.fixture
async def operator_client(runs_config, monkeypatch):
    """The run API, called by a platform operator."""
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(runs_config)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user="ops", home_org_id="dev-org", workspace_id="default", platform_role="operator"
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_an_operator_reconciles_a_step_that_ended_outcome_unknown_and_the_run_then_retries(
    operator_client, runs_config
):
    track = SqliteTrackStore(runs_config.runs.track_path)
    store = SqliteClaimStore(runs_config.runs.track_path)
    unknown = ResultEnvelope.failure("outcome-unknown", "the key was reserved by a worker that never committed")
    answers = iter([unknown, ResultEnvelope.success({"sent": "once"})])
    controller = RunController(
        LifecycleRunner(executor=InMemoryCogExecutor({"echo": lambda e, v, **f: next(answers)}), track=track),
        poll_interval=0.01,
    )
    run_id = (await operator_client.post("/v1/runs", json=ECHO)).json()["id"]
    settle(controller)
    failed = (await operator_client.get(f"/v1/runs/{run_id}")).json()
    assert failed["status"] == "FAILED" and failed["error"] == "outcome-unknown"
    # The claim as the lost worker's replacement left it: the hub's store moved it to outcome_unknown.
    store.reserve(run_id, f"{run_id}:greet:0", worker="lost")
    store.reserve(run_id, f"{run_id}:greet:0", worker="replacement")
    refused = await operator_client.post(f"/v1/runs/{run_id}/retry")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "run_not_retryable"
    assert "'greet' ended outcome-unknown" in refused.json()["error"]["message"]
    nothing = await operator_client.post(f"/v1/runs/{run_id}/reconcile", json={"step": "other/step:1"})
    assert nothing.status_code == 409 and nothing.json()["error"]["code"] == "run_not_reconcilable"
    assert nothing.json()["error"]["details"] == {"status": "FAILED", "step": "other/step:1"}  # any name reaches it
    assert (await operator_client.post("/v1/runs/run-nobody/reconcile", json={"step": "greet"})).status_code == 404
    assert (await operator_client.post(f"/v1/runs/{run_id}/reconcile", json={})).status_code == 422
    # A person settles what the lost worker did; the Track and the claim both say so, and the run then
    # retries as a new attempt.
    reconciled = await operator_client.post(
        f"/v1/runs/{run_id}/reconcile", json={"step": "greet", "note": "sent once, confirmed"}
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "FAILED" and reconciled.json()["ended"] is True
    events = (await operator_client.get(f"/v1/runs/{run_id}/events")).json()["items"]
    [recorded] = [e["payload"] for e in events if e["type"] == "outcome_reconciled"]
    assert recorded == {"step": "greet", "actor": "ops", "note": "sent once, confirmed"}
    claim = store.get(run_id, f"{run_id}:greet:0")
    assert claim.state == "recorded" and claim.actor == "ops"
    once = await operator_client.post(f"/v1/runs/{run_id}/reconcile", json={"step": "greet"})
    assert once.status_code == 409
    assert (await operator_client.post(f"/v1/runs/{run_id}/retry")).status_code == 202
    settle(controller, until=lambda: track.runs(retry_asked=True) == ())
    assert (await operator_client.get(f"/v1/runs/{run_id}")).json()["status"] == "COMPLETED"


async def test_reconciling_takes_the_operator_platform_role(runs_client):
    # The dev user holds no platform role: refused before the run is looked at.
    refused = await runs_client.post("/v1/runs/run-nobody/reconcile", json={"step": "greet", "note": "x"})
    assert refused.status_code == 403, refused.text


POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database")
async def test_on_the_hubs_database_the_claims_live_in_the_table_its_migrations_create(tmp_path, monkeypatch):
    from collab_hub_execution import PostgresTrackStore
    from collab_hub_execution.claims import PostgresClaimStore
    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=10.0)
    try:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        run_collab_schema_migrations(database)  # collab_claims comes from migration 19
        package(tmp_path / "cogs", "echo")
        config = Config.parse(
            {
                "storage": {"frames_path": str(tmp_path / "frames")},
                "frames": {
                    "postgres": {"url": POSTGRES_URL},
                    "active_state": {"backend": "memory"},
                    "history": {"backend": "memory"},
                    "usage": {"backend": "memory"},
                    "mcp_session_manager_enabled": False,
                },
                "tasks": {"backend": "memory"},
                "runs": {"packages": [str(tmp_path / "cogs")]},
            }
        )
        for name, value in {
            "FRAMES_UNSAFE_AUTH_ENABLED": "true",
            "DEV_AUTH_ENABLED": "true",
            "DEV_AUTH_USER": "dev-user",
            "DEV_AUTH_ORG": "dev-org",
            "DEV_AUTH_WORKSPACE": "default",
        }.items():
            monkeypatch.setenv(name, value)
        app = make_app(config)
        track = PostgresTrackStore(database)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                run_id = await _run(client, track)
                token = _worker_of(track, run_id)
                key = f"{run_id}:s:0"
                assert (await _reserve(client, key, token)).status_code == 201
                committed = await _commit(client, key, token)
                assert committed.status_code == 200, committed.text
        claim = PostgresClaimStore(database).get(run_id, key)
        assert claim is not None and claim.state == "committed" and claim.envelope == ENVELOPE
        with database.connection() as conn:
            (count,) = tuple(conn.execute("SELECT count(*) FROM collab_claims").fetchone().values())
            assert count == 1
    finally:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        database.close()
