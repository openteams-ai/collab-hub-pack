#!/usr/bin/env python3
"""Assertions over a rendered collab-hub release for the Cog registry block.

    helm template t helm/collab-hub -f <values> | scripts/chart_render_check.py <case>

Driven by scripts/chart_render_tests.sh. Each case parses the API Deployment
and checks the actual attributes — secretKeyRef name and key, readOnly on the
mount, the JSON's per-source content — rather than grepping for a line that
happens to contain the right name. Needs PyYAML.
"""

from __future__ import annotations

import json
import re
import sys

import yaml

CA_MOUNT = "/etc/collab-hub/cogs-ca"
SOURCES_VAR = "COLLAB_HUB_API__COGS__REGISTRY_SOURCES"


def env_id(source_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in source_id.upper())


COMPONENT_LABEL = "app.kubernetes.io/component"
INDEX_VARS = (
    "COLLAB_HUB_API__COGS__INDEX__ENABLED",
    "COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS",
    "COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP",
)


class Workload:
    """One rendered Deployment: its pod spec and first container, indexed for assertions."""

    def __init__(self, deployment: dict) -> None:
        self.deployment = deployment
        self.spec = deployment["spec"]
        self.pod = self.spec["template"]["spec"]
        self.container = self.pod["containers"][0]
        self.containers = {c["name"]: c for c in self.pod["containers"]}
        self.annotations = self.spec["template"]["metadata"].get("annotations") or {}
        self.env = {e["name"]: e for e in self.container["env"]}
        self.mounts = {m["name"]: m for m in self.container.get("volumeMounts", [])}
        self.volumes = {v["name"]: v for v in self.pod.get("volumes", [])}
        self.selector = self.spec["selector"]["matchLabels"]

    def value(self, name: str) -> str:
        return self.env[name]["value"]


