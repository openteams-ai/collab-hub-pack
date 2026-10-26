"""The run API (``/v1/runs``): launch, list, read a run's events, talk to it, cancel.

The API records intent on a SQLite Track here (on the hub's own Postgres in a
deployment) and reads status from it. A run
controller over the same Track, here with an in-memory executor, is what
advances the runs: the two never call each other.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import pytest
import pytest_asyncio
from collab_hub_execution import InMemoryCogExecutor, LifecycleRunner, ResultEnvelope, SqliteTrackStore
from collab_hub_execution.controller import RunController
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app

MANIFEST = '[workspace]\nname = "{name}"\n\n[tasks]\nserve = "python serve.py"\n'


def package(root, name):
    (root / name).mkdir(parents=True)
    (root / name / "pixi.toml").write_text(MANIFEST.format(name=name))
    (root / name / "pixi.lock").write_text("version: 6\n")


@pytest.fixture
def runs_config(tmp_path) -> Config:
    for name in ("echo", "slow", "hidden"):
        package(tmp_path / "cogs", name)
    # A directory that is allowlisted and has a manifest, and still cannot run: it has no lock.
    package(tmp_path / "cogs", "unlocked")
    (tmp_path / "cogs" / "unlocked" / "pixi.lock").unlink()
    return Config.parse({
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"active_state": {"backend": "memory"}, "history": {"backend": "memory"},
                   "usage": {"backend": "memory"}, "mcp_session_manager_enabled": False},
        "tasks": {"backend": "memory"},
        "runs": {"track_path": str(tmp_path / "track.sqlite"), "packages": [str(tmp_path / "cogs")],
                 "allow": ["echo", "slow", "unlocked"]},
    })


@pytest_asyncio.fixture
async def runs_client(runs_config, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    monkeypatch.setenv("DEV_AUTH_WORKSPACE", "default")
    app = make_app(runs_config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest.fixture
def controller(runs_config):
    """A controller over the API's Track; ``release`` lets the slow Cog answer."""
    release = threading.Event()

    def slow(entry_point, value, **feedback):
        release.wait(10)
        return ResultEnvelope.success({"slept": True})

    executor = InMemoryCogExecutor({"echo": lambda entry_point, value, **feedback: {"echo": value}, "slow": slow})
    runner = LifecycleRunner(executor=executor, track=SqliteTrackStore(runs_config.runs.track_path))
    controller = RunController(runner, poll_interval=0.01)
    controller.release = release
    yield controller
    release.set()


def settle(controller, until=lambda: True):
    """Let the controller act on what the Track holds, until ``until`` and nothing is in flight."""
    for _ in range(500):
        controller.tick()
        if until() and controller.idle():
            return
        threading.Event().wait(0.01)
    raise AssertionError("the controller did not settle")


ECHO = {"steps": [{"name": "greet", "cog": "echo", "entry_point": "run", "input": {"text": "hi"}}]}
SLOW = {"steps": [{"name": "wait", "cog": "slow", "entry_point": "run"}, {"name": "after", "cog": "echo",
                                                                           "entry_point": "run"}]}


async def test_a_submitted_run_waits_for_a_controller_and_completes_once_one_picks_it_up(runs_client, controller):
    created = await runs_client.post("/v1/runs", json=ECHO)
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "SUBMITTED" and run["ended"] is False
    assert run["backend"] == "none" and run["location"] == "local"  # so a client assumes nothing of either
    assert run["submitted_by"] == "dev-user" and run["submitted_by_name"] is None  # dev auth carries no name
    assert run["steps"] == [{"name": "greet", "cog": "echo", "entry_point": "run", "state": "pending",
                             "attempt": None, "error": None, "output": None, "output_ref": None}]
    # The API started nothing: the run advances only once a controller reads the Track.
    assert (await runs_client.get(f"/v1/runs/{run['id']}")).json()["status"] == "SUBMITTED"
    settle(controller)
    done = (await runs_client.get(f"/v1/runs/{run['id']}")).json()
    assert done["status"] == "COMPLETED" and done["ended"] is True
    assert done["steps"][0]["state"] == "completed" and done["steps"][0]["attempt"] == 0
    # The single run carries what the Cog answered with; the listing does not.
    assert done["steps"][0]["output"] == {"echo": {"text": "hi"}}
    [listed] = (await runs_client.get("/v1/runs")).json()["items"]
    assert listed["status"] == "COMPLETED" and listed["steps"][0]["output"] is None


