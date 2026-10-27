"""The remote location: a Cog worker as a pod the run controller makes, behind the same switch as local.

No cluster: the Kubernetes API and the worker's HTTP are faked, as the executor's
own tests fake them. What the pod is given, what the Track records of it, how
the controller reaches it with the run token and for turns, and how a pod
that outlived its controller is reaped.
"""

from __future__ import annotations

from test_kubernetes_executor import FakeK8sApi, FakeWorkerHttp

from collab_hub_execution import InMemoryTrackStore, LifecycleRunner, OpDefinition, OpStep, RunState, intents
from collab_hub_execution.locations import location_of, select_executor
from collab_hub_execution.run_tokens import digest

BY = {"user": "alice", "org_id": "acme"}


def _remote(api, http, **settings):
    settings.setdefault("insecure_skip_network_policy", True)
    return select_executor("remote", image="ghcr.io/openteams-ai/collab-hub-pack/api:test", namespace="hub", api=api,
                           worker_http=http, poll_interval=0, controller="hub", **settings)


def _runner(track, api, http, **settings):
    executor = _remote(api, http, **settings)
    return LifecycleRunner(executor=executor, track=track, controller="hub"), executor


def test_a_run_at_the_remote_location_materializes_a_pod_of_the_hubs_image_running_the_cogs_serve():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    track = InMemoryTrackStore()
    deliver = lambda cog, run_id, instance: {"COLLAB_MODEL_BASE_URL": "http://model/v1"}  # noqa: E731
    runner, executor = _runner(track, api, http, deliver=deliver)
    assert location_of(executor) == "remote"
    assert runner.submit(OpDefinition("r", (OpStep("a", "hermes", "ask", {"prompt": "hi"}),))) is RunState.COMPLETED
    [(path, deployment)] = [(p, b) for p, b in api.bodies if p.endswith("/deployments")]
    pod = deployment["spec"]["template"]["spec"]
    [container] = pod["containers"]
    assert container["image"] == "ghcr.io/openteams-ai/collab-hub-pack/api:test"
    assert container["command"] == ["pixi", "run", "--locked", "--manifest-path",
                                    "/opt/collab-hub/cogs/hermes/pixi.toml", "serve"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["COLLAB_COG_ID"] == "hermes" and env["COLLAB_RUN_ID"] == "r" and env["COLLAB_COG_HOST"] == "0.0.0.0"
    assert env["COLLAB_COG_PORT"] == "8080" and env["COLLAB_MODEL_BASE_URL"] == "http://model/v1"
    assert pod["automountServiceAccountToken"] is False  # a worker never talks to the API server
    assert {v["name"] for v in pod["volumes"]} == {"cache", "tmp"}
    assert {m["mountPath"] for m in container["volumeMounts"]} == {"/var/cache/collab-hub", "/tmp"}
    assert deployment["metadata"]["labels"]["collab-hub/controller"] == "hub"
    # The controller presented the worker's run token; the Track names it only by its hash, with the pod.
    [started] = [e.payload for e in track.replay("r") if e.event_type == "worker_started"]
    assert started["location"] == "remote" and started["name"] == deployment["metadata"]["name"]
    assert started["namespace"] == "hub" and "run_token" not in started
    assert http.headers[-1]["Authorization"] == f"Bearer {env['COLLAB_RUN_TOKEN']}"
    assert started["run_token_sha256"] == digest(env["COLLAB_RUN_TOKEN"])
    # Torn down after the step: the Deployment and the Service, by name.
    assert ("DELETE", f"/apis/apps/v1/namespaces/hub/deployments/{started['name']}") in api.calls
    assert ("DELETE", f"/api/v1/namespaces/hub/services/{started['name']}") in api.calls


def test_an_image_of_another_shape_runs_the_command_it_is_given():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    runner, _ = _runner(InMemoryTrackStore(), api, http, worker_command=["python", "-m", "e2e.runner"])
    assert runner.submit(OpDefinition("r", (OpStep("a", "x", "run"),))) is RunState.COMPLETED
    [(_, deployment)] = [(p, b) for p, b in api.bodies if p.endswith("/deployments")]
    assert deployment["spec"]["template"]["spec"]["containers"][0]["command"] == ["python", "-m", "e2e.runner"]


def test_a_turn_reaches_a_remote_worker_with_the_run_token():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    executor = _remote(api, http)
    worker = executor.materialize("hello", "r", "chat:0")
    assert worker.turn("t1", "sum 1 2") == "you said: sum 1 2"
    url, body = http.posts[-1]
    assert url.endswith("/turn") and body == {"turn": "t1", "text": "sum 1 2"}
    assert http.headers[-1]["Authorization"] == f"Bearer {worker._run_token}"


def test_a_pod_that_outlived_its_controller_is_reaped_by_name_on_the_next_start():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    track = InMemoryTrackStore()
    executor = _remote(api, http)
    worker = executor.materialize("hello", "r", "chat:0")
    # The controller that made the pod recorded it and died before tearing it down.
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hello", "session"),)), by=BY)
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent
    track.append(TrackEvent(run_id="r", event_type="run_picked_up", payload={"controller": "hub"},
                            schema=SCHEMA_VERSION))
    track.append(TrackEvent(run_id="r", event_type="step_started",
                            payload={"step": "chat", "attempt": 0, "cog": "hello"}, schema=SCHEMA_VERSION))
    track.append(TrackEvent(run_id="r", event_type="worker_started", payload={
        "step": "chat", "attempt": 0, "instance": "chat:0", **worker.details,
        "run_token_sha256": worker.run_token_digest}, schema=SCHEMA_VERSION))
    api.calls.clear()
    again = LifecycleRunner(executor=_remote(api, http), track=track, controller="hub")
    assert again.start() == ("r",)
    assert ("DELETE", f"/apis/apps/v1/namespaces/hub/deployments/{worker.name}") in api.calls
    assert ("DELETE", f"/api/v1/namespaces/hub/services/{worker.name}") in api.calls
    [stopped] = [e.payload for e in track.replay("r") if e.event_type == "worker_stopped"]
    assert stopped["reaped"] is True and intents.describe(track, "r").state is RunState.INTERRUPTED
    # Nothing to reap the second time: the pod is gone, and the Track says so.
    api.deleted_missing = {f"/apis/apps/v1/namespaces/hub/deployments/{worker.name}",
                           f"/api/v1/namespaces/hub/services/{worker.name}"}
    assert executor.reap(worker.details, "r") is False


def test_a_network_policy_admits_only_the_controller_to_a_worker():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    runner, _ = _runner(InMemoryTrackStore(), api, http, insecure_skip_network_policy=False,
                        allow_ingress_from={"app.kubernetes.io/component": "run-controller"})
    assert runner.submit(OpDefinition("r", (OpStep("a", "x", "run"),))) is RunState.COMPLETED
    [(_, policy)] = [(p, b) for p, b in api.bodies if p.endswith("/networkpolicies")]
    [rule] = policy["spec"]["ingress"]
    assert rule["from"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "run-controller"}}}]
    assert policy["spec"]["policyTypes"] == ["Ingress"]