class Rendered:
    """The release: the API Deployment (always), the indexer Deployment (when indexing is on), the Services."""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        deployments = {d["metadata"]["labels"][COMPONENT_LABEL]: d for d in docs if d.get("kind") == "Deployment"}
        assert set(deployments) <= {"api", "indexer", "run-controller"}, sorted(deployments)
        self.api = Workload(deployments["api"])
        self.indexer = Workload(deployments["indexer"]) if "indexer" in deployments else None
        self.controller = Workload(deployments["run-controller"]) if "run-controller" in deployments else None
        self.roles = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Role"}
        self.bindings = [d for d in docs if d.get("kind") == "RoleBinding"]
        self.service_accounts = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ServiceAccount"}
        self.services = [d for d in docs if d.get("kind") == "Service"]
        self.configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}
        # The API container is what the source/credential assertions read;
        # the indexer's env is asserted equal to it where it must be.
        self.env = self.api.env
        self.mounts = self.api.mounts
        self.volumes = self.api.volumes
        self.containers = self.api.containers
        self.annotations = self.api.annotations
        self.cogs_env = sorted(n for n in self.env if "COGS" in n)
        self.sources_json = self.env[SOURCES_VAR]["value"] if SOURCES_VAR in self.env else None
        self.sources = {s["id"]: s for s in json.loads(self.sources_json)} if self.sources_json else {}
        self.source_order = [s["id"] for s in json.loads(self.sources_json)] if self.sources_json else []

    def api_does_not_sweep(self) -> None:
        # Issue #148: whatever the values say, the API replicas render the
        # index switch off and none of its tuning -- sweeping is the indexer
        # workload's alone.
        assert self.value("COLLAB_HUB_API__COGS__INDEX__ENABLED") == "false", "an API replica must never sweep"
        for name in INDEX_VARS[1:]:
            assert name not in self.env, f"{name} rendered on the API deployment"

    def no_indexer(self) -> None:
        assert self.indexer is None, "an indexer Deployment rendered with cogs.index.enabled=false"

    def indexer_is_the_one_sweeper(self, *, interval: str, run_on_startup: str) -> None:
        """The indexer Deployment: one replica, Recreate, the API's image and settings, index on."""

        indexer = self.indexer
        assert indexer is not None, "cogs.index.enabled=true must render the indexer Deployment"
        assert indexer.spec["replicas"] == 1, indexer.spec.get("replicas")
        assert indexer.spec["strategy"] == {"type": "Recreate"}, indexer.spec.get("strategy")
        assert indexer.value("COLLAB_HUB_API__COGS__INDEX__ENABLED") == "true"
        assert indexer.value("COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS") == interval
        assert indexer.value("COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP") == run_on_startup
        # Same process image and settings as the API: everything but the
        # index switch and its tuning is identical, Secret refs included.
        api = self.api
        assert indexer.container["image"] == api.container["image"]
        assert indexer.pod["serviceAccountName"] == api.pod["serviceAccountName"]
        assert indexer.pod.get("securityContext") == api.pod.get("securityContext")
        assert indexer.container.get("securityContext") == api.container.get("securityContext")
        api_env = {name: entry for name, entry in api.env.items() if name not in INDEX_VARS}
        indexer_env = {name: entry for name, entry in indexer.env.items() if name not in INDEX_VARS}
        assert indexer_env == api_env, "the indexer's environment differs from the API's beyond the index switch"
        # Nothing routes to it: its component label is its own, and no
        # Service selects it.
        assert indexer.selector[COMPONENT_LABEL] == "indexer"
        assert api.selector[COMPONENT_LABEL] == "api"
        for service in self.services:
            assert service["spec"]["selector"][COMPONENT_LABEL] != "indexer", service["metadata"]["name"]
        # Its frames-storage mount is an emptyDir: the API's claim stays the
        # API's, and the process only needs the path to start.
        assert indexer.mounts["frames-storage"]["mountPath"] == api.mounts["frames-storage"]["mountPath"]
        assert indexer.volumes["frames-storage"] == {"name": "frames-storage", "emptyDir": {}}
        assert indexer.container["resources"] == api.container["resources"], "empty indexer resources follow the API"

    def value(self, name: str) -> str:
        return self.env[name]["value"]

    def secret_ref(self, name: str, secret: str, key: str) -> None:
        entry = self.env.get(name)
        assert entry is not None, f"{name} not rendered"
        assert "value" not in entry, f"{name} rendered as a literal value"
        ref = entry["valueFrom"]["secretKeyRef"]
        assert ref == {"name": secret, "key": key}, f"{name} -> {ref}, expected {secret}/{key}"

    def credentials_attached(self, source_id: str, secret: str, *, username_key="username", password_key="password"):
        source = self.sources[source_id]
        user_var, pw_var = (f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_{s}" for s in ("USERNAME", "PASSWORD"))
        assert source["credentials"] == {"username_env": user_var, "password_env": pw_var}, source
        self.secret_ref(user_var, secret, username_key)
        self.secret_ref(pw_var, secret, password_key)
        assert secret not in self.sources_json, f"Secret name {secret} leaked into the JSON"

    def webhook_attached(self, source_id: str, secret: str, *, key="secret") -> None:
        var = f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_WEBHOOK_SECRET"
        assert self.sources[source_id]["webhook_secret_env"] == var
        self.secret_ref(var, secret, key)
        assert secret not in self.sources_json, f"Secret name {secret} leaked into the JSON"

    def no_credentials(self, source_id: str) -> None:
        assert "credentials" not in self.sources[source_id]
        assert "webhook_secret_env" not in self.sources[source_id]
        prefix = f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_"
        assert not [n for n in self.env if n.startswith(prefix)], f"{source_id} has Secret env vars but no Secret"

    def no_source_vars_for(self, source_id: str) -> None:
        prefix = f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_"
        assert not [n for n in self.env if n.startswith(prefix)], f"stale env vars for removed source {source_id}"
        assert source_id not in self.sources


def case_default(r: Rendered) -> None:
    assert r.cogs_env == ["COLLAB_HUB_API__COGS__INDEX__ENABLED"], r.cogs_env
    assert not [n for n in r.env if n.startswith("COLLAB_HUB_API__FEATURES__")], "flag vars rendered with no flags set"
    r.api_does_not_sweep()
    r.no_indexer()
    assert "cogs-ca-bundle" not in r.mounts and "cogs-ca-bundle" not in r.volumes


def case_fixture(r: Rendered) -> None:
    r.api_does_not_sweep()
    r.indexer_is_the_one_sweeper(interval="120", run_on_startup="false")
    assert r.source_order == ["harbor-main", "public.mirror"]
    harbor = r.sources["harbor-main"]
    assert harbor["kind"] == "harbor"
    assert harbor["api_url"] == "http://harbor-core.harbor.svc.cluster.local:80"
    assert harbor["token_url"] == "http://harbor-core.harbor.svc.cluster.local:80/service/token"
    assert harbor["projects"] == ["cogs"]
    assert harbor["ca_bundle_path"] == f"{CA_MOUNT}/ca.crt", "shared CA bundle applies to a source without its own"
    assert "request_timeout_seconds" not in harbor, "unset timeout is omitted, not defaulted, in the JSON"
    r.credentials_attached("harbor-main", "collab-hub-harbor-robot")
    r.webhook_attached("harbor-main", "collab-hub-harbor-webhook")
    static = r.sources["public.mirror"]
    assert static["kind"] == "static"
    assert static["index_url"] == "https://registry.example.com/catalog.v1.json"
    assert static["repositories"] == ["cogs/alpha"]
    assert static["ca_bundle_path"] == "/etc/ssl/certs/ca-certificates.crt", "per-source caBundlePath wins"
    assert static["request_timeout_seconds"] == 20
    r.no_credentials("public.mirror")
    mount = r.mounts["cogs-ca-bundle"]
    assert mount["mountPath"] == CA_MOUNT and mount["readOnly"] is True, mount
    volume = r.volumes["cogs-ca-bundle"]["configMap"]
    assert volume["name"] == "collab-hub-cogs-ca"
    assert volume["items"] == [{"key": "ca.crt", "path": "ca.crt"}]
    # The indexer is the process that talks to the registries, so the CA
    # bundle is mounted there the same way.
    assert r.indexer is not None
    assert r.indexer.mounts["cogs-ca-bundle"] == mount
    assert r.indexer.volumes["cogs-ca-bundle"] == r.volumes["cogs-ca-bundle"]


def case_static_only(r: Rendered) -> None:
    # Sources with the indexer off: the source JSON still renders (the read
    # API needs it), the two tuning vars do not, and no indexer Deployment
    # does either.
    r.api_does_not_sweep()
    r.no_indexer()
    assert r.source_order == ["public"]
    assert "ca_bundle_path" not in r.sources["public"], "no CA bundle configured, none passed"
    assert "cogs-ca-bundle" not in r.mounts


def case_three_sources(r: Rendered) -> None:
    assert r.source_order == ["harbor-main", "mid.one", "tail-two"]
    r.credentials_attached("harbor-main", "robot-a")
    assert "webhook_secret_env" not in r.sources["harbor-main"]
    r.credentials_attached("mid.one", "robot-b", username_key="user", password_key="pass")
    r.webhook_attached("tail-two", "hook-c", key="token")
    assert "credentials" not in r.sources["tail-two"]


def case_reordered(r: Rendered) -> None:
    assert r.source_order == ["tail-two", "harbor-main"]
    r.webhook_attached("tail-two", "hook-c", key="token")
    assert "credentials" not in r.sources["tail-two"]
    r.credentials_attached("harbor-main", "robot-a")
    assert "webhook_secret_env" not in r.sources["harbor-main"]
    r.no_source_vars_for("mid.one")


def case_indexer_resources(r: Rendered) -> None:
    # cogs.indexer.resources set: the indexer container carries them and the
    # API keeps its own; everything else about the two stays identical.
    r.api_does_not_sweep()
    indexer = r.indexer
    assert indexer is not None
    assert indexer.spec["replicas"] == 1 and indexer.spec["strategy"] == {"type": "Recreate"}
    assert indexer.value("COLLAB_HUB_API__COGS__INDEX__ENABLED") == "true"
    assert indexer.container["resources"] == {"requests": {"memory": "1Gi"}, "limits": {"memory": "2Gi"}}
    assert r.api.container["resources"] != indexer.container["resources"]
    assert indexer.container["image"] == r.api.container["image"]


def case_features(r: Rendered) -> None:
    flags = {n: r.value(n) for n in r.env if n.startswith("COLLAB_HUB_API__FEATURES__")}
    assert flags == {
        "COLLAB_HUB_API__FEATURES__COGS_UI": "true",
        "COLLAB_HUB_API__FEATURES__NEW_THING": "false",
    }, flags


SERVE_PREFIX = "COLLAB_HUB_API__COGS__SERVE__"


def serve_env(r: Rendered) -> dict[str, str]:
    return {n[len(SERVE_PREFIX) :]: r.value(n) for n in r.env if n.startswith(SERVE_PREFIX)}


def kind(r: Rendered, name: str) -> dict | None:
    return next((d for d in r.docs if d.get("kind") == name), None)


def case_publish_with_indexer(r: Rendered) -> None:
    # Who may publish (issue #180) governs push requests, which reach the API
    # replicas only: they carry it, the indexer carries none of it.
    publish = {n: r.value(n) for n in r.env if n.startswith("COLLAB_HUB_API__COGS__PUBLISH__")}
    assert publish == {
        "COLLAB_HUB_API__COGS__PUBLISH__ALLOWED_ROLES": '["owner"]',
        "COLLAB_HUB_API__COGS__PUBLISH__MAX_PENDING_REPOSITORIES": "5",
    }, publish
    assert r.indexer is not None
    assert not [n for n in r.indexer.env if n.startswith("COLLAB_HUB_API__COGS__PUBLISH__")], sorted(r.indexer.env)


def case_serve_with_indexer(r: Rendered) -> None:
    # Serving and sweeping both on: /v2 routes to the API replicas only, but
    # the indexer carries the same serve settings. A sweep is held to the
    # serving redirect policy when the deployment serves pulls
    # (docs/cog-registry.md), and a process without cogs.serve.enabled
    # follows redirects unchecked.
    assert serve_env(r)["ENABLED"] == "true", serve_env(r)
    r.indexer_is_the_one_sweeper(interval="300", run_on_startup="true")  # env equal to the API's, serve included
    assert r.indexer.value(SERVE_PREFIX + "ENABLED") == "true"


def case_serve_off(r: Rendered) -> None:
    # Sources configured, serving left off: no trace of it anywhere.
    assert serve_env(r) == {}, serve_env(r)
    app = kind(r, "NebariApp")
    assert app is not None
    assert [route["pathPrefix"] for route in app["spec"]["routing"]["publicRoutes"]] == ["/v1", "/mcp", "/health"]
    assert kind(r, "BackendTrafficPolicy") is None


def case_serve_nebariapp(r: Rendered) -> None:
    # The public URL is derived from the NebariApp hostname, and /v2 joins the
    # routes the gateway leaves to the app's own authentication.
    assert serve_env(r) == {
        "ENABLED": "true",
        "PUBLIC_URL": "https://hub.example.com",
        "CREDENTIAL_TTL_SECONDS": "900",
        "TOKEN_TTL_SECONDS": "300",
        "MAX_BLOB_BYTES": "1073741824",
        "MAX_BLOB_SECONDS": "900",
    }, serve_env(r)
    public = kind(r, "NebariApp")["spec"]["routing"]["publicRoutes"]
    assert public[-1] == {"pathPrefix": "/v2", "pathType": "PathPrefix"}, public
    assert [route["pathPrefix"] for route in public] == ["/v1", "/mcp", "/health", "/v2"]
    assert kind(r, "HTTPRoute") is None
    # The route is the operator's and NebariApp carries no timeout, so the
    # blob time limit reaches the gateway as a policy on the public route,
    # named the way the operator names it: "<NebariApp name>-public-route".
    app = kind(r, "NebariApp")
    policy = kind(r, "BackendTrafficPolicy")
    assert policy is not None, "no request timeout reaches the route that carries /v2"
    assert policy["apiVersion"] == "gateway.envoyproxy.io/v1alpha1"
    assert policy["metadata"]["namespace"] == app["metadata"]["namespace"]
    assert policy["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": app["metadata"]["name"] + "-public-route",
            }
        ],
        "timeout": {"http": {"requestTimeout": "930s"}},
    }, policy["spec"]


