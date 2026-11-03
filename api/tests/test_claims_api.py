"""The keyed claim's routes (``/v1/claims``): a worker's run token reaches its own run's keys, and no other.

A worker is simulated by what the controller leaves on the Track: a
``worker_started`` naming the hash of its run token. The routes verify a
presented token against that record and never ask the controller.
"""

# ruff: noqa: F811 - the fixtures imported from test_runs_api are used as parameters
from __future__ import annotations

import os

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

ENVELOPE = ResultEnvelope.success({"sent": "once"}, usage={"tokens": 1}).to_dict()


def _append(track, run_id, kind, **payload):
    track.append(TrackEvent(run_id=run_id, event_type=kind, payload=payload, schema=SCHEMA_VERSION))


def _worker_of(track, run_id, instance="s:0"):
    """A worker the controller started on the run: its token, with the hash the Track records."""
    token = run_tokens.mint()
    _append(track, run_id, "worker_started", step="s", attempt=0, instance=instance, location="local",
            run_token_sha256=run_tokens.digest(token))
    return token


async def _run(client, track):
    """A submitted run, picked up and with a step started, as a controller leaves it before the worker acts."""
    run_id = (await client.post("/v1/runs", json=ECHO)).json()["id"]
    _append(track, run_id, "run_picked_up", controller="c")
    _append(track, run_id, "step_started", step="s", cog="echo", digest=None, attempt=0)
    return run_id


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_a_worker_reserves_commits_and_reads_its_key_with_its_run_token(runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    token = _worker_of(track, run_id)
    key = f"{run_id}:s:0"
    reserved = await runs_client.post(f"/v1/claims/{key}/reserve", headers=_bearer(token))
    assert reserved.status_code == 201, reserved.text
    assert reserved.json()["state"] == "reserved" and reserved.json()["yours"] is True
    assert reserved.json()["run_id"] == run_id and reserved.json()["envelope"] is None
    # Reserving again, the answer having been lost, still says to act.
    again = await runs_client.post(f"/v1/claims/{key}/reserve", headers=_bearer(token))
    assert again.status_code == 201 and again.json()["yours"] is True
    committed = await runs_client.post(f"/v1/claims/{key}/commit", json={"envelope": ENVELOPE}, headers=_bearer(token))
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "committed" and committed.json()["envelope"] == ENVELOPE
    assert committed.json()["committed_at"] and committed.json()["yours"] is False
    read = await runs_client.get(f"/v1/claims/{key}", headers=_bearer(token))
    assert read.status_code == 200 and read.json()["state"] == "committed"
    # A replay, by the worker that replaced this one: the envelope, and nothing to act on.
    replacement = _worker_of(track, run_id, instance="s:0:again")
    replay = await runs_client.post(f"/v1/claims/{key}/reserve", headers=_bearer(replacement))
    assert replay.status_code == 200 and replay.json()["state"] == "committed"
    assert replay.json()["yours"] is False and replay.json()["envelope"] == ENVELOPE
    # The claims sit in the Track's own file.
    assert SqliteClaimStore(runs_config.runs.track_path).get(run_id, key).state == "committed"


async def test_a_key_reserved_by_a_worker_that_was_replaced_is_outcome_unknown(runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    key = f"{run_id}:s:0"
    lost = _worker_of(track, run_id, instance="lost")
    assert (await runs_client.post(f"/v1/claims/{key}/reserve", headers=_bearer(lost))).status_code == 201
    _append(track, run_id, "worker_stopped", step="s", attempt=0, instance="lost", reaped=True)
    replacement = _worker_of(track, run_id)
    replay = await runs_client.post(f"/v1/claims/{key}/reserve", headers=_bearer(replacement))
    assert replay.status_code == 200 and replay.json()["state"] == "outcome_unknown"
    assert replay.json()["yours"] is False
    refused = await runs_client.post(f"/v1/claims/{key}/commit", json={"envelope": ENVELOPE},
                                     headers=_bearer(replacement))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "claim_refused"


async def test_a_run_token_reaches_only_its_own_runs_keys_and_only_while_its_worker_is_up(runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    mine, other = await _run(runs_client, track), await _run(runs_client, track)
    token = _worker_of(track, mine)
    _worker_of(track, other)
    refused = await runs_client.post(f"/v1/claims/{other}:s:0/reserve", headers=_bearer(token))
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "run_token_invalid"
    assert (await runs_client.get(f"/v1/claims/{other}:s:0", headers=_bearer(token))).status_code == 401
    assert SqliteClaimStore(runs_config.runs.track_path).get(other, f"{other}:s:0") is None  # nothing was written
    for key in ("no-run-here", "nobody:s:0", f"{mine}:s:0"):
        for headers in ({}, _bearer("not-a-token")):
            answered = await runs_client.post(f"/v1/claims/{key}/reserve", headers=headers)
            assert answered.status_code == 401, (key, headers, answered.text)
            assert answered.json()["error"]["code"] in ("run_token_required", "run_token_invalid")
    # Its own key, while it is up; not once the worker was stopped.
    assert (await runs_client.post(f"/v1/claims/{mine}:s:0/reserve", headers=_bearer(token))).status_code == 201
    _append(track, mine, "worker_stopped", step="s", attempt=0, instance="s:0")
    stopped = await runs_client.post(f"/v1/claims/{mine}:s:0/commit", json={"envelope": ENVELOPE},
                                     headers=_bearer(token))
    assert stopped.status_code == 401
    assert (await runs_client.get(f"/v1/claims/{mine}:s:0", headers=_bearer(token))).status_code == 401


async def test_a_commit_is_refused_without_a_reservation_or_with_something_that_is_no_envelope(
        runs_client, runs_config):
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(runs_client, track)
    token = _worker_of(track, run_id)
    key = f"{run_id}:s:0"
    never = await runs_client.post(f"/v1/claims/{key}/commit", json={"envelope": ENVELOPE}, headers=_bearer(token))
    assert never.status_code == 409 and "never reserved" in never.json()["error"]["message"]
    assert (await runs_client.get(f"/v1/claims/{key}", headers=_bearer(token))).status_code == 404
    await runs_client.post(f"/v1/claims/{key}/reserve", headers=_bearer(token))
    invalid = await runs_client.post(f"/v1/claims/{key}/commit", json={"envelope": {"pause": True}},
                                     headers=_bearer(token))
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "envelope_invalid"
    malformed = await runs_client.post(f"/v1/claims/{key}/commit", json={"result": ENVELOPE}, headers=_bearer(token))
    assert malformed.status_code == 422
    assert (await runs_client.get(f"/v1/claims/{key}", headers=_bearer(token))).json()["state"] == "reserved"


@pytest_asyncio.fixture
async def hardened_client(runs_config, monkeypatch):
    """The run API under the hardened protection map: every path authenticated unless listed."""
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    monkeypatch.setenv("DEV_AUTH_WORKSPACE", "default")
    values = runs_config.model_dump(mode="json")
    values["security"] = {"paths": [rule.model_dump() for rule in recommended_path_rules()],
                          "default_access": "authenticated"}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_the_claim_routes_take_the_run_token_under_the_hardened_map_and_nothing_else(hardened_client,
                                                                                            runs_config):
    # The map's bearer auth would refuse a run token before the route saw it: the claim routes authenticate
    # themselves, outside it, and a request with no run token is still a 401 in the API's envelope.
    track = SqliteTrackStore(runs_config.runs.track_path)
    run_id = await _run(hardened_client, track)
    token = _worker_of(track, run_id)
    reserved = await hardened_client.post(f"/v1/claims/{run_id}:s:0/reserve", headers=_bearer(token))
    assert reserved.status_code == 201, reserved.text
    refused = await hardened_client.post(f"/v1/claims/{run_id}:s:0/reserve")
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "run_token_required"


async def test_a_run_whose_step_ended_outcome_unknown_is_not_retried_until_reconciled(runs_client, runs_config):
    from collab_hub_execution import intents

    track = SqliteTrackStore(runs_config.runs.track_path)
    unknown = ResultEnvelope.failure("outcome-unknown", "the key was reserved by a worker that never committed")
    answers = iter([unknown, ResultEnvelope.success({"sent": "once"})])
    controller = RunController(LifecycleRunner(executor=InMemoryCogExecutor(
        {"echo": lambda e, v, **f: next(answers)}), track=track), poll_interval=0.01)
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    settle(controller)
    failed = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert failed["status"] == "FAILED" and failed["error"] == "outcome-unknown"
    refused = await runs_client.post(f"/v1/runs/{run_id}/retry")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "run_not_retryable"
    assert "'greet' ended outcome-unknown" in refused.json()["error"]["message"]
    # A person settles what the lost worker did; the run then retries as a new attempt.
    intents.reconcile_outcome(track, run_id, step="greet", actor="ops", note="sent once, confirmed")
    events = (await runs_client.get(f"/v1/runs/{run_id}/events")).json()["items"]
    [reconciled] = [e["payload"] for e in events if e["type"] == "outcome_reconciled"]
    assert reconciled == {"step": "greet", "actor": "ops", "note": "sent once, confirmed"}
    assert (await runs_client.post(f"/v1/runs/{run_id}/retry")).status_code == 202
    settle(controller, until=lambda: track.runs(retry_asked=True) == ())
    assert (await runs_client.get(f"/v1/runs/{run_id}")).json()["status"] == "COMPLETED"


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
        run_collab_schema_migrations(database)  # collab_claims comes from migration 17
        package(tmp_path / "cogs", "echo")
        config = Config.parse({
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"postgres": {"url": POSTGRES_URL}, "active_state": {"backend": "memory"},
                       "history": {"backend": "memory"}, "usage": {"backend": "memory"},
                       "mcp_session_manager_enabled": False},
            "tasks": {"backend": "memory"},
            "runs": {"packages": [str(tmp_path / "cogs")]},
        })
        for name, value in {"FRAMES_UNSAFE_AUTH_ENABLED": "true", "DEV_AUTH_ENABLED": "true",
                            "DEV_AUTH_USER": "dev-user", "DEV_AUTH_ORG": "dev-org",
                            "DEV_AUTH_WORKSPACE": "default"}.items():
            monkeypatch.setenv(name, value)
        app = make_app(config)
        track = PostgresTrackStore(database)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                run_id = await _run(client, track)
                token = _worker_of(track, run_id)
                key = f"{run_id}:s:0"
                assert (await client.post(f"/v1/claims/{key}/reserve", headers=_bearer(token))).status_code == 201
                committed = await client.post(f"/v1/claims/{key}/commit", json={"envelope": ENVELOPE},
                                              headers=_bearer(token))
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
