"""KubernetesCogExecutor unit tests — no cluster; the API + worker HTTP are faked."""

import logging
import re

import httpx
import pytest

from collab_hub_execution import (
    EnvelopeInvalid,
    KubernetesCogExecutor,
    ResultEnvelope,
    cog_slug,
    label_value,
    resource_name,
)
from collab_hub_execution.kubernetes import _KubernetesWorker


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError])
def test_ambiguous_interaction_failure_is_not_reposted(error):
    calls = []

    def handle(request):
        calls.append(request)
        raise error("response unavailable", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        worker = _KubernetesWorker("c", "worker", "http://worker", client)
        with pytest.raises(error):
            worker.interact("run", {"side_effect": True}, idempotency_key="r:s:0")
    assert len(calls) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_failure_can_retry_without_changing_payload(error, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.content)
        if len(calls) == 1:
            raise error("not connected", request=request)
        return httpx.Response(200, json={"envelope": 1, "ok": True, "payload": "done"})

    monkeypatch.setattr("collab_hub_execution.kubernetes.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        worker = _KubernetesWorker("c", "worker", "http://worker", client)
        assert worker.interact("run", "draft", idempotency_key="r:s:0") == ResultEnvelope.success("done")
    assert len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.parametrize("timeout", [60.0, 300.0, None])
def test_interaction_timeout_preserves_bounded_connections(timeout):
    executor = KubernetesCogExecutor(
        runner_image="worker:test", insecure_skip_network_policy=True, interaction_timeout=timeout,
    )
    with executor.worker_http as client:
        assert client.timeout.read == timeout
        assert client.timeout.write == timeout
        assert client.timeout.connect == 5.0
        assert client.timeout.pool == 5.0


@pytest.mark.parametrize("signal", [{"approved": True}, None])
def test_signal_is_sent_separately_from_original_input(signal):
    http = FakeWorkerHttp()
    worker = _KubernetesWorker("c", "worker", "http://worker", http)
    worker.interact("review", {"draft": "v1"})
    assert "signal" not in http.posts[-1][1]
    worker.interact("review", {"draft": "v1"}, signal=signal)
    assert http.posts[-1][1]["input"] == {"draft": "v1"}
    assert http.posts[-1][1]["signal"] == signal


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeK8sApi:
    """Records calls; returns created responses and a ready deployment."""

    def __init__(
        self, *, conflict_on_deployment: bool = False, ready_after: int = 0, fail_delete: bool = False
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict]] = []  # what was POSTed, path and manifest
        self.deleted_missing: set[str] = set()  # paths whose delete answers 404
        self.conflict_on_deployment = conflict_on_deployment
        self._gets = 0
        self._ready_after = ready_after
        self.fail_delete = fail_delete

    def post(self, path: str, *, json: dict) -> FakeResponse:
        self.calls.append(("POST", path))
        self.bodies.append((path, json))
        if self.conflict_on_deployment and "deployments" in path:
            return FakeResponse(409)
        return FakeResponse(201)

    def patch(self, path: str, *, json: dict, headers: dict) -> FakeResponse:
        self.calls.append(("PATCH", path))
        return FakeResponse(200)

    def delete(self, path: str) -> FakeResponse:
        self.calls.append(("DELETE", path))
        # httpx does not raise on 4xx/5xx; a real failed delete returns an error
        # response the caller must inspect.
        if path in self.deleted_missing:
            return FakeResponse(404)
        return FakeResponse(500 if self.fail_delete else 200)

    def get(self, path: str) -> FakeResponse:
        self.calls.append(("GET", path))
        ready = 1 if self._gets >= self._ready_after else 0
        self._gets += 1
        return FakeResponse(200, {"status": {"readyReplicas": ready}})


class FakeWorkerHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.headers: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict | None = None) -> FakeResponse:
        self.posts.append((url, json))
        self.headers.append(dict(headers or {}))
        if url.endswith("/turn"):
            return FakeResponse(200, {"text": f"you said: {json['text']}"})
        return FakeResponse(200, {"envelope": 1, "ok": True, "payload": f"handled:{json['entry_point']}"})