def case_serve_nebariapp_no_timeout(r: Rendered) -> None:
    # routeTimeout=false: /v2 is still a public route, and the chart attaches no policy.
    public = kind(r, "NebariApp")["spec"]["routing"]["publicRoutes"]
    assert public[-1] == {"pathPrefix": "/v2", "pathType": "PathPrefix"}, public
    assert kind(r, "BackendTrafficPolicy") is None


def case_serve_httproute(r: Rendered) -> None:
    # An explicit public URL adds a port to the ingress host, a 5 GiB limit renders
    # as an integer (not 5.36870912e+09), and /v2 gets its own rule, first,
    # with a request timeout that outlasts the blob time limit.
    env = serve_env(r)
    assert env["PUBLIC_URL"] == "https://hub.example.com:8443", env
    assert env["MAX_BLOB_BYTES"] == "5368709120", env
    assert env["MAX_BLOB_SECONDS"] == "1800" and env["CREDENTIAL_TTL_SECONDS"] == "600", env
    rules = kind(r, "HTTPRoute")["spec"]["rules"]
    assert rules[0]["matches"] == [{"path": {"type": "PathPrefix", "value": "/v2"}}], rules[0]
    assert rules[0]["timeouts"] == {"request": "1830s"}, rules[0]
    assert rules[0]["backendRefs"] == rules[1]["backendRefs"]
    assert rules[1]["matches"] == [{"path": {"type": "PathPrefix", "value": "/"}}] and "timeouts" not in rules[1]
    # The chart's own route carries the timeout; the Envoy Gateway policy is for the operator's.
    assert kind(r, "BackendTrafficPolicy") is None