async def test_a_run_can_be_named_and_is_listed_by_that_name(runs_client):
    created = (await runs_client.post("/v1/runs", json={**ECHO, "name": "echo on Claude"})).json()
    assert created["name"] == "echo on Claude"
    assert (await runs_client.get(f"/v1/runs/{created['id']}")).json()["name"] == "echo on Claude"
    unnamed = (await runs_client.post("/v1/runs", json=ECHO)).json()
    assert unnamed["name"] is None
    assert [item["name"] for item in (await runs_client.get("/v1/runs")).json()["items"]] == [None, "echo on Claude"]
    for bad in ("", " leading space", "x" * 65, "semi;colon"):
        assert (await runs_client.post("/v1/runs", json={**ECHO, "name": bad})).status_code == 422, bad


async def test_runs_are_listed_newest_first_filtered_by_status_and_paged(runs_client, controller):
    first = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    settle(controller)
    second = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    listing = (await runs_client.get("/v1/runs")).json()
    assert [item["id"] for item in listing["items"]] == [second, first] and listing["next_offset"] is None
    completed = (await runs_client.get("/v1/runs", params={"status": "completed"})).json()
    assert [item["id"] for item in completed["items"]] == [first]
    page = (await runs_client.get("/v1/runs", params={"limit": 1})).json()
    assert [item["id"] for item in page["items"]] == [second] and page["next_offset"] == 1
    rest = (await runs_client.get("/v1/runs", params={"limit": 1, "offset": 1})).json()
    assert [item["id"] for item in rest["items"]] == [first] and rest["next_offset"] is None


async def test_cancelling_a_run_mid_step_ends_it_cancelled_with_its_actor(runs_client, controller, runs_config):
    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    track = SqliteTrackStore(runs_config.runs.track_path)
    for _ in range(500):  # until the slow step is in flight
        controller.tick()
        if any(event.event_type == "interaction_started" for event in track.replay(run_id)):
            break
        threading.Event().wait(0.01)
    assert (await runs_client.get(f"/v1/runs/{run_id}")).json()["steps"][0]["state"] == "running"
    accepted = await runs_client.post(f"/v1/runs/{run_id}/cancel")
    assert accepted.status_code == 202
    assert accepted.json()["cancel_requested_by"] == "dev-user" and accepted.json()["status"] == "RUNNING"
    again = await runs_client.post(f"/v1/runs/{run_id}/cancel")  # asking twice records it once
    assert again.status_code == 202
    assert [e.event_type for e in track.replay(run_id)].count("cancel_requested") == 1
    controller.tick()  # the controller delivers the request, then the step in flight returns
    controller.release.set()
    settle(controller)
    ended = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert ended["status"] == "CANCELLED"
    assert [step["state"] for step in ended["steps"]] == ["cancelled", "pending"]  # the second never started
    [cancelled] = [event for event in track.replay(run_id) if event.event_type == "cancelled"]
    assert cancelled.payload == {"actor": "dev-user"}
    conflict = await runs_client.post(f"/v1/runs/{run_id}/cancel")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "run_ended"
    assert "CANCELLED" in conflict.json()["error"]["message"]


async def test_a_run_cancelled_before_any_controller_picked_it_up_never_starts(runs_client, controller):
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    assert (await runs_client.post(f"/v1/runs/{run_id}/cancel")).status_code == 202
    settle(controller)
    ended = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert ended["status"] == "CANCELLED" and ended["steps"][0]["state"] == "pending"
    assert controller.runner.executor.materialized == []


@pytest.mark.parametrize(("cog", "why"), [("hidden", "not allowlisted"), ("absent", "not allowlisted"),
                                          ("unlocked", "has no pixi.lock"),
                                          ("../echo", "not a package name")])
