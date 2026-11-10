"""In-cluster E2E driver: a Cog published by digest, installed, run from its artifact, run again from the cache.

Runs as a Job with the cog-executor ServiceAccount, against a registry in
the cluster and a cache claim every worker pod mounts. It publishes the fake
Cog under ``/cog`` as an artifact, installs it (fetch through the registry,
bind through the Cog's own ``resolve`` over an inventory generated from a
models block, check on a worker pod with the binding delivered), runs it
once from its artifact and asserts the worker was configured from its
delivered binding alone, runs it again and asserts the init container found
the package in the cache, then runs a gated two-step Op on it. Exits 0 on
success, 1 on failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
import urllib.parse

import httpx

from collab_hub_execution import (
    Gate,
    InMemoryInstallStore,
    InMemoryTrackStore,
    LifecycleRunner,
    ModelsBlock,
    OpDefinition,
    OpStep,
    RunBudget,
    RunState,
    inventory,
)
from collab_hub_execution.artifacts import RegistryClient, publish
from collab_hub_execution.controller import BindingDelivery
from collab_hub_execution.installer import Installer, harnesses_installed
from collab_hub_execution.installs import InstallRow, new_install_id
from collab_hub_execution.kubernetes import PULL_CONTAINER, _in_cluster_api

# The served model the inventory is generated from. The fake Cog calls no model: what is asserted is that
# its endpoint, id and key reach the worker through the delivered binding alone.
MODELS = """
[models.fake]
endpoint = "http://fake-model.cogs-e2e.svc.cluster.local:8090/v1"
model = "fake-model"
auth_ref = "env:E2E_MODEL_KEY"
context_window = 8192
max_output_tokens = 1024
locality = "in-cluster"
"""


def pod_log(api, namespace: str, name: str, container: str) -> str:
    """The log of a worker's container, found by the ``app`` label its Deployment gives its pod."""
    selector = urllib.parse.quote(f"app={name}")
    pods = api.get(f"/api/v1/namespaces/{namespace}/pods?labelSelector={selector}").json().get("items", [])
    assert pods, f"no pod of {name}"
    pod = pods[0]["metadata"]["name"]
    return api.get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log?container={container}").text


def wait_for_registry(host: str, within: float = 180.0) -> None:
    """Until the registry's ``/v2/`` answers: its pod is applied beside this Job, and comes up after it."""
    deadline = time.monotonic() + within
    while True:
        try:
            if httpx.get(f"http://{host}/v2/", timeout=5.0).status_code < 500:
                return
        except httpx.HTTPError as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"E2E FAILED: the registry {host} never answered: {exc}")
        time.sleep(2)