def _executor(api, worker_http):
    return KubernetesCogExecutor(
        runner_image="collab-hub/cog-runner:test",
        namespace="cogs",
        api=api,
        worker_http=worker_http,
        poll_interval=0,
        insecure_skip_network_policy=True,
    )


def _secure_executor(api, worker_http, allow_ingress_from=None):
    return KubernetesCogExecutor(
        runner_image="collab-hub/cog-runner:test",
        namespace="cogs",
        api=api,
        worker_http=worker_http,
        poll_interval=0,
        allow_ingress_from=allow_ingress_from or {"app": "hub"},
    )


def test_materialize_creates_deployment_and_service_then_reaches_by_dns():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    worker = _executor(api, http).materialize("openteams/research-analyst", "run-1")

    assert ("POST", "/apis/apps/v1/namespaces/cogs/deployments") in api.calls
    assert ("POST", "/api/v1/namespaces/cogs/services") in api.calls
    # reached only over service DNS, at the per-(cog, run) name — never a port-forward
    assert worker.url == f"http://{worker.name}.cogs.svc.cluster.local:8080"
    assert worker.name.startswith("cog-research-analyst-")


def test_interact_posts_to_the_entry_point_with_idempotency_key():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    worker = _executor(api, http).materialize("openteams/draft-generator", "run-2")
    result = worker.interact("draft", {"topic": "x"}, idempotency_key="run-2:draft:0")
    assert result == ResultEnvelope.success("handled:draft")
    url, body = http.posts[-1]
    assert url.endswith("/invoke")
    assert body["entry_point"] == "draft" and body["idempotency_key"] == "run-2:draft:0"


def test_existing_deployment_is_replaced_with_a_merge_patch_not_update():
    api = FakeK8sApi(conflict_on_deployment=True)
    worker = _executor(api, FakeWorkerHttp()).materialize("openteams/doc-formatter", "run-3")
    # 409 is resolved with PATCH (granted by RBAC), never PUT (update, not granted)
    assert ("PATCH", f"/apis/apps/v1/namespaces/cogs/deployments/{worker.name}") in api.calls
    assert not any(method == "PUT" for method, _ in api.calls)


def test_wait_ready_polls_until_ready():
    api = FakeK8sApi(ready_after=2)  # not ready on the first two GETs
    _executor(api, FakeWorkerHttp()).materialize("openteams/x", "run-4")
    assert sum(1 for method, _ in api.calls if method == "GET") >= 3


def test_teardown_deletes_deployment_and_service_by_worker_name():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    ex = _executor(api, http)
    worker = ex.materialize("openteams/reviewer", "run-5")
    ex.teardown(worker)
    assert ("DELETE", f"/apis/apps/v1/namespaces/cogs/deployments/{worker.name}") in api.calls
    assert ("DELETE", f"/api/v1/namespaces/cogs/services/{worker.name}") in api.calls


class PausingWorkerHttp:
    def post(self, url: str, *, json: dict, headers: dict | None = None) -> FakeResponse:
        return FakeResponse(200, {"pause": True, "reason": "needs approval"})


def test_a_worker_asking_to_pause_is_not_answering_with_an_envelope():
    # A Cog cannot pause a run; only its step's Gate can escalate one.
    worker = _executor(FakeK8sApi(), PausingWorkerHttp()).materialize("openteams/gated", "run-6")
    with pytest.raises(EnvelopeInvalid):
        worker.interact("review", {"draft": "v1"})


# --- resource naming (#2 concurrency, #6 DNS validity) ---


def test_cog_slug_is_dns_safe_bounded_and_nonempty():
    assert cog_slug("openteams/Research Analyst!") == "research-analyst"
    assert cog_slug("openteams/café") == "caf"  # non-ASCII dropped
    assert cog_slug("openteams/---") == "cog"  # empty -> fallback, never blank
    assert len(cog_slug("x/" + "a" * 200)) <= 40


