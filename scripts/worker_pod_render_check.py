#!/usr/bin/env python3
"""The worker pod a rendered run controller makes: its Cog pulled by digest in an init container.

    helm template t helm/collab-hub -f <values> | PYTHONPATH=execution/src python3 scripts/worker_pod_render_check.py

The chart renders the controller; the controller makes each worker's
Deployment at run time (``KubernetesCogExecutor``). This builds that
Deployment from the rendered controller's environment, the way the
controller would, for a Cog installed by digest, and asserts what the chart
promises of it: an init container of the hub's image pulls the artifact at
its pinned digest into the cache, the main container serves the package
from there, and the cache is the claim the values name. Needs the execution
package on the path, its dependency httpx, and PyYAML.
"""

from __future__ import annotations

import json
import sys

import yaml

from collab_hub_execution.kubernetes import KubernetesCogExecutor, resource_name

REFERENCE = "hub.example.com/cogs/notes@sha256:" + "a" * 64
LOCK_DIGEST = "sha256:" + "b" * 64


def main() -> int:
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    controller = next(d for d in docs if d.get("kind") == "Deployment"
                      and d["metadata"]["labels"].get("app.kubernetes.io/component") == "run-controller")
    [container] = controller["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    executor = KubernetesCogExecutor(
        runner_image=env["COLLAB_CONTROLLER_IMAGE"], namespace=env["COLLAB_CONTROLLER_NAMESPACE"],
        allow_ingress_from=json.loads(env["COLLAB_CONTROLLER_ALLOW_INGRESS_FROM"]),
        resources=json.loads(env["COLLAB_CONTROLLER_WORKER_RESOURCES"]),
        cache_size_limit=env["COLLAB_CONTROLLER_WORKER_CACHE_SIZE_LIMIT"],
        tmp_size_limit=env["COLLAB_CONTROLLER_WORKER_TMP_SIZE_LIMIT"],
        cache_claim=env.get("COLLAB_CONTROLLER_WORKER_CACHE_CLAIM"),
        registry_credential="user:secret" if "COLLAB_CONTROLLER_REGISTRY_CREDENTIAL_ENV" in env else None,
        registry_plain_http=(env.get("COLLAB_CONTROLLER_REGISTRY_PLAIN_HTTP") or "").split(","),
    )
    name = resource_name(REFERENCE, "run-1", "ask:0")
    deployment, _ = executor._manifests(REFERENCE, "run-1", name, files={"binding.json": "{}"}, lock_digest=LOCK_DIGEST)
    pod = deployment["spec"]["template"]["spec"]
    [pull] = pod["initContainers"]
    assert pull["image"] == env["COLLAB_CONTROLLER_IMAGE"], pull["image"]
    assert pull["command"][:4] == ["python", "-m", "collab_hub_execution.artifacts", "materialize"], pull["command"]
    assert pull["command"][4] == REFERENCE and "@sha256:" in pull["command"][4], "the pull is not by digest"
    assert pull["envFrom"] == [{"secretRef": {"name": f"{name}-pull"}}], "the credential reaches the pull alone"
    [worker] = pod["containers"]
    assert worker["command"][:4] == ["python", "-m", "collab_hub_execution.artifacts", "serve"], worker["command"]
    assert worker["envFrom"] == [{"secretRef": {"name": name}}]
    assert not [c for c in worker["command"] if "/opt/collab-hub/cogs" in c], "the worker runs from the image"
    # The Cog's container: its package read-only, the environment of its lock alone writable, nothing else of
    # the cache; the pull container the whole cache, to land the package and make the environment directory.
    cache_mounts = {m["mountPath"]: m for m in worker["volumeMounts"] if m["name"] == "cache"}
    package = f"/var/cache/collab-hub/artifacts/packages/{'a' * 64}"
    environment = f"/var/cache/collab-hub/artifacts/envs/{'b' * 64}"
    assert set(cache_mounts) == {package, environment}, cache_mounts
    assert cache_mounts[package]["readOnly"] is True
    assert cache_mounts[package]["subPath"] == f"artifacts/packages/{'a' * 64}"
    assert not cache_mounts[environment].get("readOnly")
    assert cache_mounts[environment]["subPath"] == f"artifacts/envs/{'b' * 64}"
    assert [m["mountPath"] for m in pull["volumeMounts"] if m["name"] == "cache"] == ["/var/cache/collab-hub"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    if env.get("COLLAB_CONTROLLER_WORKER_CACHE_CLAIM"):
        assert volumes["cache"]["persistentVolumeClaim"]["claimName"] == env["COLLAB_CONTROLLER_WORKER_CACHE_CLAIM"]
    else:
        assert "emptyDir" in volumes["cache"]
    assert volumes["binding"]["secret"] == {"secretName": name, "items": [{"key": "binding.json",
                                                                             "path": "binding.json"}]}
    worker_env = {e["name"]: e["value"] for e in worker["env"]}
    assert worker_env["COLLAB_BINDING_FILE"] == "/etc/collab-hub/binding/binding.json"
    plain = {e["name"]: e["value"] for e in pull["env"]}.get("COLLAB_REGISTRY_PLAIN_HTTP")
    assert plain == (env.get("COLLAB_CONTROLLER_REGISTRY_PLAIN_HTTP") or None), plain
    print(f"worker {name}: pulls {REFERENCE} in {pull['name']}, serves it from its read-only package")
    return 0


if __name__ == "__main__":
    sys.exit(main())
