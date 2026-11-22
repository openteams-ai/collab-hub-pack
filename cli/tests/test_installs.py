"""Installing a Cog by digest from the terminal: `cog install`, `cog installs`, `cog show-install`, `cog uninstall`."""

from __future__ import annotations

import json

import pytest

from collab_hub_cli import main

from .conftest import HUB

COG = {"cog_id": "acme/notes", "digest": "sha256:" + "4" * 64, "version": "0.1.0",
       "reference": "hub.test/cogs/notes@sha256:" + "4" * 64, "repository": "cogs/notes", "tags": ["0.1.0"],
       "card": {"name": "notes", "kind": "context", "description": "Notes"}}


@pytest.fixture(autouse=True)
def dev_hub(stub, monkeypatch):
    stub.dev_auth = True
    stub.cogs = [COG]
    monkeypatch.setattr(main, "POLL_SECONDS", 0)
    return stub


def test_cog_install_asks_by_id_by_pinned_version_or_by_reference_and_prints_the_install(stub, cli):
    result = cli("--hub", HUB, "cog", "install", "acme/notes")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "inst-000000000001"
    assert "Installing acme/notes as inst-000000000001" in result.stderr
    pinned = cli("--hub", HUB, "cog", "install", "acme/notes@sha256:" + "4" * 64, "--json")
    assert pinned.exit_code == 0 and json.loads(pinned.stdout)["state"] == "published"
    by_reference = cli("--hub", HUB, "cog", "install", COG["reference"])
    assert by_reference.exit_code == 0
    package = cli("--hub", HUB, "cog", "install", "echo", "--package")
    assert package.exit_code == 0
    bodies = [json.loads(r.content) for r in stub.requests if r.url.path == "/v1/cogs/installs" and r.method == "POST"]
    assert bodies == [{"cog_id": "acme/notes"}, {"cog_id": "acme/notes", "digest": "sha256:" + "4" * 64},
                      {"reference": COG["reference"]}, {"package": "echo"}]
    missing = cli("--hub", HUB, "cog", "install", "acme/nope")
    assert missing.exit_code == 1 and "Cog not found (HTTP 404)" in missing.stderr


def test_cog_install_wait_follows_the_install_to_invokable_or_to_the_step_that_failed(stub, cli):
    stub.install_progress["inst-000000000001"] = ["fetched", "bound", "invokable"]
    result = cli("--hub", HUB, "cog", "install", "acme/notes", "--wait")
    assert result.exit_code == 0, result.output
    assert "inst-000000000001: fetched" in result.stderr and "inst-000000000001: invokable" in result.stderr
    assert "state      invokable" in result.stdout and "model      model:fake" in result.stdout
    assert "install_fetched" in result.stdout
    stub.install_progress["inst-000000000002"] = ["fetched", "bound", "check failed"]
    stuck = cli("--hub", HUB, "cog", "install", "acme/notes", "--wait")
    assert stuck.exit_code == 1 and "check failed" in stuck.stderr
    assert "failed     check: model-unavailable: 503" in stuck.stdout


def test_cog_installs_lists_and_show_install_prints_one(stub, cli):
    assert cli("--hub", HUB, "cog", "installs").stderr.strip() == "No installs."
    cli("--hub", HUB, "cog", "install", "acme/notes")
    stub.install_progress["inst-000000000001"] = ["invokable"]
    cli("--hub", HUB, "cog", "show-install", "inst-000000000001")
    listed = cli("--hub", HUB, "cog", "installs")
    assert listed.exit_code == 0 and listed.stdout.splitlines()[1].split()[:3] == [
        "inst-000000000001", "acme/notes", "invokable"]
    as_json = cli("--hub", HUB, "cog", "installs", "--json", "--state", "invokable", "--all")
    assert [i["id"] for i in json.loads(as_json.stdout)] == ["inst-000000000001"]
    [listing] = [r for r in stub.requests if r.url.path == "/v1/cogs/installs" and r.url.params.get("state")]
    assert listing.url.params["state"] == "invokable" and listing.url.params["include_uninstalled"] == "true"
    shown = cli("--hub", HUB, "cog", "show-install", "inst-000000000001")
    assert shown.exit_code == 0 and "binding    bnd-0000000000000001" in shown.stdout
    missing = cli("--hub", HUB, "cog", "show-install", "inst-000000000009")
    assert missing.exit_code == 1 and "No install inst-000000000009" in missing.stderr


def test_cog_uninstall_asks_and_waits_for_the_removal(stub, cli):
    cli("--hub", HUB, "cog", "install", "acme/notes")
    result = cli("--hub", HUB, "cog", "uninstall", "inst-000000000001", "--wait")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "uninstalled" and "Uninstalling inst-000000000001" in result.stderr
    [deleted] = [r for r in stub.requests if r.method == "DELETE"]
    assert deleted.url.path == "/v1/cogs/installs/inst-000000000001"


def test_cog_launch_runs_an_installed_cog_through_the_run_one_form(stub, cli):
    stub.launchable = (COG["reference"], "echo")
    result = cli("--hub", HUB, "cog", "launch", COG["reference"], "--entry", "ask", "--input", '{"q": 1}')
    assert result.exit_code == 0, result.output
    [request] = [r for r in stub.requests if r.url.path == "/v1/runs"]
    assert json.loads(request.content) == {"cog": COG["reference"], "entry_point": "ask", "input": {"q": 1},
                                           "gate": {"escalate": "error"}}
    stub.launchable = ("inst-000000000001",)
    by_id = cli("--hub", HUB, "cog", "launch", "inst-000000000001", "--entry", "ask")
    assert by_id.exit_code == 0
    assert json.loads(stub.requests[-1].content)["cog"] == "inst-000000000001"
