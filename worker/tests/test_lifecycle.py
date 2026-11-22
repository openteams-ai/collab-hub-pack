"""The lifecycle entry points the SDK answers for every Cog: ``resolve`` over the hub's inventory, and ``check``."""

from __future__ import annotations

import json

from conftest import fake_agent_command

from collab_hub_cog_worker import AcpHarnessAdapter, Harness, Task, Worker, load_binding, select_satisfiers

MODEL = {"id": "model:fake", "kind": "model", "provides": ["model", "model-endpoint/openai-compatible"],
         "locality": "in-cluster", "endpoint": "http://fake/v1", "model": "fake-model", "auth_ref": "env:K"}
EXTERNAL = {"id": "model:claude", "kind": "model", "provides": ["model", "model-endpoint/anthropic"],
            "locality": "external", "provider": "anthropic", "model": "claude-opus-5-5"}
HERMES = {"id": "harness:inst-1", "kind": "harness", "provides": ["harness"], "locality": "in-cluster",
          "cog_id": "openteams/hermes", "reference": "hub/cogs/hermes@sha256:" + "a" * 64}


def test_the_default_selection_takes_the_first_satisfier_of_each_capability_that_its_locality_allows():
    requires = [{"capability": "model", "locality": "any"}, {"capability": "harness"}]
    assert select_satisfiers(requires, [MODEL, EXTERNAL, HERMES]) == {
        "selections": {"model": "model:fake", "harness": "harness:inst-1"}, "unsatisfied": []}
    # The locality constrains: an in-cluster requirement skips the external model.
    assert select_satisfiers([{"capability": "model-endpoint/anthropic", "locality": "in-cluster"}],
                             [MODEL, EXTERNAL]) == {"selections": {}, "unsatisfied": ["model-endpoint/anthropic"]}
    assert select_satisfiers([{"capability": "model-endpoint/anthropic", "locality": "external"}],
                             [MODEL, EXTERNAL])["selections"] == {"model-endpoint/anthropic": "model:claude"}
    # The Cogs a requirement names are preferred, and required when a candidate names its Cog.
    assert select_satisfiers([{"capability": "harness", "satisfiers": ["openteams/hermes"]}],
                             [HERMES])["selections"] == {"harness": "harness:inst-1"}
    assert select_satisfiers([{"capability": "harness", "satisfiers": ["acme/pi"]}], [HERMES]) == {
        "selections": {}, "unsatisfied": ["harness"]}


def test_resolve_is_answered_without_starting_the_harness_and_check_starts_it_against_the_binding(tmp_path):
    started = []

    def harness(context):
        started.append(context.binding.record())
        return AcpHarnessAdapter(fake_agent_command())

    environ = {"COLLAB_RUN_TOKEN": "t", "COLLAB_COG_ID": "ctx", "COLLAB_RUN_ID": "install:inst-1"}
    worker = Worker("ctx", "0.1.0", environ=environ, harness=harness)

    @worker.task("ask")
    def ask(task: Task, harness: Harness):
        return harness.interact("hi")

    status, envelope = worker.invoke("resolve", {"requires": [{"capability": "model"}], "inventory": [MODEL]})
    assert status == 200 and envelope["ok"] and envelope["payload"] == {
        "selections": {"model": "model:fake"}, "unsatisfied": []}
    assert started == []  # resolve runs before a binding exists: no harness was started
    assert envelope["binding"] is None
    status, envelope = worker.invoke("resolve", {"inventory": "no"})
    assert status == 422 and envelope["error"]["code"] == "invalid-input"

    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"id": "bnd-1", "provider": "openai-compatible", "endpoint": "http://fake/v1",
                                "model": "fake-model", "auth_ref": "env:COLLAB_MODEL_API_KEY",
                                "harness": {"satisfier": "harness:inst-1", "reference": HERMES["reference"]}}))
    bound = Worker("ctx", "0.1.0", harness=harness,
                   environ={**environ, "COLLAB_BINDING_FILE": str(path), "COLLAB_MODEL_API_KEY": "sk-secret-value"})
    status, envelope = bound.invoke("check", {})
    assert status == 200 and envelope["ok"], envelope
    assert envelope["payload"]["harness"] == "the agent" and envelope["payload"]["entry_points"] == []
    assert envelope["payload"]["binding"]["id"] == "bnd-1" and "sk-secret-value" not in json.dumps(envelope)
    assert envelope["binding"]["id"] == "bnd-1" and envelope["binding"]["harness"]["satisfier"] == "harness:inst-1"
    assert started and started[-1]["id"] == "bnd-1"  # check started the harness against the delivered binding


def test_a_cog_s_own_resolve_or_check_replaces_the_built_in_one():
    worker = Worker("own", environ={"COLLAB_RUN_TOKEN": "t"})

    @worker.task("resolve")
    def resolve(task: Task, harness):
        return {"selections": {"model": "model:mine"}}

    @worker.task("check")
    def check(task: Task, harness):
        return {"probed": True}

    assert worker.invoke("resolve", {"requires": [], "inventory": []})[1]["payload"] == {
        "selections": {"model": "model:mine"}}
    assert worker.invoke("check", {})[1]["payload"] == {"probed": True}


def test_a_binding_file_carries_the_record_s_id_and_harness_into_the_envelope(tmp_path):
    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"id": "bnd-2", "endpoint": "http://m/v1", "model": "m",
                                "harness": {"reference": "r", "digest": "d"}}))
    binding = load_binding({"COLLAB_BINDING_FILE": str(path)})
    assert binding.id == "bnd-2" and binding.harness == {"reference": "r", "digest": "d"} and binding.bound
    assert binding.record()["id"] == "bnd-2" and binding.record()["harness"] == {"reference": "r", "digest": "d"}
    harness_only = tmp_path / "harness.json"
    harness_only.write_text(json.dumps({"id": "bnd-3", "harness": {"reference": "r"}}))
    assert load_binding({"COLLAB_BINDING_FILE": str(harness_only)}).bound
