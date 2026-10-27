"""Kubernetes CogExecutor: materialize a Cog worker as a Deployment + Service.

Talks to the API server with the pod ServiceAccount token over httpx (no
kubernetes-client dependency). This is the only place K8s primitives are touched
(ADR invariant 2: materialization behind the pluggable executor). The API client
and the worker HTTP client are injectable so the executor is unit-testable
without a cluster; the defaults are the in-cluster SA-token client and httpx.

Workers are reached over in-cluster service DNS (never port-forwards). Each
materialization gets a name that is a valid DNS label and unique per (Cog, run),
so concurrent runs never share or tear down each other's workloads.

Security boundary: a worker's ``/invoke`` Service is unauthenticated, so the
network is the control. Construction requires an explicit choice — you cannot get
the insecure default by omission:

- ``allow_ingress_from={<hub pod label selector>}`` (recommended) materializes a
  NetworkPolicy per worker that admits only the hub to the ``/invoke`` port and
  denies all other ingress. The selector must be non-empty (an empty selector
  matches every pod). Add ``allow_ingress_from_namespace={...}`` when the hub runs
  in another namespace. Requires a policy-enforcing CNI.
- ``insecure_skip_network_policy=True`` deliberately opts out, for a trusted,
  single-tenant namespace only.

Blast radius is reduced regardless — workers carry no ServiceAccount token
(``automountServiceAccountToken: false``). Egress restriction to hub-mediated
paths, and request authentication on the endpoint, are tracked in #11.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from httpx import ConnectError, ConnectTimeout

from .envelope import CODE_FOR_STATUS, EnvelopeInvalid, ResultEnvelope
from .ops import _NO_SIGNAL

_log = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "collab-hub"
RUNNER_PORT = 8080
MAX_DNS_LABEL = 63
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - path, not a secret
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def cog_slug(cog: str) -> str:
    """Reduce a Cog id's last path segment to a DNS-label-safe token.

    Lowercased, restricted to ``[a-z0-9-]`` (non-ASCII/alnum -> ``-``), stripped
    of leading/trailing hyphens, capped, and never empty.
    """
    raw = cog.split("/")[-1].lower()
    cleaned = "".join(ch if (ch.isascii() and ch.isalnum()) or ch == "-" else "-" for ch in raw)
    cleaned = cleaned.strip("-")
    return cleaned[:40] or "cog"


DIGEST_HEX = 16  # 64-bit suffix: birthday collisions stay negligible at realistic run volumes


def resource_name(cog: str, run_id: str, instance: str = "") -> str:
    """A valid, unique DNS-1123 label for one Cog materialization.

    Deterministic (recovery re-derives the same name for the same instance) and
    collision-resistant across Cogs whose slugs would otherwise coincide (the
    digest is over the full Cog id + run id + instance). ``instance`` distinguishes
    each materialization within a run — the engine passes step name + attempt — so
    back-to-back steps on the same Cog, and successive revision attempts, never
    reuse a name that a prior teardown may still be terminating. The suffix is
    ``DIGEST_HEX`` hex chars (64 bits), keeping the name within the 63-char
    DNS-label limit while keeping collisions negligible at realistic scale.
    """
    digest = hashlib.sha1(f"{cog}\x00{run_id}\x00{instance}".encode()).hexdigest()[:DIGEST_HEX]  # noqa: S324 - name uniqueness, not security
    return f"cog-{cog_slug(cog)}-{digest}"[:MAX_DNS_LABEL].rstrip("-")


def label_value(value: str) -> str:
    """A safe Kubernetes label value: ``[A-Za-z0-9._-]``, <=63, alnum-bounded.

    Arbitrary ids (run ids with ``/``, spaces, or length) would make object
    creation fail, so the value is normalized for the label and the exact id is
    kept in an annotation instead (see ``_manifests``).
    """
    cleaned = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in "-_." else "-" for ch in value)
    cleaned = cleaned.strip("-_.")[:63].strip("-_.")
    return cleaned or "unknown"


def _require_label_selector(selector: dict[str, str], what: str) -> None:
    """Validate a non-empty, well-formed label selector.

    An empty selector is rejected because an empty pod/namespace selector matches
    *everything* — a fail-open the worker's ingress rule must never have.
    """
    if not isinstance(selector, dict) or not selector:
        raise ValueError(f"{what} must be a non-empty label selector; an empty selector matches everything")
    for key, value in selector.items():
        if not (isinstance(key, str) and key and isinstance(value, str) and value):
            raise ValueError(f"{what} label {key!r}={value!r} is invalid (keys and values must be non-empty strings)")


class K8sApi(Protocol):
    """Minimal shape of the Kubernetes API client the executor needs."""

    def post(self, path: str, *, json: Any) -> Any: ...
    def patch(self, path: str, *, json: Any, headers: dict[str, str]) -> Any: ...
    def delete(self, path: str) -> Any: ...
    def get(self, path: str) -> Any: ...


class WorkerHttp(Protocol):
    def post(self, url: str, *, json: Any) -> Any: ...


def _in_cluster_api() -> Any:
    import httpx

    with open(TOKEN_PATH) as handle:
        token = handle.read().strip()
    # Prefer the injected service env (an IP:port) so the API server needs no DNS.
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT") or "443"
    base_url = f"https://{host}:{port}" if host else "https://kubernetes.default.svc"
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        verify=CA_PATH,
        timeout=30,
    )


def _default_worker_http(interaction_timeout: float | None = 60.0) -> Any:
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(connect=5.0, read=interaction_timeout, write=interaction_timeout, pool=5.0),
    )


class KubernetesCogExecutor:
    """Materialize a Cog worker as a Deployment + Service and reach it by DNS."""

    name = "kubernetes"

    def __init__(
        self,
        *,
        runner_image: str,
        namespace: str = DEFAULT_NAMESPACE,
        allow_ingress_from: dict[str, str] | None = None,
        allow_ingress_from_namespace: dict[str, str] | None = None,
        insecure_skip_network_policy: bool = False,
        api: K8sApi | None = None,
        worker_http: WorkerHttp | None = None,
        interaction_timeout: float | None = 60.0,
        ready_timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> None:
        # A worker's /invoke endpoint is unauthenticated, so the network boundary is
        # the control. Force an explicit choice at construction — the secure path
        # (a NetworkPolicy admitting only the hub) or a deliberate, named opt-out —
        # so the insecure single-tenant assumption cannot be deployed by omission.
        if allow_ingress_from is None and not insecure_skip_network_policy:
            raise ValueError(
                "KubernetesCogExecutor exposes an unauthenticated worker endpoint. Pass "
                "allow_ingress_from={<pod label selector for the hub>} to materialize a "
                "NetworkPolicy that admits only the hub, or set insecure_skip_network_policy=True "
                "to deliberately run without one in a trusted, single-tenant namespace."
            )
        if allow_ingress_from is not None and insecure_skip_network_policy:
            raise ValueError("pass allow_ingress_from OR insecure_skip_network_policy, not both")
        if allow_ingress_from is not None:
            # An empty podSelector matches ALL pods in the namespace — the opposite
            # of the intent — so reject it (and any malformed label) explicitly.
            _require_label_selector(allow_ingress_from, "allow_ingress_from")
        if allow_ingress_from_namespace is not None:
            if allow_ingress_from is None:
                raise ValueError("allow_ingress_from_namespace requires allow_ingress_from")
            _require_label_selector(allow_ingress_from_namespace, "allow_ingress_from_namespace")
        self.runner_image = runner_image
        self.namespace = namespace
        self.allow_ingress_from = allow_ingress_from
        self.allow_ingress_from_namespace = allow_ingress_from_namespace
        self._api = api
        self._worker_http = worker_http
        self.interaction_timeout = interaction_timeout
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval

    @property
    def api(self) -> K8sApi:
        if self._api is None:
            self._api = _in_cluster_api()
        return self._api

    @property
    def worker_http(self) -> WorkerHttp:
        if self._worker_http is None:
            self._worker_http = _default_worker_http(self.interaction_timeout)
        return self._worker_http

    def _service_url(self, name: str) -> str:
        return f"http://{name}.{self.namespace}.svc.cluster.local:{RUNNER_PORT}"

    def _manifests(self, cog: str, run_id: str, name: str) -> tuple[dict, dict]:
        labels = {
            "app": name,
            "collab-hub/cog": cog_slug(cog),
            "collab-hub/run": label_value(run_id),
            "app.kubernetes.io/managed-by": "collab-hub-executor",
        }
        # Label values are charset/length-constrained; keep the exact run id in an
        # annotation (unconstrained) so nothing is lost and creation never fails.
        annotations = {"collab-hub/run-id": run_id}
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels, "annotations": annotations},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": labels, "annotations": annotations},
                    "spec": {
                        # A Cog worker talks to the hub, not the Kubernetes API, so
                        # it should not carry a ServiceAccount token it could use
                        # (or leak) against the API server.
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "runner",
                                "image": self.runner_image,
                                "imagePullPolicy": "IfNotPresent",
                                "ports": [{"containerPort": RUNNER_PORT}],
                                "env": [{"name": "COG_ID", "value": cog}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthz", "port": RUNNER_PORT},
                                    "initialDelaySeconds": 1,
                                    "periodSeconds": 1,
                                },
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "256Mi"},
                                },
                            }
                        ]
                    },
                },
            },
        }
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels, "annotations": annotations},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": RUNNER_PORT, "targetPort": RUNNER_PORT}],
            },
        }
        return deployment, service

    def _network_policy(self, cog: str, run_id: str, name: str) -> dict:
        """A NetworkPolicy that admits only the hub to this worker's /invoke port.

        Selecting the worker pod with a single ingress rule denies all other
        ingress, so an unauthenticated endpoint is not reachable cluster-wide — only
        pods matching ``allow_ingress_from`` can call it. When the hub runs in
        another namespace, ``allow_ingress_from_namespace`` scopes the peer to that
        namespace (both selectors AND within one peer). (Egress restriction to
        hub-mediated paths is #11.)
        """
        labels = {
            "app": name,
            "collab-hub/cog": cog_slug(cog),
            "collab-hub/run": label_value(run_id),
            "app.kubernetes.io/managed-by": "collab-hub-executor",
        }
        peer = {"podSelector": {"matchLabels": self.allow_ingress_from}}
        if self.allow_ingress_from_namespace is not None:
            peer["namespaceSelector"] = {"matchLabels": self.allow_ingress_from_namespace}
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": labels,
                "annotations": {"collab-hub/run-id": run_id},
            },
            "spec": {
                "podSelector": {"matchLabels": {"app": name}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [peer],
                        "ports": [{"protocol": "TCP", "port": RUNNER_PORT}],
                    }
                ],
            },
        }

    def _apply(self, collection_path: str, name: str, body: dict, *, replaceable: bool) -> None:
        response = self.api.post(collection_path, json=body)
        if response.status_code == 409:  # already exists
            if not replaceable:
                return
            # merge-patch updates the existing object's spec; RBAC grants `patch`
            # (not `update`), so this stays within least privilege.
            response = self.api.patch(
                f"{collection_path}/{name}",
                json=body,
                headers={"Content-Type": "application/merge-patch+json"},
            )
        response.raise_for_status()

    def _wait_ready(self, name: str) -> None:
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}"
        deadline = time.monotonic() + self.ready_timeout
        while True:
            response = self.api.get(path)
            response.raise_for_status()
            ready = (response.json().get("status") or {}).get("readyReplicas") or 0
            if ready >= 1:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"deployment {name} not ready within {self.ready_timeout}s")
            time.sleep(self.poll_interval)

    def _network_policy_collection(self) -> str:
        return f"/apis/networking.k8s.io/v1/namespaces/{self.namespace}/networkpolicies"

    def _resource_paths(self, name: str) -> tuple[str, ...]:
        paths = [
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            f"/api/v1/namespaces/{self.namespace}/services/{name}",
        ]
        if self.allow_ingress_from is not None:
            paths.append(f"{self._network_policy_collection()}/{name}")
        return tuple(paths)

    def materialize(self, cog: str, run_id: str, instance: str = "") -> "_KubernetesWorker":
        name = resource_name(cog, run_id, instance)
        deployment, service = self._manifests(cog, run_id, name)
        try:
            if self.allow_ingress_from is not None:
                # Put the ingress restriction in place before the pod can serve.
                self._apply(
                    self._network_policy_collection(), name, self._network_policy(cog, run_id, name), replaceable=True
                )
            self._apply(
                f"/apis/apps/v1/namespaces/{self.namespace}/deployments", name, deployment, replaceable=True
            )
            self._apply(
                f"/api/v1/namespaces/{self.namespace}/services", name, service, replaceable=False
            )
            self._wait_ready(name)
        except Exception:
            # A partial materialization (e.g. Deployment created, then Service or
            # readiness failed) would leak a workload the engine can't tear down —
            # it never got a worker handle. Delete by the deterministic name,
            # best-effort, then re-raise the original failure.
            self._cleanup(name)
            raise
        return _KubernetesWorker(cog, name, self._service_url(name), self.worker_http)

    def _cleanup(self, name: str) -> None:
        """Best-effort delete of a (possibly partial) materialization by name.

        Runs while a materialize() failure is already propagating, so it never
        raises (that would mask the original cause). It still validates each delete
        the way teardown() does — an HTTP error response does not raise on its own —
        and logs any resource it could not remove, so an orphan is visible for
        remediation instead of being silently swallowed.
        """
        for path in self._resource_paths(name):
            try:
                response = self.api.delete(path)
                status = getattr(response, "status_code", None)
                if status is not None and status not in (200, 202, 404):
                    _log.warning("cleanup of %s returned HTTP %s; resource may be orphaned", path, status)
            except Exception as exc:  # noqa: BLE001 - cleanup runs during failure handling
                _log.warning("cleanup of %s failed: %r; resource may be orphaned", path, exc)

    def teardown(self, worker: "_KubernetesWorker") -> None:
        for path in self._resource_paths(worker.name):
            response = self.api.delete(path)
            if response.status_code not in (200, 202, 404):
                response.raise_for_status()


class _KubernetesWorker:
    """A materialized Cog reached over its service DNS via its declared entry point."""

    def __init__(self, cog: str, name: str, url: str, http: WorkerHttp) -> None:
        self.cog = cog
        self.name = name
        self.url = url
        self.http = http

    def interact(
        self, entry_point: str, input: Any = None, idempotency_key: str | None = None,
        *, signal: Any = _NO_SIGNAL, frames: Sequence[Mapping[str, Any]] | None = None,
    ) -> ResultEnvelope:
        payload = {"entry_point": entry_point, "input": input, "idempotency_key": idempotency_key}
        if signal is not _NO_SIGNAL:
            payload["signal"] = signal
        if frames:
            payload["frames"] = [dict(frame) for frame in frames]
        response = self._post_with_retry(f"{self.url}/invoke", payload)
        status = response.status_code
        body = self._decoded(response)
        if status == 200:
            # A Cog cannot pause a run: an answer that is not an envelope, a
            # `{"pause": true}` included, fails the step as EnvelopeInvalid.
            return ResultEnvelope.parse(body)
        if status in CODE_FOR_STATUS:
            # The envelope document's error statuses. The body is an error
            # envelope; when it is not one (a proxy's page, a worker that died
            # mid-answer) the status alone still names the failure.
            if isinstance(body, dict) and "envelope" in body:
                envelope = ResultEnvelope.parse(body)
                if envelope.ok:
                    raise EnvelopeInvalid(f"HTTP {status} carried an ok: true envelope")
                return envelope
            return ResultEnvelope.failure(CODE_FOR_STATUS[status], f"HTTP {status} from the worker without an envelope")
        response.raise_for_status()
        raise EnvelopeInvalid(f"unexpected HTTP {status} from /invoke")

    @staticmethod
    def _decoded(response: Any) -> Any:
        """The response's JSON body, or None when there is none to decode."""
        try:
            return response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is answered by the status handling
            return None

    def _post_with_retry(self, url: str, payload: Any, *, attempts: int = 40, delay: float = 0.5) -> Any:
        # A freshly materialized Service can be briefly unroutable (endpoints /
        # kube-proxy still programming) even after the pod is Ready. Retry the
        # connection only — HTTP responses (including errors) are returned as-is.
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self.http.post(url, json=payload)
            except (ConnectError, ConnectTimeout) as exc:
                last = exc
                time.sleep(delay)
        raise last  # type: ignore[misc]