async def test_a_step_naming_a_cog_the_hub_cannot_launch_is_refused_naming_those_it_can(runs_client, cog, why):
    body = {"steps": [{"name": "s", "cog": cog, "entry_point": "run"}]}
    refused = await runs_client.post("/v1/runs", json=body)
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "cog_not_launchable" and "echo, slow" in error["message"]
    assert error["details"]["launchable"] == ["echo", "slow"] and why in error["details"]["refused"][cog]
    assert (await runs_client.get("/v1/runs")).json()["items"] == []  # nothing was recorded


@pytest.mark.parametrize("body", [
    {"steps": []},
    {"steps": [{"name": "s", "cog": "echo"}]},
    {"steps": [{"name": "s", "cog": "echo", "entry_point": "run", "gate": {"escalate": "sometimes"}}]},
    {"steps": [{"name": "s", "cog": "echo", "entry_point": "run"}, {"name": "s", "cog": "echo", "entry_point": "run"}]},
    {"steps": [{"name": "s", "cog": "echo", "entry_point": "run"}], "run_id": "mine"},
])
async def test_a_malformed_op_is_refused(runs_client, body):
    assert (await runs_client.post("/v1/runs", json=body)).status_code == 422


async def test_a_steps_gate_is_recorded_with_the_op(runs_client, controller):
    body = {"steps": [{"name": "s", "cog": "echo", "entry_point": "run", "gate": {"escalate": "always"}}]}
    run_id = (await runs_client.post("/v1/runs", json=body)).json()["id"]
    settle(controller)
    waiting = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert waiting["status"] == "WAITING_AT_GATE" and waiting["steps"][0]["state"] == "waiting_at_gate"


async def test_another_organization_can_neither_see_nor_cancel_a_run(runs_client, monkeypatch):
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    monkeypatch.setenv("DEV_AUTH_ORG", "another-org")
    assert (await runs_client.get("/v1/runs")).json()["items"] == []
    missing = await runs_client.get(f"/v1/runs/{run_id}")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "run_not_found"
    assert (await runs_client.post(f"/v1/runs/{run_id}/cancel")).status_code == 404
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    assert (await runs_client.get(f"/v1/runs/{run_id}")).json()["cancel_requested_by"] is None


async def test_unknown_and_malformed_run_ids(runs_client):
    assert (await runs_client.get("/v1/runs/run-000000000000")).status_code == 404
    assert (await runs_client.post("/v1/runs/run-000000000000/cancel")).status_code == 404
    assert (await runs_client.get("/v1/runs/has spaces")).status_code == 422


