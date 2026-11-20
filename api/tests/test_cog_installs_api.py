"""Installs (issue #105): ``POST``, ``GET`` and ``DELETE /v1/cogs/installs``, and running an installed Cog once.

The API records the ask on the install store beside the SQLite Track and
reads the state back; a run controller over the same store, here with an
in-memory executor whose Cog answers ``resolve`` and ``check``, moves the
install through fetched, bound and invokable. The two never call each other.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio
from collab_hub_execution import (
    InMemoryCogExecutor,
    LifecycleRunner,
    ModelsBlock,
    ResultEnvelope,
    SqliteTrackStore,
    inventory,
)
from collab_hub_execution.artifacts import RegistryClient, publish
from collab_hub_execution.controller import BindingDelivery, RunController
from collab_hub_execution.installer import Installer, harnesses_installed
from collab_hub_execution.installs import SqliteInstallStore
from httpx import ASGITransport, AsyncClient
from test_cog_serving import bearer
from test_cogs_api import AUTH, HOST, SOURCE, T0, _jwt, fixture_card, row
from test_runs_api import package

from collab_hub_api.cogs.catalog import CogArtifact, InMemoryCogCatalogStore, card_search_fields
from collab_hub_api.config import Config
from collab_hub_api.core import make_app

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "execution" / "tests"))
from fake_registry import FakeRegistry  # noqa: E402  - the execution suite's registry, after the path is set

MODELS = ModelsBlock.parse(
    {"models": {"fake": {"endpoint": "http://127.0.0.1:8090/v1", "model": "fake-model", "auth_ref": "env:FAKE_KEY"}}}
)
ENVIRON = {"FAKE_KEY": "fake-secret"}
OTHER = {"IdToken-test": _jwt({"preferred_username": "bob", "org_id": "org-b", "workspace_id": "workspace-b"})}


@pytest.fixture
def registry():
    registry = FakeRegistry().start()
    yield registry
    registry.stop()


@pytest.fixture
def config(tmp_path) -> Config:
    package(tmp_path / "cogs", "echo")
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"mcp_session_manager_enabled": False},
            "tasks": {"backend": "memory"},
            "cogs": {"catalog": {"backend": "memory"}},
            "runs": {"track_path": str(tmp_path / "track.sqlite"), "packages": [str(tmp_path / "cogs")]},
        }
    )


@pytest_asyncio.fixture
async def api(config, monkeypatch, registry, tmp_path):
    """The client, the seeded catalog, and the artifact the catalog's context Cog is published as."""
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(config)
    async with app.router.lifespan_context(app):
        store: InMemoryCogCatalogStore = app.state.cog_catalog_store
        directory = tmp_path / "bundle"
        directory.mkdir()
        (directory / "pixi.toml").write_text('[workspace]\nname = "notes"\n\n[tasks]\nserve = "python serve.py"\n')
        (directory / "pixi.lock").write_text("version: 6\n")
        (directory / "COG.md").write_text("---\nname: cog-meeting-notes\n---\n")
        client = RegistryClient(plain_http={registry.host})
        reference = publish(directory, f"{registry.host}/cogs/cog-meeting-notes:1", client)
        card = fixture_card("pixi-context")
        store.upsert(row("4", card, repository="cogs/cog-meeting-notes", host=registry.host))
        # The row's digest is the artifact's, so the reference the API pins is what the registry holds.
        store.upsert(
            CogArtifact(
                source_id=SOURCE,
                host=registry.host,
                repository="cogs/cog-meeting-notes",
                digest=reference.digest,
                status="indexed",
                tags=("1",),
                pushed_at=T0,
                card=card,
                **card_search_fields(card),
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=AUTH) as client:
            yield client, store, reference


def selecting(check_ok=True):
    def handle(entry_point, value, **feedback):
        if entry_point == "resolve":
            chosen = {
                r["capability"]: next(s["id"] for s in value["inventory"] if r["capability"] in s["provides"])
                for r in value["requires"]
            }
            return {"selections": chosen}
        if entry_point == "check":
            return {"probed": True} if check_ok else ResultEnvelope.failure("model-unavailable", "503")
        return {"answer": "hi"}

    return handle


@pytest.fixture
def controller(config, registry):
    """A controller over the API's Track and install store, with an in-memory executor and the fake registry."""

    def make(check_ok=True):
        track = SqliteTrackStore(config.runs.track_path)
        installs = SqliteInstallStore(config.runs.track_path)
        delivery = BindingDelivery(track, installs, MODELS, environ=ENVIRON)

        class Delivering(InMemoryCogExecutor):
            def materialize(self, cog, run_id, instance=""):
                self.given = delivery(cog, run_id, instance)
                return super().materialize(cog, run_id, instance)

        executor = Delivering({})
        executor.handlers = _Any(selecting(check_ok))
        runner = LifecycleRunner(executor=executor, track=track, controller="one")
        install = Installer(
            installs,
            executor,
            controller="one",
            environ=ENVIRON,
            registry=RegistryClient(plain_http={registry.host}),
            inventory=lambda org: inventory(MODELS, harnesses_installed(installs, org)),
        )
        delivery.installer = install
        controller = RunController(runner, poll_interval=0.01, installer=install)
        controller.executor = executor
        return controller

    return make


class _Any(dict):
    def __init__(self, handler):
        self.handler = handler

    def __getitem__(self, key):
        return self.handler


def settle(controller, until=lambda: True, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        controller.tick()
        if until() and controller.idle():
            return
        time.sleep(0.01)
    raise AssertionError("the controller did not settle")


async def test_an_install_is_asked_for_moved_by_the_controller_and_read_back_with_its_events(api, controller):
    client, _, reference = api
    asked = await client.post("/v1/cogs/installs", json={"cog_id": "example/cog-meeting-notes"})
    assert asked.status_code == 202, asked.text
    install = asked.json()
    assert install["state"] == "published" and install["reference"] == str(reference)
    assert install["digest"] == reference.digest and install["cog_id"] == "example/cog-meeting-notes"
    assert install["kind"] == "context" and install["requested_by"] == "alice" and not install["invokable"]
    # The API started nothing: the install moves only once a controller reads the store.
    assert (await client.get(f"/v1/cogs/installs/{install['id']}")).json()["state"] == "published"
    assert (await client.get("/v1/runs/launchable")).json()["items"] == ["echo"]
    one = controller()
    settle(one, until=lambda: True)
    done = (await client.get(f"/v1/cogs/installs/{install['id']}")).json()
    assert done["state"] == "invokable" and done["invokable"] and done["picked_up_by"] == "one", done
    assert [e["type"] for e in done["events"]] == ["install_fetched", "binding_admitted", "check_passed"]
    assert done["binding"]["model"]["id"] == "model:fake" and done["binding_id"] == done["binding"]["id"]
    assert "fake-secret" not in json.dumps(done)
    assert done["lock_digest"].startswith("sha256:")
    listed = (await client.get("/v1/cogs/installs")).json()
    assert [i["id"] for i in listed["items"]] == [install["id"]] and listed["items"][0]["events"] is None
    invokable = (await client.get("/v1/cogs/installs", params={"state": "invokable"})).json()["items"]
    assert [i["id"] for i in invokable] == [install["id"]]
    assert (await client.get("/v1/cogs/installs", params={"state": "bound"})).json()["items"] == []
    assert (await client.get("/v1/runs/launchable")).json()["items"] == [str(reference), "echo"]
    # The same digest again is the install that holds it; another organization's ask is its own.
    again = await client.post("/v1/cogs/installs", json={"reference": str(reference)})
    assert again.status_code == 409 and again.json()["error"]["details"]["install"] == install["id"]
    theirs = await client.post("/v1/cogs/installs", json={"reference": str(reference)}, cookies=OTHER)
    assert theirs.status_code == 202 and theirs.json()["id"] != install["id"]
    assert (await client.get(f"/v1/cogs/installs/{install['id']}", cookies=OTHER)).status_code == 404


async def test_a_failing_check_leaves_the_install_bound_and_says_which_step_failed(api, controller):
    client, _, reference = api
    install = (await client.post("/v1/cogs/installs", json={"reference": str(reference)})).json()
    settle(controller(check_ok=False))
    stuck = (await client.get(f"/v1/cogs/installs/{install['id']}")).json()
    assert stuck["state"] == "bound" and not stuck["invokable"]
    assert stuck["failed_step"] == "check" and stuck["failure"] == "check: model-unavailable: 503"
    assert [e["type"] for e in stuck["events"]][-1] == "check_failed"
    # A run on it is refused as a conflict with the install's state, naming the install.
    refused = await client.post("/v1/runs", json={"cog": str(reference), "entry_point": "ask"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "cog_not_installed"
    assert "bound, not invokable: check: model-unavailable: 503" in refused.json()["error"]["message"]
    assert refused.json()["error"]["details"]["installs"] == {str(reference): install["id"]}


async def test_an_installed_cog_runs_once_with_its_binding_and_an_uninstalled_digest_is_refused(api, controller):
    client, _, reference = api
    refused = await client.post("/v1/runs", json={"cog": str(reference), "entry_point": "ask", "input": {"q": 1}})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "cog_not_launchable"
    assert "not installed" in refused.json()["error"]["details"]["refused"][str(reference)]
    install = (await client.post("/v1/cogs/installs", json={"reference": str(reference)})).json()
    one = controller()
    settle(one)
    install = (await client.get(f"/v1/cogs/installs/{install['id']}")).json()
    assert install["invokable"]
    for named_by in (str(reference), reference.digest, install["id"]):
        created = await client.post(
            "/v1/runs", json={"cog": named_by, "entry_point": "ask", "input": {"q": 1}, "gate": {"escalate": "never"}}
        )
        assert created.status_code == 201, created.text
        run = created.json()
        [step] = run["steps"]
        assert step == {
            "name": "cog-meeting-notes",
            "cog": str(reference),
            "entry_point": "ask",
            "state": "pending",
            "attempt": None,
            "error": None,
            "output": None,
            "output_ref": None,
        }
        settle(one, until=lambda: True)
        done = (await client.get(f"/v1/runs/{run['id']}")).json()
        assert done["status"] == "COMPLETED", done
        events = (await client.get(f"/v1/runs/{run['id']}/events")).json()["items"]
        started = next(e for e in events if e["type"] == "step_started")
        assert started["payload"]["install"] == install["id"]
        assert started["payload"]["binding_id"] == install["binding_id"]
        assert started["payload"]["digest"] == reference.digest
        completed = next(e for e in events if e["type"] == "step_completed")
        assert completed["payload"]["binding_id"] == install["binding_id"]
        # The worker was delivered the binding file and the key, which the Track never holds.
        given = one.executor.given
        assert json.loads(given.files["binding.json"])["id"] == install["binding_id"]
        assert given.env == {"COLLAB_MODEL_API_KEY": "fake-secret"}
        assert "fake-secret" not in json.dumps(events)
    # A multi-step Op may name the installed Cog beside a package, and the run-one form takes one shape only.
    mixed = await client.post(
        "/v1/runs",
        json={
            "steps": [
                {"name": "ask", "cog": str(reference), "entry_point": "ask"},
                {"name": "echo", "cog": "echo", "entry_point": "run"},
            ]
        },
    )
    assert mixed.status_code == 201
    for bad in (
        {"cog": str(reference)},
        {"cog": str(reference), "entry_point": "ask", "steps": []},
        {"steps": [{"name": "a", "cog": "echo", "entry_point": "run"}], "input": 1},
    ):
        assert (await client.post("/v1/runs", json=bad)).status_code == 422, bad
    # Another organization's install is not this one's to run.
    theirs = await client.post("/v1/runs", json={"cog": install["id"], "entry_point": "ask"}, cookies=OTHER)
    assert theirs.status_code == 422


async def test_an_uninstall_is_asked_for_and_the_controller_removes_the_install(api, controller):
    client, _, reference = api
    install = (await client.post("/v1/cogs/installs", json={"reference": str(reference)})).json()
    one = controller()
    settle(one)
    assert (await client.delete(f"/v1/cogs/installs/{install['id']}", cookies=OTHER)).status_code == 404
    asked = await client.delete(f"/v1/cogs/installs/{install['id']}")
    assert asked.status_code == 202 and asked.json()["uninstall_requested_by"] == "alice"
    settle(one)
    gone = (await client.get(f"/v1/cogs/installs/{install['id']}")).json()
    assert gone["state"] == "uninstalled" and [e["type"] for e in gone["events"]][-1] == "uninstalled"
    assert (await client.get("/v1/cogs/installs")).json()["items"] == []
    kept = (await client.get("/v1/cogs/installs", params={"include_uninstalled": "true"})).json()["items"]
    assert [i["id"] for i in kept] == [install["id"]]
    assert (await client.delete(f"/v1/cogs/installs/{install['id']}")).status_code == 409
    refused = await client.post("/v1/runs", json={"cog": str(reference), "entry_point": "ask"})
    assert refused.status_code == 422
    assert (await client.get("/v1/cogs/installs/inst-000000000000")).status_code == 404


async def test_what_the_catalog_does_not_hold_cannot_be_installed(api):
    client, store, reference = api
    missing = await client.post("/v1/cogs/installs", json={"cog_id": "example/absent"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "cog_not_found"
    version = await client.post(
        "/v1/cogs/installs", json={"cog_id": "example/cog-meeting-notes", "digest": "sha256:" + "9" * 64}
    )
    assert version.status_code == 404 and version.json()["error"]["code"] == "cog_version_not_found"
    unknown = await client.post("/v1/cogs/installs", json={"reference": f"{HOST}/cogs/x@sha256:" + "8" * 64})
    assert unknown.status_code == 404
    bad = await client.post("/v1/cogs/installs", json={"reference": "not-a-reference"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "reference_invalid"
    for body in ({}, {"cog_id": "a", "reference": "b"}, {"digest": "sha256:" + "1" * 64}):
        assert (await client.post("/v1/cogs/installs", json=body)).status_code == 422, body
    pinned = await client.post(
        "/v1/cogs/installs", json={"cog_id": "example/cog-meeting-notes", "digest": reference.digest}
    )
    assert pinned.status_code == 202 and pinned.json()["digest"] == reference.digest


async def test_a_development_package_installs_from_the_directory_source(api, controller):
    client, _, _ = api
    refused = await client.post("/v1/cogs/installs", json={"package": "absent"})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "cog_not_launchable"
    asked = await client.post("/v1/cogs/installs", json={"package": "echo"})
    assert asked.status_code == 202 and asked.json()["reference"] == "echo" and asked.json()["kind"] == "package"
    assert asked.json()["digest"].startswith("sha256:")


async def test_on_a_hub_that_resolves_memberships_owners_and_operators_install_and_a_member_is_refused(
    config, monkeypatch, tmp_path
):
    """Installing runs the Cog's `resolve` and `check` with the hub's model key delivered, so it takes the
    organization's owner role, or the platform operator's, where the hub has roles at all."""
    from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
    from collab_hub_api.frames.org_source import ORG_SOURCE_ENV
    from collab_hub_api.frames.orgs import ROLE_MEMBER, ROLE_OWNER

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    values = {
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"mcp_session_manager_enabled": False, "orgs": {"backend": "memory"}},
        "tasks": {"backend": "memory"},
        "cogs": {"catalog": {"backend": "memory"}},
        "runs": {"track_path": str(tmp_path / "track.sqlite")},
    }
    app = make_app(Config.parse(values))
    reference = f"{HOST}/cogs/notes@sha256:{'0' * 64}"
    async with app.router.lifespan_context(app):
        orgs = app.state.org_store
        orgs.set_membership("sub-owner", "org-1", role=ROLE_OWNER)
        orgs.set_membership("sub-member", "org-1", role=ROLE_MEMBER)
        orgs.set_platform_role("sub-operator")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            member, owner = bearer({"sub": "sub-member"}), bearer({"sub": "sub-owner"})
            operator = bearer({"sub": "sub-operator"})
            refused = await client.post("/v1/cogs/installs", json={"reference": reference}, headers=member)
            assert refused.status_code == 403 and refused.json()["error"]["code"] == "install_forbidden"
            assert (await client.delete("/v1/cogs/installs/inst-000000000000", headers=member)).status_code == 403
            # Past the gate: the catalog holds nothing at that digest, which is the owner's and the operator's answer.
            assert (
                await client.post("/v1/cogs/installs", json={"reference": reference}, headers=owner)
            ).status_code == 404
            assert (
                await client.get("/v1/cogs/installs", headers=member)
            ).status_code == 200  # reading is every member's
            operator_asked = await client.post("/v1/cogs/installs", json={"reference": reference}, headers=operator)
            assert operator_asked.status_code != 403


def test_the_install_routes_sit_under_the_cogs_prefix_and_before_the_catalogs_cog_id_route(config):
    app = make_app(config)
    paths = [route.path for route in app.routes if route.path.startswith("/v1/cogs")]
    assert paths.index("/v1/cogs/installs") < paths.index("/v1/cogs/{cog_id:path}")


async def test_without_an_install_store_the_routes_say_so(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(
        Config.parse(
            {
                "storage": {"frames_path": str(tmp_path / "frames")},
                "frames": {"mcp_session_manager_enabled": False},
                "tasks": {"backend": "memory"},
                "cogs": {"catalog": {"backend": "memory"}},
            }
        )
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=AUTH) as client:
            answer = await client.post("/v1/cogs/installs", json={"cog_id": "x/y"})
            assert answer.status_code == 503 and answer.json()["error"]["code"] == "runs_unavailable"


def test_the_install_router_reaches_the_execution_package_through_the_run_router_alone():
    import ast

    source = Path(__file__).resolve().parents[1] / "src" / "collab_hub_api" / "routers" / "installs.py"
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            assert not any(name.startswith("collab_hub_execution") for name in names), names


@pytest.mark.usefixtures("api")
def test_nothing_in_the_api_waits_on_a_thread_of_its_own():
    assert not [t for t in threading.enumerate() if t.name.startswith("install-")]