def main() -> int:
    namespace = os.environ["E2E_NAMESPACE"]
    runner_image = os.environ["E2E_RUNNER_IMAGE"]
    registry_host = os.environ["E2E_REGISTRY"]
    cache_claim = os.environ["E2E_CACHE_CLAIM"]

    print("== publish the fake Cog to the cluster's registry", flush=True)
    wait_for_registry(registry_host)
    client = RegistryClient(plain_http={registry_host})
    reference = publish("/cog", f"{registry_host}/cogs/echo:e2e", client)
    print(f"published {reference}", flush=True)

    models = ModelsBlock.parse(tomllib.loads(MODELS))
    models.check(os.environ)
    track, installs = InMemoryTrackStore(), InMemoryInstallStore()
    delivery = BindingDelivery(track, installs, models)
    # The durability backend is configuration: none runs the step functions in this process; the location is
    # configuration too: remote, a worker pod per step, pulled from its artifact, through the one switch.
    engine = LifecycleRunner(track=track, budget=RunBudget(max_tokens=35), backend="none", location="remote",
                             controller="op-driver", location_settings={
                                 "image": runner_image, "namespace": namespace,
                                 # Materialize a NetworkPolicy admitting only this driver (the stand-in hub)
                                 # to each worker. kind's default CNI does not enforce NetworkPolicy, so this
                                 # exercises the secure code path; a policy-enforcing CNI enforces it.
                                 "allow_ingress_from": {"app": "op-driver"},
                                 "ready_timeout": 600, "poll_interval": 2, "deliver": delivery,
                                 "cache_claim": cache_claim, "registry_plain_http": [registry_host]})
    executor = engine.executor
    installer = Installer(installs, executor, controller="op-driver", registry=client,
                          inventory=lambda org: inventory(models, harnesses_installed(installs, org)))
    delivery.installer = installer

    print("== install it by digest: fetch, bind through its resolve, check with the binding delivered", flush=True)
    card = {"kind": "context", "requires": [{"capability": "model", "locality": "any"}],
            "ops": {"usage": ["run"], "lifecycle": ["serve", "resolve", "check"]}}
    asked = installs.create(InstallRow(id=new_install_id(), org_id="e2e", reference=str(reference),
                                       digest=reference.digest, cog_id="e2e/echo", card=card, requested_by="e2e"))
    assert installs.claim(asked.id, "op-driver")
    install = installer.install(asked.id)
    print(f"install {install.id}: {install.state} {install.failed_step or ''} {install.failure or ''}", flush=True)
    for event in installs.events(install.id):
        print(f"  - {event.event_type} {json.dumps({k: v for k, v in event.payload.items() if k != 'reference'})}",
              flush=True)
    assert install.invokable, f"the install is {install.state}: {install.failure}"
    assert install.binding["model"]["id"] == "model:fake"
    assert install.binding["model"]["auth_ref"] == "env:E2E_MODEL_KEY"
    assert "e2e-model-key-value" not in json.dumps(install.binding)
    api = _in_cluster_api()

    def run_once(run_id: str) -> tuple[dict, str]:
        step = OpStep("ask", str(reference), "run", {"q": run_id}, digest=reference.digest, install=install.id,
                      binding=install.binding_id, gate=Gate(escalate="never"))
        op = OpDefinition(run_id, (step,))
        status = engine.submit(op)
        print(f"{run_id}: {status}", flush=True)
        for event in track.replay(run_id):
            extra = event.payload.get("error") or event.payload.get("reason") or ""
            print(f"  - {event.event_type} {extra}".rstrip(), flush=True)
        assert status is RunState.COMPLETED, f"{run_id} ended {status}"
        [started] = [e.payload for e in track.replay(run_id) if e.event_type == "worker_started"]
        assert started["artifact"] == str(reference) and started["binding_id"] == install.binding_id
        assert started["cache"] == f"claim:{cache_claim}"
        [completed] = [e.payload for e in track.replay(run_id) if e.event_type == "step_completed"]
        return completed, started["name"]

    print("== run it once from its artifact", flush=True)
    completed, _ = run_once("e2e-first")
    payload = completed["payload"]
    # The worker was configured from its delivered binding alone: the file the hub mounted names the model and
    # the key by reference, the key reached the environment, and nothing else of the model is in the pod.
    assert payload["binding"]["id"] == install.binding_id and payload["binding"]["model"] == "fake-model"
    assert payload["binding"]["endpoint"].startswith("http://fake-model.") and payload["binding"]["key_present"]
    assert payload["binding"]["auth_ref"] == "env:COLLAB_MODEL_API_KEY"
    assert "COLLAB_BINDING_FILE" in payload["environment"] and "COLLAB_MODEL_API_KEY" in payload["environment"]
    assert "COLLAB_REGISTRY_CREDENTIAL" not in payload["environment"]  # the pull's credential never reaches the Cog
    assert completed["binding_id"] == install.binding_id and completed["binding"]["id"] == install.binding_id
    assert "e2e-model-key-value" not in json.dumps([e.payload for e in track.replay("e2e-first")])

    print("== run it again: the init container finds the package of this lock in the cache", flush=True)
    worker_logs: dict[str, str] = {}
    materialize = executor.materialize

    def materialize_and_read(cog, run_id, instance=""):
        worker = materialize(cog, run_id, instance)
        worker_logs[run_id] = pod_log(api, namespace, worker.name, PULL_CONTAINER)
        return worker

    executor.materialize = materialize_and_read
    completed, _ = run_once("e2e-second")
    pull_log = worker_logs["e2e-second"]
    print(pull_log, flush=True)
    assert "artifact cached:" in pull_log, "the second run pulled the artifact again"
    assert "artifact pulling:" not in pull_log

    print("== a gated two-step Op on the installed Cog", flush=True)
    op = OpDefinition("e2e-run", (
        OpStep("research", str(reference), "run", {"topic": "kind e2e"}, digest=reference.digest,
               install=install.id, binding=install.binding_id),
        OpStep("review", str(reference), "run", {"draft": "v1"}, digest=reference.digest, install=install.id,
               binding=install.binding_id, gate=Gate(escalate="always")),
    ))
    status = engine.submit(op)
    assert status is RunState.WAITING_AT_GATE, f"expected WAITING_AT_GATE, got {status}"
    first = engine.open_escalation("e2e-run")["escalation"]
    status = engine.decide("e2e-run", escalation=first, actor="e2e", outcome="send_back", findings=["cite a source"])
    assert status is RunState.WAITING_AT_GATE, f"expected the revision at the Gate, got {status}"
    revision = engine.open_escalation("e2e-run")["escalation"]
    assert revision != first, "a revision is a new escalation"
    status = engine.decide("e2e-run", escalation=revision, actor="e2e", outcome="approve")
    assert status is RunState.COMPLETED, f"expected COMPLETED after approval, got {status}"
    events = [e.event_type for e in track.replay("e2e-run")]
    print("track events:", events, flush=True)
    for required in ("op_submitted", "materialized", "gate_escalated", "gate_decided", "step_completed", "completed"):
        assert required in events, f"missing {required!r} in Track"
    started = [e.payload for e in track.replay("e2e-run") if e.event_type == "worker_started"]
    assert started and all(s["location"] == "remote" and len(s["run_token_sha256"]) == 64 for s in started)
    assert all(s["binding_id"] == install.binding_id for s in started)
    review = next(e.payload["payload"] for e in track.replay("e2e-run")
                  if e.event_type == "step_completed" and e.payload["step"] == "review")
    assert review["echo"] == {"draft": "v1"} and review["signal"] == ["cite a source"]
    assert sum(e.payload["usage"]["tokens"] for e in track.replay("e2e-run")
               if e.event_type == "interaction_usage") == 30

    print("== uninstall: every workload of the install is swept", flush=True)
    installs.request_uninstall(install.id, "e2e")
    installer.uninstall(install.id)
    assert installs.get(install.id).state == "uninstalled"
    print("E2E OK: a published Cog installed by digest, run from its artifact with its delivered binding, its "
          "environment found in the cache on the second run, a Gate sent a step back and approved it",
          flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as exc:
        print(f"E2E FAILED: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