async def test_the_run_api_is_refused_without_a_caller(runs_config, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(runs_config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/runs")).status_code == 401
            assert (await client.post("/v1/runs", json=ECHO)).status_code == 401


async def test_a_hub_with_no_track_serves_the_run_api_as_unavailable(dev_client):
    # No database and no runs.track_path: the routes are there, and say why they cannot answer.
    answered = await dev_client.get("/v1/runs")
    assert answered.status_code == 503
    assert answered.json()["error"]["code"] == "runs_unavailable"
    assert "no database" in answered.json()["error"]["message"]
    assert (await dev_client.post("/v1/runs", json=ECHO)).status_code == 503
    assert "/v1/runs" in (await dev_client.get("/openapi.json")).json()["paths"]


def test_the_api_constructs_no_executor_and_reaches_execution_only_from_the_run_router():
    # ADR-0002 invariant 3: the API writes intent and reads the Track. Nothing in it imports the
    # runner, the controller or an executor, and the execution package is imported in one module,
    # which is itself imported only when the flag is on.
    source = Path(__file__).resolve().parents[1] / "src" / "collab_hub_api"
    forbidden = ("runner", "controller", "kubernetes", "locations.local", "locations.launcher", "backends")
    importers = []
    for path in sorted(source.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            used = [module for module in modules if module.split(".")[0] == "collab_hub_execution"]
            if used:
                importers.append(path.name)
            for module in used:
                assert not any(f".{part}" in f".{module.split('.', 1)[-1]}" or module.endswith("Executor")
                               or module.endswith("LifecycleRunner") for part in forbidden), (path.name, module)
    assert set(importers) == {"runs.py"}


RUNTIME_PROBE = """
import asyncio, json, os, sys
import collab_hub_execution as execution
from collab_hub_execution.controller import RunController
from collab_hub_execution.kubernetes import KubernetesCogExecutor
from collab_hub_execution.locations.local import LocalProcessCogExecutor
from httpx import ASGITransport, AsyncClient
from collab_hub_api.config import Config
from collab_hub_api.core import make_app

constructed = []
for cls in (execution.LifecycleRunner, RunController, LocalProcessCogExecutor, KubernetesCogExecutor,
            execution.InMemoryCogExecutor):
    def refuse(self, *args, _name=cls.__name__, **kwargs):
        constructed.append(_name)
        raise RuntimeError(f"the API constructed a {_name}")
    cls.__init__ = refuse

async def main():
    app = make_app(Config.parse(json.loads(sys.argv[1])))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            run = (await client.post("/v1/runs", json=json.loads(sys.argv[2]))).json()
            assert (await client.get("/v1/runs")).status_code == 200
            assert (await client.get("/v1/runs/launchable")).status_code == 200
            assert (await client.get(f"/v1/runs/{run['id']}")).status_code == 200
            assert (await client.post(f"/v1/runs/{run['id']}/turns", json={"text": "hi"})).status_code == 202
            assert (await client.post(f"/v1/runs/{run['id']}/cancel")).status_code == 202
    print(json.dumps(constructed))

asyncio.run(main())
"""


def test_the_api_process_constructs_no_runner_controller_or_executor_while_serving_runs(runs_config, tmp_path):
    # The same invariant at run time, in a process of its own: with the run API on, a submission, a
    # listing, a turn and a cancel construct nothing that advances a run — every such constructor
    # refuses, and the routes still answer.
    import json
    import os
    import subprocess
    import sys

    env = {**os.environ, "FRAMES_UNSAFE_AUTH_ENABLED": "true", "DEV_AUTH_ENABLED": "true",
           "DEV_AUTH_USER": "dev-user", "DEV_AUTH_ORG": "dev-org", "DEV_AUTH_WORKSPACE": "default"}
    probed = subprocess.run([sys.executable, "-c", RUNTIME_PROBE, json.dumps(runs_config.model_dump(mode="json")),
                             json.dumps(ECHO)], capture_output=True, text=True, env=env, timeout=120)
    assert probed.returncode == 0, probed.stderr[-3000:]
    assert json.loads(probed.stdout.strip().splitlines()[-1]) == []


def test_the_retired_cog_runs_flag_is_accepted_and_changes_nothing(tmp_path):
    # Retired once the run API shipped: a deployment that still sets it starts, the name logged.
    config = Config.parse({"storage": {"frames_path": str(tmp_path)}, "features": {"cog_runs": True}})
    assert config.features.retired_names == ["cog_runs"]
    assert any(route.path == "/v1/runs" for route in make_app(config).routes)


async def test_with_no_package_directory_nothing_is_launchable(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    config = Config.parse({
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"active_state": {"backend": "memory"}, "history": {"backend": "memory"},
                   "usage": {"backend": "memory"}, "mcp_session_manager_enabled": False},
        "tasks": {"backend": "memory"},
        "runs": {"track_path": str(tmp_path / "track.sqlite")}})
    app = make_app(config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/v1/runs", json=ECHO)
    assert refused.status_code == 422 and "are: none" in refused.json()["error"]["message"]


async def test_a_package_that_does_not_exist_is_refused_without_naming_the_hubs_directories(
        runs_config, tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    runs_config.runs.allow = None
    app = make_app(runs_config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/v1/runs", json={"steps": [{"name": "s", "cog": "absent",
                                                                     "entry_point": "run"}]})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["refused"] == {"absent": "no package 'absent'"}
    assert str(tmp_path) not in refused.text


def test_package_directories_arrive_from_the_environment_as_one_path_list(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLAB_HUB_API__RUNS__PACKAGES", f"{tmp_path / 'a'}:{tmp_path / 'b'}")
    monkeypatch.setenv("COLLAB_HUB_API__RUNS__TRACK_PATH", str(tmp_path / "t.sqlite"))
    runs = Config.parse({"storage": {"frames_path": str(tmp_path)}}).runs
    assert runs.packages == [str(tmp_path / "a"), str(tmp_path / "b")] and runs.track_path.endswith("t.sqlite")


async def test_the_hub_names_the_cogs_it_can_launch(runs_client):
    assert (await runs_client.get("/v1/runs/launchable")).json() == {"items": ["echo", "slow"]}


async def test_a_turn_is_recorded_answered_through_the_track_and_read_back(runs_client, runs_config):
    from collab_hub_execution import intents

    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    asked = await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "sum 1 2"})
    assert asked.status_code == 202
    turn = asked.json()
    assert turn["state"] == "pending" and turn["text"] == "sum 1 2" and turn["asked_by"] == "dev-user"
    # The controller's half, as it records an answer.
    track = SqliteTrackStore(runs_config.runs.track_path)
    intents.answer_turn(track, run_id, turn["turn"], text="1 + 2 = 3")
    answered = (await runs_client.get(f"/v1/runs/{run_id}/turns/{turn['turn']}")).json()
    assert answered["state"] == "answered" and answered["answer"] == "1 + 2 = 3"
    missing = await runs_client.get(f"/v1/runs/{run_id}/turns/000000000000")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "turn_not_found"
    assert (await runs_client.get(f"/v1/runs/{run_id}/turns/not-a-turn")).status_code == 422
    assert (await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": ""})).status_code == 422


async def test_a_run_that_ended_takes_no_turns(runs_client, controller):
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    settle(controller)
    refused = await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "hello"})
    assert refused.status_code == 409 and "has ended COMPLETED" in refused.json()["error"]["message"]