def test_resource_name_is_valid_unique_per_run_and_collision_resistant():
    n_a = resource_name("openteams/reviewer", "run-A")
    n_b = resource_name("openteams/reviewer", "run-B")
    assert n_a != n_b  # concurrent runs of the same Cog never collide
    assert n_a == resource_name("openteams/reviewer", "run-A")  # deterministic (recovery-safe)
    assert len(n_a) <= 63
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", n_a)  # valid DNS-1123 label
    # 64-bit digest suffix (16 hex): wide enough that collisions stay negligible
    assert re.search(r"-[0-9a-f]{16}$", n_a)
    # slug-colliding Cogs still get distinct names (digest is over the full id)
    assert resource_name("a/reviewer", "r") != resource_name("b/reviewer", "r")
    # a very long Cog id still yields a valid, in-bounds label with a full suffix
    long_name = resource_name("openteams/" + "a" * 300, "run-A")
    assert len(long_name) <= 63 and re.search(r"-[0-9a-f]{16}$", long_name)
    # instance distinguishes materializations within a run (step and attempt), so a
    # back-to-back same-Cog step or a next attempt never reuses a name
    base = resource_name("openteams/reviewer", "run-A")
    assert resource_name("openteams/reviewer", "run-A", "review:0") != base
    assert resource_name("openteams/reviewer", "run-A", "review:0") != resource_name(
        "openteams/reviewer", "run-A", "summarize:0"
    )
    assert resource_name("openteams/reviewer", "run-A", "review:0") != resource_name(
        "openteams/reviewer", "run-A", "review:1"
    )


def test_distinct_instances_get_distinct_worker_names():
    ex = _executor(FakeK8sApi(), FakeWorkerHttp())
    w1 = ex.materialize("openteams/reviewer", "run-1", "review:0")
    w2 = ex.materialize("openteams/reviewer", "run-1", "summarize:0")  # same Cog, different step
    w3 = ex.materialize("openteams/reviewer", "run-1", "review:1")  # same step, next attempt
    assert len({w1.name, w2.name, w3.name}) == 3


def test_partial_materialization_is_cleaned_up_on_failure():
    # Deployment is created, then readiness never arrives: materialize must not
    # leak the Deployment (the engine gets no worker handle to tear down).
    api = FakeK8sApi(ready_after=99)  # never ready
    ex = KubernetesCogExecutor(
        runner_image="collab-hub/cog-runner:test",
        namespace="cogs",
        api=api,
        worker_http=FakeWorkerHttp(),
        poll_interval=0,
        ready_timeout=0,
        insecure_skip_network_policy=True,
    )
    with pytest.raises(TimeoutError):
        ex.materialize("openteams/reviewer", "run-x")
    name = resource_name("openteams/reviewer", "run-x")
    assert ("DELETE", f"/apis/apps/v1/namespaces/cogs/deployments/{name}") in api.calls
    assert ("DELETE", f"/api/v1/namespaces/cogs/services/{name}") in api.calls


def test_partial_cleanup_surfaces_a_failed_delete_instead_of_swallowing_it(caplog):
    # A delete that returns an HTTP error (httpx would not raise) must not be
    # silently ignored — the orphaned resource is logged for remediation, and the
    # original materialize failure still propagates.
    api = FakeK8sApi(ready_after=99, fail_delete=True)
    ex = KubernetesCogExecutor(
        runner_image="collab-hub/cog-runner:test",
        namespace="cogs",
        api=api,
        worker_http=FakeWorkerHttp(),
        poll_interval=0,
        ready_timeout=0,
        insecure_skip_network_policy=True,
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TimeoutError):  # original cause, not masked by cleanup
            ex.materialize("openteams/reviewer", "run-x")
    assert any("orphaned" in record.getMessage() for record in caplog.records)


def test_label_value_is_a_safe_kubernetes_label(monkeypatch):
    # run ids with '/', spaces, or excess length must not break object creation
    assert label_value("org/ws/run 42") == "org-ws-run-42"
    assert len(label_value("x" * 200)) <= 63
    assert label_value("///") == "unknown"  # never blank
    assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", label_value("org/ws/run 42"))


def test_manifests_keep_the_exact_run_id_in_an_annotation():
    ex = _executor(FakeK8sApi(), FakeWorkerHttp())
    _deployment, service = ex._manifests("openteams/reviewer", "org/ws/run 42", "cog-reviewer-abcd1234")
    assert service["metadata"]["labels"]["collab-hub/run"] == "org-ws-run-42"  # sanitized label
    assert service["metadata"]["annotations"]["collab-hub/run-id"] == "org/ws/run 42"  # exact id preserved