def case_serve_httproute_no_timeout(r: Rendered) -> None:
    # routeTimeout=false (Gateway API CRDs without HTTPRoute timeouts): the
    # host's own "/" rule carries /v2, and no rule sets a timeout.
    assert serve_env(r)["PUBLIC_URL"] == "https://hub.example.com"
    rules = kind(r, "HTTPRoute")["spec"]["rules"]
    assert len(rules) == 1 and "timeouts" not in rules[0], rules


def case_run_controller(r: Rendered) -> None:
    # The API is told where the Cogs are; the controller is a second container of the same pod.
    assert r.value("COLLAB_HUB_API__RUNS__PACKAGES") == "/opt/collab-hub/cogs"
    controller = r.containers.get("run-controller")
    assert controller is not None, f"no run-controller container: {sorted(r.containers)}"
    assert controller["command"] == ["collab-hub-run-controller"]
    assert controller["image"] == r.containers["api"]["image"], "the controller runs another image"
    env = {e["name"]: e for e in controller["env"]}
    assert env["COLLAB_CONTROLLER_TRACK"]["valueFrom"]["secretKeyRef"] == {"name": "hub-db", "key": "url"}
    assert env["COLLAB_CONTROLLER_ID"]["value"] == "hub" and env["COLLAB_CONTROLLER_WAIT"]["value"] == "600"
    assert env["COLLAB_CONTROLLER_MODELS"]["value"] == "/etc/collab-hub/runs/models.toml"
    assert env["COLLAB_CONTROLLER_WORKER_START_TIMEOUT"]["value"] == "900"
    # The pod rolls when its models block changes: the controller reads it once, at start.
    assert re.fullmatch(r"[0-9a-f]{64}", r.annotations.get("checksum/run-models", "")), r.annotations
    # No AWS web identity in the container Cogs run in.
    assert r.annotations.get("eks.amazonaws.com/skip-containers") == "run-controller", r.annotations
    assert r.volumes["run-tmp"]["emptyDir"]["sizeLimit"] == "5Gi" and r.volumes["run-cache"]["emptyDir"]["sizeLimit"]
    assert env["ANTHROPIC_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "claude"
    # Liveness only: a readiness probe would hold the pod out while the controller waits for its id.
    assert "livenessProbe" in controller and "readinessProbe" not in controller and "startupProbe" not in controller
    assert controller["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {m["name"]: m["mountPath"] for m in controller["volumeMounts"]}
    assert mounts == {"run-cache": "/var/cache/collab-hub", "run-tmp": "/tmp", "run-models": "/etc/collab-hub/runs"}
    assert "frames-storage" not in mounts, "the controller reaches the API's frames"
    assert r.volumes["run-models"]["configMap"]["name"] == "t-collab-hub-run-models"
    assert 'auth_ref = "env:ANTHROPIC_API_KEY"' in r.configmaps["t-collab-hub-run-models"]["data"]["models.toml"]
    # At the local location the controller is the API pod's: no Deployment and no Role of its own.
    assert r.controller is None and not r.roles and not r.bindings
    assert r.value("COLLAB_HUB_API__RUNS__LOCATION") == "local"


def case_no_run_controller(r: Rendered) -> None:
    assert sorted(r.containers) == ["api"], sorted(r.containers)
    assert not r.annotations, r.annotations
    assert "COLLAB_HUB_API__RUNS__PACKAGES" not in r.env
    assert not r.configmaps.get("t-collab-hub-run-models")
    assert r.controller is None and not r.roles and not r.bindings


def case_run_controller_remote(r: Rendered) -> None:
    # The API pod runs the API alone, told where its Cogs and their workers are; it may create nothing.
    assert sorted(r.containers) == ["api"], sorted(r.containers)
    assert r.value("COLLAB_HUB_API__RUNS__PACKAGES") == "/opt/collab-hub/cogs"
    assert r.value("COLLAB_HUB_API__RUNS__LOCATION") == "remote"
    assert r.api.spec["replicas"] == 2  # more than one API pod is fine: the controller is not in it
    api_sa = r.api.pod["serviceAccountName"]
    assert not [b for b in r.bindings if any(s["name"] == api_sa for s in b["subjects"])], "the API holds a Role"
    # The controller: its own Deployment, one replica under one id, its own ServiceAccount and Role.
    controller = r.controller
    assert controller is not None, "no run-controller Deployment"
    assert controller.spec["replicas"] == 1 and controller.spec["strategy"] == {"type": "Recreate"}
    assert controller.container["command"] == ["collab-hub-run-controller"]
    assert controller.container["image"] == r.api.container["image"]
    assert controller.value("COLLAB_CONTROLLER_LOCATION") == "remote"
    assert controller.value("COLLAB_CONTROLLER_IMAGE") == r.api.container["image"]
    assert controller.value("COLLAB_CONTROLLER_ID") == "hub" and controller.value("COLLAB_CONTROLLER_NAMESPACE")
    assert controller.env["COLLAB_CONTROLLER_TRACK"]["valueFrom"]["secretKeyRef"] == {"name": "hub-db", "key": "url"}
    assert controller.value("COLLAB_CONTROLLER_MODELS") == "/etc/collab-hub/runs/models.toml"
    allow = json.loads(controller.value("COLLAB_CONTROLLER_ALLOW_INGRESS_FROM"))
    assert allow == controller.selector, (allow, controller.selector)  # only the controller's pods reach a worker
    assert "livenessProbe" in controller.container and "readinessProbe" not in controller.container
    assert re.fullmatch(r"[0-9a-f]{64}", controller.annotations.get("checksum/run-models", "")), controller.annotations
    sa = controller.pod["serviceAccountName"]
    assert sa == "t-collab-hub-run-controller" and sa in r.service_accounts
    [binding] = [b for b in r.bindings if any(s["name"] == sa for s in b["subjects"])]
    role = r.roles[binding["roleRef"]["name"]]
    rules = {(rule["apiGroups"][0], rule["resources"][0]): sorted(rule["verbs"]) for rule in role["rules"]}
    assert rules == {
        ("apps", "deployments"): ["create", "delete", "deletecollection", "get", "list", "patch", "watch"],
        ("", "services"): ["create", "delete", "deletecollection", "get", "list"],
        ("networking.k8s.io", "networkpolicies"): ["create", "delete", "deletecollection", "get", "list", "patch"],
        ("", "secrets"): ["create", "delete", "deletecollection", "patch"],  # written, never read
    }, rules
    # A worker pod's resources and volume limits reach the controller from the chart.
    assert json.loads(controller.value("COLLAB_CONTROLLER_WORKER_RESOURCES")) == {
        "requests": {"cpu": "100m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "4Gi"}}
    assert controller.value("COLLAB_CONTROLLER_WORKER_CACHE_SIZE_LIMIT") == "10Gi"
    assert controller.value("COLLAB_CONTROLLER_WORKER_TMP_SIZE_LIMIT") == "2Gi"
    assert "run-cache" not in r.volumes and "run-tmp" not in r.volumes  # nothing of a worker's on the API pod


CASES = {
    "serve-off": case_serve_off,
    "serve-with-indexer": case_serve_with_indexer,
    "publish-with-indexer": case_publish_with_indexer,
    "serve-nebariapp": case_serve_nebariapp,
    "serve-nebariapp-no-timeout": case_serve_nebariapp_no_timeout,
    "serve-httproute": case_serve_httproute,
    "serve-httproute-no-timeout": case_serve_httproute_no_timeout,
    "run-controller": case_run_controller,
    "run-controller-remote": case_run_controller_remote,
    "no-run-controller": case_no_run_controller,
    "features": case_features,
    "default": case_default,
    "fixture": case_fixture,
    "static-only": case_static_only,
    "three-sources": case_three_sources,
    "reordered": case_reordered,
    "indexer-resources": case_indexer_resources,
}


def main() -> int:
    case = sys.argv[1]
    rendered = Rendered([d for d in yaml.safe_load_all(sys.stdin) if d])
    try:
        CASES[case](rendered)
    except AssertionError as exc:
        print(f"{case}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