async def test_another_organization_can_neither_talk_to_a_run_nor_read_its_turns(runs_client, monkeypatch):
    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    turn = (await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "hi"})).json()["turn"]
    monkeypatch.setenv("DEV_AUTH_ORG", "another-org")
    assert (await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "hi"})).status_code == 404
    assert (await runs_client.get(f"/v1/runs/{run_id}/turns/{turn}")).status_code == 404


async def test_a_run_shows_who_submitted_it_by_name_and_is_scoped_by_principal(runs_config, monkeypatch):
    from collab_hub_api.frames.auth import AuthContext, DisplayIdentity, get_auth_context

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(runs_config)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user="3b8ec34f", home_org_id="dev-org", workspace_id="default",
        display=DisplayIdentity(name="Dev User", email="dev@example.com"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            run = (await client.post("/v1/runs", json=ECHO)).json()
            listed = (await client.get("/v1/runs")).json()["items"]
    assert run["submitted_by"] == "3b8ec34f" and run["submitted_by_name"] == "Dev User"
    assert [item["submitted_by_name"] for item in listed] == ["Dev User"]


async def test_refusals_on_the_run_api_use_the_error_envelope(runs_client):
    # The API's error envelope, as on the other /v1 routes, with what was refused in its details.
    bad_name = await runs_client.post("/v1/runs", json={**ECHO, "name": "a/b"})
    assert bad_name.status_code == 422
    error = bad_name.json()["error"]
    assert error["code"] == "validation_error" and error["details"][0]["loc"] == ["body", "name"]
    bad_id = await runs_client.get("/v1/runs/has spaces")
    assert bad_id.status_code == 422 and bad_id.json()["error"]["code"]


async def test_a_turn_over_the_limit_in_bytes_is_refused_with_422_not_500(runs_client):
    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    from collab_hub_execution import intents

    text = "字" * (intents.MAX_TURN_TEXT // 3 + 1)  # fewer characters than the limit, more bytes
    assert len(text) < intents.MAX_TURN_TEXT < len(text.encode())
    refused = await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": text})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "turn_too_long"


async def test_a_run_that_cannot_be_read_is_left_out_of_the_list_and_not_found_alone(runs_client, runs_config):
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

    good = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    track = SqliteTrackStore(runs_config.runs.track_path)
    by = {"user": "dev-user", "org_id": "dev-org", "workspace_id": "default"}
    track.append(TrackEvent(run_id="run-broken00000", event_type="op_submitted",
                            payload={"op": {"run_id": "run-broken00000", "steps": []}, "submitted_by": by},
                            schema=SCHEMA_VERSION))
    track.append(TrackEvent(run_id="run-broken00000", event_type="gate_decided", payload={"outcome": "approve"},
                            schema=SCHEMA_VERSION))
    assert [item["id"] for item in (await runs_client.get("/v1/runs")).json()["items"]] == [good]
    assert (await runs_client.get("/v1/runs/run-broken00000")).status_code == 404


# --- the run's events (Phase 12) ----------------------------------------------------------------


async def test_a_runs_events_are_read_in_pages_after_a_sequence(runs_client, controller, monkeypatch):
    run = (await runs_client.post("/v1/runs", json=ECHO)).json()
    settle(controller)
    page = (await runs_client.get(f"/v1/runs/{run['id']}/events")).json()
    types = [event["type"] for event in page["items"]]
    assert types[:2] == ["op_submitted", "run_picked_up"] and types[-1] == "completed"
    assert page["next_after"] is None
    sequences = [event["sequence"] for event in page["items"]]
    assert sequences == sorted(sequences)
    # After a sequence, only what follows it; a limit, and where to go on from.
    after = (await runs_client.get(f"/v1/runs/{run['id']}/events", params={"after": sequences[1]})).json()
    assert [event["sequence"] for event in after["items"]] == sequences[2:]
    first = (await runs_client.get(f"/v1/runs/{run['id']}/events", params={"limit": 2})).json()
    assert [event["sequence"] for event in first["items"]] == sequences[:2] and first["next_after"] == sequences[1]
    # Another organization's run has no events to show it.
    monkeypatch.setenv("DEV_AUTH_ORG", "another-org")
    assert (await runs_client.get(f"/v1/runs/{run['id']}/events")).status_code == 404


async def test_where_a_worker_is_on_the_controllers_host_stays_out_of_the_events(runs_client, runs_config):
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

    run = (await runs_client.post("/v1/runs", json=ECHO)).json()
    SqliteTrackStore(runs_config.runs.track_path).append(TrackEvent(
        run_id=run["id"], event_type="worker_started", schema=SCHEMA_VERSION, payload={
            "step": "greet", "attempt": 1, "instance": "greet:1", "location": "local", "pid": 4242, "pgid": 4242,
            "logs": "/var/runs/x", "run_token_sha256": "0" * 64}))
    [started] = [event for event in (await runs_client.get(f"/v1/runs/{run['id']}/events")).json()["items"]
                 if event["type"] == "worker_started"]
    assert started["payload"] == {"step": "greet", "attempt": 1, "instance": "greet:1", "location": "local",
                                  "run_token_sha256": "0" * 64}


async def test_only_the_fields_named_for_an_event_are_served_so_a_new_one_stays_out(runs_client, runs_config):
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

    from collab_hub_api.routers.runs import _event

    run = (await runs_client.post("/v1/runs", json=ECHO)).json()
    SqliteTrackStore(runs_config.runs.track_path).append(TrackEvent(
        run_id=run["id"], event_type="cancel_requested", schema=SCHEMA_VERSION,
        payload={"actor": "bob", "recorded_later": "x"}))
    served = {event["type"]: event["payload"]
              for event in (await runs_client.get(f"/v1/runs/{run['id']}/events")).json()["items"]}
    assert served["cancel_requested"] == {"actor": "bob"}  # a field nobody named is not served
    # An event type nobody named is served with nothing of its payload.
    later = _event(TrackEvent(run_id=run["id"], event_type="a_fact_from_a_later_controller", sequence=99,
                              schema=SCHEMA_VERSION, payload={"anything": "x"}))
    assert later.type == "a_fact_from_a_later_controller" and later.payload == {}


def _sse(text):
    """Server-sent events as (id, event, data) triples."""
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if fields:
            events.append((fields.get("id"), fields.get("event"), json.loads(fields["data"])))
    return events


async def test_a_runs_events_stream_until_it_ends_and_resume_from_the_last_one_seen(runs_client, controller):
    run = (await runs_client.post("/v1/runs", json=ECHO)).json()
    settle(controller)
    streamed = await runs_client.get(f"/v1/runs/{run['id']}/events", headers={"Accept": "text/event-stream"})
    assert streamed.status_code == 200 and streamed.headers["content-type"].startswith("text/event-stream")
    events = _sse(streamed.text)
    assert events[-1][1:] == ("end", {"status": "COMPLETED"})
    ids = [int(event_id) for event_id, kind, _ in events if kind != "end"]
    assert [kind for _, kind, _ in events][:2] == ["op_submitted", "run_picked_up"]
    # A client that lost the connection resumes after the last id it saw: nothing missed, nothing again.
    resumed = await runs_client.get(f"/v1/runs/{run['id']}/events",
                                    headers={"Accept": "text/event-stream", "Last-Event-ID": str(ids[2])})
    assert [int(event_id) for event_id, kind, _ in _sse(resumed.text) if kind != "end"] == ids[3:]


async def test_a_live_run_streams_its_events_as_they_land(runs_client, controller):
    import asyncio

    run = (await runs_client.post("/v1/runs", json=SLOW)).json()
    stream = asyncio.create_task(
        runs_client.get(f"/v1/runs/{run['id']}/events", headers={"Accept": "text/event-stream"}))
    driving = threading.Thread(target=settle, args=(controller,))
    driving.start()
    await asyncio.sleep(0.5)
    assert not stream.done()  # the run is in flight: the stream is still open
    controller.release.set()
    events = _sse((await asyncio.wait_for(stream, 30)).text)
    await asyncio.to_thread(driving.join, 30)
    assert events[-1][1:] == ("end", {"status": "COMPLETED"}) and "step_completed" in [e[1] for e in events]
    assert events[-2][1] == "completed"  # the run's last event, always before `end`


# --- the Track on the hub's own Postgres ----------------------------------------------------------

POSTGRES_URL = __import__("os").environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database")
async def test_without_a_track_path_runs_are_recorded_in_the_hubs_own_database(tmp_path, monkeypatch):
    from collab_hub_execution import PostgresTrackStore
    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=10.0)
    try:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        run_collab_schema_migrations(database)  # the Track's tables come from the hub's migrations
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
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                run = (await client.post("/v1/runs", json=ECHO)).json()
                runner = LifecycleRunner(executor=InMemoryCogExecutor({"echo": lambda e, v, **f: {"echo": v}}),
                                         track=PostgresTrackStore(database))
                settle(RunController(runner, poll_interval=0.01))
                done = (await client.get(f"/v1/runs/{run['id']}")).json()
                assert done["status"] == "COMPLETED", done
    finally:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        database.close()


async def test_the_stream_ends_with_the_runs_last_event_then_end(runs_client, controller, monkeypatch):
    # The stream reads a run's new events and its status in one read, so a run that reads as ended has
    # its last event among those read: `end` never comes before it. Resumed just before that event, the
    # stream sends it and then `end`; and the read that drives both is one query per tick.
    run = (await runs_client.post("/v1/runs", json=ECHO)).json()
    settle(controller)
    service = runs_client._transport.app.state.run_service
    events = service.track.replay(run["id"])
    service.views.view(run["id"])  # read once, as the route does to check who may see it
    reads = []
    real = service.track.replay
    monkeypatch.setattr(service.track, "replay", lambda run_id, **kw: reads.append(kw) or real(run_id, **kw))
    streamed = await runs_client.get(f"/v1/runs/{run['id']}/events",
                                     headers={"Accept": "text/event-stream", "Last-Event-ID": str(events[-2].sequence)})
    assert [kind for _, kind, _ in _sse(streamed.text)] == [events[-1].event_type, "end"]
    # The route's check, then one tick: the run's incremental read, and, the run having ended and let its
    # events go, the tail after the cursor. No separate read to learn whether the run has ended.
    assert reads == [{"after_sequence": events[-1].sequence}] * 2 + [{"after_sequence": events[-2].sequence}]


def test_a_hub_with_a_database_refuses_a_track_file_beside_it(tmp_path):
    # A pod-local SQLite file would win over the hub's database and vanish with the pod: refused at start.
    config = Config.parse({
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"postgres": {"url": "postgresql://collab:collab@127.0.0.1:1/collab"}},
        "runs": {"track_path": str(tmp_path / "track.sqlite")},
    })
    with pytest.raises(RuntimeError, match="unset COLLAB_HUB_API__RUNS__TRACK_PATH"):
        make_app(config)
    assert not (tmp_path / "track.sqlite").exists()