def test_worker_pod_does_not_mount_a_service_account_token():
    ex = _executor(FakeK8sApi(), FakeWorkerHttp())
    deployment, _service = ex._manifests("openteams/reviewer", "run-1", "cog-reviewer-x")
    # a worker talks to the hub, not the K8s API, so it carries no SA token
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


# --- worker network boundary is enforced, not just documented ---


def test_construction_requires_an_explicit_network_security_choice():
    # the insecure default cannot be reached by omission
    with pytest.raises(ValueError):
        KubernetesCogExecutor(
            runner_image="x", namespace="cogs", api=FakeK8sApi(), worker_http=FakeWorkerHttp()
        )
    # and the two choices are mutually exclusive
    with pytest.raises(ValueError):
        KubernetesCogExecutor(
            runner_image="x",
            namespace="cogs",
            api=FakeK8sApi(),
            worker_http=FakeWorkerHttp(),
            allow_ingress_from={"app": "hub"},
            insecure_skip_network_policy=True,
        )


def test_empty_ingress_selector_is_rejected():
    # {} serializes to an empty podSelector, which matches ALL pods in the namespace
    with pytest.raises(ValueError):
        KubernetesCogExecutor(
            runner_image="x", namespace="cogs", api=FakeK8sApi(), worker_http=FakeWorkerHttp(),
            allow_ingress_from={},
        )


def test_malformed_ingress_labels_are_rejected():
    for bad in ({"": "hub"}, {"app": ""}):
        with pytest.raises(ValueError):
            KubernetesCogExecutor(
                runner_image="x", namespace="cogs", api=FakeK8sApi(), worker_http=FakeWorkerHttp(),
                allow_ingress_from=bad,
            )


def test_network_policy_can_scope_the_hub_to_another_namespace():
    ex = KubernetesCogExecutor(
        runner_image="x",
        namespace="cogs",
        api=FakeK8sApi(),
        worker_http=FakeWorkerHttp(),
        allow_ingress_from={"app": "hub"},
        allow_ingress_from_namespace={"kubernetes.io/metadata.name": "collab-hub"},
    )
    policy = ex._network_policy("openteams/reviewer", "run-ns", "cog-reviewer-x")
    peer = policy["spec"]["ingress"][0]["from"][0]
    assert peer["podSelector"] == {"matchLabels": {"app": "hub"}}
    assert peer["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "collab-hub"}}


def test_namespace_selector_requires_a_pod_selector():
    with pytest.raises(ValueError):
        KubernetesCogExecutor(
            runner_image="x", namespace="cogs", api=FakeK8sApi(), worker_http=FakeWorkerHttp(),
            allow_ingress_from_namespace={"kubernetes.io/metadata.name": "hub-ns"},
            insecure_skip_network_policy=True,
        )


def test_network_policy_admits_only_the_hub_and_is_applied_before_the_pod():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    ex = _secure_executor(api, http, allow_ingress_from={"app": "collab-hub-api"})
    worker = ex.materialize("openteams/reviewer", "run-np")

    np_post = ("POST", "/apis/networking.k8s.io/v1/namespaces/cogs/networkpolicies")
    dep_post = ("POST", "/apis/apps/v1/namespaces/cogs/deployments")
    assert np_post in api.calls and dep_post in api.calls
    assert api.calls.index(np_post) < api.calls.index(dep_post)  # policy in place before the pod

    policy = ex._network_policy("openteams/reviewer", "run-np", worker.name)
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": worker.name}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"][0]["from"] == [{"podSelector": {"matchLabels": {"app": "collab-hub-api"}}}]
    assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_teardown_deletes_the_network_policy_too():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    worker = (ex := _secure_executor(api, http)).materialize("openteams/reviewer", "run-np2")
    ex.teardown(worker)
    assert ("DELETE", f"/apis/networking.k8s.io/v1/namespaces/cogs/networkpolicies/{worker.name}") in api.calls


def test_insecure_skip_creates_no_network_policy():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    _executor(api, http).materialize("openteams/reviewer", "run-x")
    assert not any("networkpolicies" in path for _method, path in api.calls)
