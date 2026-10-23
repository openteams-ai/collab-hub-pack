"""Test doubles shared by the cog registry suites.

``FakeOCIClient`` mirrors the public surface of ``cogs.oci.OCIClient`` without
touching its implementation, so these suites run against the interface
skeleton and stay valid once the real client lands. ``harbor_rest_handler``
builds an ``httpx.MockTransport`` handler serving the two Harbor REST routes
the adapter uses, from the same canned data the static fake is seeded with, so
the contract suite can assert identical results from both adapters.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_OCI_MANIFEST,
    BasicCredentials,
    Descriptor,
    Manifest,
    OCINotFound,
    OCITooLarge,
)

HOST = "registry.example"
URL = f"https://{HOST}"
API_HOST = "registry-core.internal.svc"
API_URL = f"http://{API_HOST}"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64

PUSHED_A = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
PUSHED_B = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

# Canned registry content: two repositories under one project. Tags for
# DIGEST_A are two; DIGEST_C carries no timestamp so both adapters must yield
# pushed_at=None for it.
REPOSITORIES = ["cogs/alpha", "cogs/beta"]
ARTIFACTS: dict[str, list[dict]] = {
    "cogs/alpha": [
        {"digest": DIGEST_B, "tags": ["v0"], "pushed_at": PUSHED_B, "media_type": MEDIA_TYPE_OCI_MANIFEST},
        {"digest": DIGEST_A, "tags": ["v1", "latest"], "pushed_at": PUSHED_A, "media_type": MEDIA_TYPE_OCI_MANIFEST},
    ],
    "cogs/beta": [
        {"digest": DIGEST_C, "tags": ["latest"], "pushed_at": None, "media_type": MEDIA_TYPE_OCI_MANIFEST},
    ],
}


def _rfc3339(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if value else None


def manifest_for(entry: dict) -> Manifest:
    """The manifest for one canned entry; ``layers`` (a tuple of ``Descriptor``) is optional and defaults to none."""

    annotations = {}
    if entry["pushed_at"] is not None:
        annotations["org.opencontainers.image.created"] = _rfc3339(entry["pushed_at"])
    return Manifest(
        media_type=entry["media_type"],
        digest=entry["digest"],
        config=Descriptor(media_type="application/vnd.pixi.config.v1+toml", digest="sha256:" + "0" * 64, size=0),
        layers=tuple(entry.get("layers", ())),
        annotations=annotations,
    )


@dataclass
class FakeOCIClient:
    """Same method names and constructor keywords as ``OCIClient``; canned answers."""

    base_url: str
    credentials: BasicCredentials | None = None
    token_url: str | None = None
    ca_bundle_path: str | None = None
    timeout_seconds: float = 10.0
    max_manifest_bytes: int = 0
    transport: httpx.AsyncBaseTransport | None = None
    redirect_hosts: tuple[str, ...] = ()
    restrict_redirects: bool = False
    tags: dict[str, list[str]] = field(default_factory=dict)
    manifests: dict[tuple[str, str], Manifest] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    closed: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def seed(self, artifacts: dict[str, list[dict]]) -> None:
        """Load canned entries; an entry's optional ``blobs`` ({digest: bytes}) become fetchable layers."""

        for repo, entries in artifacts.items():
            self.tags.setdefault(repo, [])
            for entry in entries:
                manifest = manifest_for(entry)
                for tag in entry["tags"]:
                    self.tags[repo].append(tag)
                    self.manifests[(repo, tag)] = manifest
                self.manifests[(repo, entry["digest"])] = manifest
                self.blobs.update(entry.get("blobs", {}))

    async def list_tags(self, repo: str) -> list[str]:
        self.calls.append(("list_tags", repo))
        if repo not in self.tags:
            raise OCINotFound(repo)
        return list(self.tags[repo])

    async def get_manifest(self, repo: str, ref: str) -> Manifest:
        self.calls.append(("get_manifest", repo, ref))
        try:
            return self.manifests[(repo, ref)]
        except KeyError:
            raise OCINotFound(f"{repo}:{ref}") from None

    async def get_blob(self, repo: str, descriptor: Descriptor | str, *, max_bytes: int) -> bytes:
        digest = descriptor if isinstance(descriptor, str) else descriptor.digest
        self.calls.append(("get_blob", repo, digest))
        if isinstance(descriptor, Descriptor) and descriptor.size > max_bytes:
            raise OCITooLarge(f"blob {digest} is {descriptor.size} bytes, cap is {max_bytes}")
        try:
            return self.blobs[digest]
        except KeyError:
            raise OCINotFound(digest) from None

    async def aclose(self) -> None:
        self.closed += 1


class FakeOCIFactory:
    """An ``oci_client_factory`` that records every client it builds and seeds it with canned content."""

    def __init__(self, artifacts: dict[str, list[dict]] | None = None) -> None:
        self.artifacts = ARTIFACTS if artifacts is None else artifacts
        self.clients: list[FakeOCIClient] = []

    def __call__(self, base_url: str, **kwargs) -> FakeOCIClient:
        client = FakeOCIClient(base_url, **kwargs)
        client.seed(self.artifacts)
        self.clients.append(client)
        return client


def harbor_artifact_json(entry: dict) -> dict:
    """One element of Harbor's ``GET .../artifacts?with_tag=true`` answer."""

    return {
        "digest": entry["digest"],
        "manifest_media_type": entry["media_type"],
        "media_type": "application/vnd.pixi.config.v1+toml",
        "push_time": _rfc3339(entry["pushed_at"]),
        "tags": [{"name": tag, "immutable": False} for tag in entry["tags"]] or None,
        "type": "UNKNOWN",
    }


def harbor_rest_handler(
    *,
    repositories: list[str] | None = None,
    artifacts: dict[str, list[dict]] | None = None,
    project: str = "cogs",
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the two Harbor REST routes from canned data (single page, no ``Link``)."""

    repositories = REPOSITORIES if repositories is None else repositories
    artifacts = ARTIFACTS if artifacts is None else artifacts

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        prefix = f"/api/v2.0/projects/{project}/repositories"
        if path == prefix:
            # Reverse order: the adapter, not the registry, owns the sort.
            body = [{"name": name, "project_id": 2, "artifact_count": 1} for name in reversed(repositories)]
            return httpx.Response(200, json=body, headers={"X-Total-Count": str(len(body))})
        if path.startswith(prefix + "/") and path.endswith("/artifacts"):
            encoded = path[len(prefix) + 1 : -len("/artifacts")]
            repo = f"{project}/{encoded.replace('%252F', '/')}"
            if repo not in artifacts:
                return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND", "message": "not found"}]})
            return httpx.Response(200, json=[harbor_artifact_json(entry) for entry in artifacts[repo]])
        return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND", "message": path}]})

    return handler


def index_document(repositories: list[str]) -> bytes:
    entries = []
    for repo in repositories:
        namespace, _, name = repo.partition("/")
        entries.append({"namespace": namespace, "name": name})
    return json.dumps({"schemaVersion": 1, "repositories": entries}).encode()
