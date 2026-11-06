"""Pulls through the Hub (issue #179): the ``/v2/`` surface against a fake backing registry.

The fake registry is an ``httpx.MockTransport`` handed to a real ``static``
source, so everything between the Hub's router and the wire is the code that
runs in production: the generic OCI client, its bearer-token dance with the
*Hub's own* source credential, and its redirect following. The fake demands
that credential, redirects blobs to a "signed" object-storage URL on another
host, and can be told to fail -- with error bodies and ``Location`` headers
that name it -- so the leak tests have something to leak.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from collab_hub_api import config as config_module
from collab_hub_api.cogs.catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
)
from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_NEBI_ASSET,
    MEDIA_TYPE_OCI_INDEX,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_CONFIG,
    MEDIA_TYPE_PIXI_LOCK,
    MEDIA_TYPE_PIXI_TOML,
)
from collab_hub_api.cogs.registry import build_registry_sources
from collab_hub_api.cogs.registry_credentials import CREDENTIAL_SECRET_PREFIX, PULL_TOKEN_PREFIX
from collab_hub_api.config import Config, recommended_path_rules
from collab_hub_api.core import make_app
from collab_hub_api.frames.auth import AuthContext, NoOrganizationError
from collab_hub_api.routers import cogs as cogs_router
from collab_hub_api.routers import registry as registry_router
from collab_hub_api.routers.registry import BlobStreamAborted, parse_registry_path, requested_repositories

HUB_URL = "https://hub.example"
HUB_HOST = "hub.example"

BACKING_HOST = "backing.registry.internal"
BACKING_URL = f"https://{BACKING_HOST}"
STORAGE_HOST = "blobstore.backing.internal"
UPSTREAM_USER = "robot$hub"
UPSTREAM_PASSWORD = "upstream-robot-password-9f3"
UPSTREAM_TOKEN = "upstream-bearer-token-71c"
STORAGE_SIGNATURE = "presigned-signature-4be"
UPLOAD_STATE = "upstream-upload-state-5d2"

SECRETS = (UPSTREAM_PASSWORD, UPSTREAM_TOKEN, STORAGE_SIGNATURE, UPLOAD_STATE)
"""Nothing here may appear in a response or in a log line."""
BACKING_NAMES = (BACKING_HOST, STORAGE_HOST)
"""Nothing here may appear in a response to a client."""

REPO = "cogs/cog-alpha"
OTHER_REPO = "cogs/cog-beta"
UNINDEXED_REPO = "cogs/not-indexed"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def descriptor(media_type: str, data: bytes, title: str | None = None) -> dict:
    entry: dict = {"mediaType": media_type, "digest": sha256(data), "size": len(data)}
    if title:
        entry["annotations"] = {"org.opencontainers.image.title": title}
    return entry


class Bundle:
    """One nebi-shaped artifact: a pixi config blob and one layer per bundled file."""

    def __init__(self, seed: str, *, big: int = 0) -> None:
        self.config = f'{{"seed": "{seed}"}}'.encode()
        self.files = {
            "pixi.toml": (MEDIA_TYPE_PIXI_TOML, f'[workspace]\nname = "{seed}"\n'.encode()),
            "pixi.lock": (MEDIA_TYPE_PIXI_LOCK, f"version: 6\n# {seed}\n".encode()),
            "COG.md": (MEDIA_TYPE_NEBI_ASSET, f"---\nname: {seed}\n---\n# {seed}\n".encode()),
        }
        if big:
            # Several stream chunks, so the relay is exercised as a stream.
            self.files["model.bin"] = (MEDIA_TYPE_NEBI_ASSET, os.urandom(big))
        document = {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": descriptor(MEDIA_TYPE_PIXI_CONFIG, self.config),
            "layers": [descriptor(media_type, data, title) for title, (media_type, data) in self.files.items()],
        }
        self.manifest = json.dumps(document, indent=2).encode()
        self.digest = sha256(self.manifest)

    @property
    def blobs(self) -> dict[str, bytes]:
        blobs = {sha256(self.config): self.config}
        blobs.update({sha256(data): data for _media_type, data in self.files.values()})
        return blobs


class FakeRegistry:
    """A private registry behind a token endpoint, with blobs on a separate "object storage" host."""

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str], tuple[str, bytes]] = {}
        self.blobs: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, httpx.Response] = {}
        self.tamper: dict[str, bytes] = {}
        self.blob_delay = 0.0
        self.stream_delay = 0.0
        self.storage_scheme = "https"
        # The push half: upload sessions by id, and switches for the failures a registry can answer with.
        self.uploads: dict[str, bytearray] = {}
        self.opened = 0
        self.upload_delay = 0.0
        self.upload_gate: asyncio.Event | None = None
        self.refuse_writes: httpx.Response | None = None
        self.storage_saw_authorization = False

    def publish(self, repo: str, bundle: Bundle, *tags: str) -> None:
        for ref in (bundle.digest, *tags):
            self.manifests[(repo, ref)] = (MEDIA_TYPE_OCI_MANIFEST, bundle.manifest)
        self.blobs.update(bundle.blobs)

    def publish_raw(self, repo: str, media_type: str, body: bytes, *tags: str) -> str:
        digest = sha256(body)
        for ref in (digest, *tags):
            self.manifests[(repo, ref)] = (media_type, body)
        return digest

    def writes(self) -> list[str]:
        """``METHOD path`` of every write the registry was sent with its credential (the 401 dance excluded)."""

        return [
            f"{request.method} {request.url.path}"
            for request in self.requests
            if request.method in ("POST", "PATCH", "PUT", "DELETE")
            and request.headers.get("authorization") == f"Bearer {UPSTREAM_TOKEN}"
        ]

    async def _write(self, request: httpx.Request, rest: str) -> httpx.Response:
        """The push half of the distribution API, as a registry answers it (absolute, stateful upload URLs)."""

        if request.method == "HEAD" and "/manifests/" in rest:
            repo, _, ref = rest.rpartition("/manifests/")
            return httpx.Response(200 if (repo, ref) in self.manifests else 404)
        if request.method == "HEAD":
            digest = rest.rpartition("/blobs/")[2]
            if digest in self.blobs:
                return httpx.Response(200, headers={"Content-Length": str(len(self.blobs[digest]))})
            return httpx.Response(404)
        if self.refuse_writes is not None:
            return self.refuse_writes
        body = await request.aread()
        if "/blobs/uploads" in rest:
            repo, _, upload = rest.partition("/blobs/uploads")
            upload = upload.strip("/")
            location = f"{BACKING_URL}/v2/{repo}/blobs/uploads/{{}}?_state={UPLOAD_STATE}"
            if request.method == "POST":
                self.opened += 1
                upload = f"upstream-{self.opened}"
                self.uploads[upload] = bytearray()
                return httpx.Response(202, headers={"Location": location.format(upload), "Range": "0-0"})
            if upload not in self.uploads or request.url.params.get("_state") != UPLOAD_STATE:
                return httpx.Response(404, text=f"{BACKING_HOST}: unknown upload")
            received = self.uploads[upload]
            if request.method == "GET":
                return httpx.Response(204, headers={"Location": location.format(upload)})
            if request.method == "DELETE":
                del self.uploads[upload]
                return httpx.Response(204)
            if self.upload_delay:
                await asyncio.sleep(self.upload_delay)
            if self.upload_gate is not None:
                # Held at the registry until the test lets it through.
                await self.upload_gate.wait()
            content_range = request.headers.get("content-range")
            if content_range is not None and int(content_range.split("-")[0]) != len(received):
                return httpx.Response(416, text=f"{BACKING_HOST}: out of order")
            received.extend(body)
            if request.method == "PATCH":
                return httpx.Response(202, headers={"Location": location.format(upload)})
            digest = request.url.params.get("digest")
            if sha256(bytes(received)) != digest:
                return httpx.Response(400, text=f"digest invalid at {BACKING_HOST} for {UPSTREAM_USER}")
            self.blobs[digest] = bytes(received)
            del self.uploads[upload]
            return httpx.Response(201, headers={"Location": f"{BACKING_URL}/v2/{repo}/blobs/{digest}"})
        if request.method == "PUT" and "/manifests/" in rest:
            repo, _, ref = rest.rpartition("/manifests/")
            media_type = request.headers.get("content-type", MEDIA_TYPE_OCI_MANIFEST)
            for name in {ref, sha256(body)}:
                self.manifests[(repo, name)] = (media_type, body)
            return httpx.Response(201, headers={"Location": f"{BACKING_URL}/v2/{repo}/manifests/{sha256(body)}"})
        return httpx.Response(405, text=f"{BACKING_HOST}: unsupported")

    async def _slowly(self, body: bytes):
        for offset in range(0, len(body), 8):
            await asyncio.sleep(self.stream_delay)
            yield body[offset : offset + 8]

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def served(self, marker: str) -> int:
        """Requests for ``marker`` the registry answered, i.e. not counting the 401 that starts the token dance."""

        return sum(
            marker in request.url.path and request.headers.get("authorization") == f"Bearer {UPSTREAM_TOKEN}"
            for request in self.requests
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == STORAGE_HOST:
            if "authorization" in request.headers:
                self.storage_saw_authorization = True
            if STORAGE_SIGNATURE not in str(request.url):
                return httpx.Response(403, text="signature required")
            digest = path.rsplit("/", 1)[-1]
            if self.blob_delay:
                await asyncio.sleep(self.blob_delay)
            body = self.tamper.get(digest, self.blobs[digest])
            if self.stream_delay:
                return httpx.Response(200, content=self._slowly(body), headers={"Content-Length": str(len(body))})
            return httpx.Response(200, content=body)
        assert request.url.host == BACKING_HOST, request.url
        if path == "/token":
            expected = "Basic " + base64.b64encode(f"{UPSTREAM_USER}:{UPSTREAM_PASSWORD}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, text="bad credential")
            return httpx.Response(200, json={"token": UPSTREAM_TOKEN, "expires_in": 300})
        if request.headers.get("authorization") != f"Bearer {UPSTREAM_TOKEN}":
            challenge = f'Bearer realm="{BACKING_URL}/token",service="{BACKING_HOST}"'
            return httpx.Response(401, headers={"WWW-Authenticate": challenge}, text="unauthorized")
        if path in self.fail:
            return self.fail[path]
        _v2, _, rest = path.partition("/v2/")
        if request.method != "GET" or "/blobs/uploads/" in rest:
            return await self._write(request, rest)
        if rest.endswith("/tags/list"):
            repo = rest[: -len("/tags/list")]
            tags = sorted(ref for (name, ref) in self.manifests if name == repo and not ref.startswith("sha256:"))
            return httpx.Response(200, json={"name": repo, "tags": tags})
        if "/manifests/" in rest:
            repo, _, ref = rest.rpartition("/manifests/")
            if (repo, ref) not in self.manifests:
                return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN", "message": BACKING_HOST}]})
            media_type, body = self.manifests[(repo, ref)]
            return httpx.Response(
                200, content=body, headers={"Content-Type": media_type, "Docker-Content-Digest": sha256(body)}
            )
        if "/blobs/" in rest:
            digest = rest.rpartition("/blobs/")[2]
            if digest not in self.blobs:
                return httpx.Response(404, json={"errors": [{"code": "BLOB_UNKNOWN", "message": BACKING_HOST}]})
            location = f"{self.storage_scheme}://{STORAGE_HOST}/store/{digest}?X-Signature={STORAGE_SIGNATURE}"
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(404, text=f"{BACKING_HOST}: no such route")


def bearer(payload: dict) -> dict[str, str]:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    return {"Authorization": f"Bearer {encode({'alg': 'none'})}.{encode(payload)}."}


ALICE = bearer({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "ws", "sid": "session-alice"})
BOB = bearer({"preferred_username": "bob", "org_id": "org-a", "workspace_id": "ws"})


def basic(username: str, secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()}


def catalog_row(repo: str, digest: str, *, tags=("latest",), source_id="backing", status=STATUS_INDEXED, **kwargs):
    indexed = status == STATUS_INDEXED
    return CogArtifact(
        source_id=source_id,
        host=BACKING_HOST,
        repository=repo,
        digest=digest,
        status=status,
        tags=tuple(tags),
        pushed_at=kwargs.pop("pushed_at", T0),
        card={"id": f"example/{repo.rsplit('/', 1)[-1]}", "name": repo} if indexed else None,
        cog_id=f"example/{repo.rsplit('/', 1)[-1]}" if indexed else None,
        name=repo if indexed else None,
        **kwargs,
    )


def settings(tmp_path, *, serve: dict | None = None, security: dict | None = None, publish: dict | None = None) -> dict:
    result: dict = {
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"mcp_session_manager_enabled": False},
        "tasks": {"backend": "memory"},
        "cogs": {
            "catalog": {"backend": "memory"},
            "registry_sources": [
                {
                    "id": "backing",
                    "kind": "static",
                    "url": BACKING_URL,
                    "repositories": [REPO],
                    "credentials": {"username": UPSTREAM_USER, "password": UPSTREAM_PASSWORD},
                }
            ],
            "serve": {"enabled": True, "public_url": HUB_URL} if serve is None else serve,
        },
    }
    if security is not None:
        result["security"] = security
    if publish is not None:
        # Publishing on: pushes are written through to the one source, and this is who may push.
        result["cogs"]["registry_sources"][0]["publish"] = True
        result["cogs"]["publish"] = publish
    return result


class Hub:
    """One app with its fake backing registry, and the handful of client moves the tests repeat."""

    def __init__(self, app, client: AsyncClient, upstream: FakeRegistry) -> None:
        self.app = app
        self.client = client
        self.upstream = upstream
        self.responses: list[httpx.Response] = []

    @property
    def serving(self):
        return self.app.state.cog_registry_serving

    @property
    def catalog(self):
        return self.app.state.cog_catalog_store

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        response = await self.client.request(method, url, **kwargs)
        self.responses.append(response)
        return response

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def exchange(self, who: dict[str, str] = ALICE) -> dict:
        response = await self.request("POST", "/v1/cogs/registry-credentials", headers=who)
        assert response.status_code == 201, response.text
        return response.json()

    async def token(self, credential: dict, *repositories: str) -> str:
        response = await self.get(
            "/v2/token",
            params={"service": HUB_HOST, "scope": [f"repository:{name}:pull" for name in repositories]},
            headers=basic(credential["username"], credential["secret"]),
        )
        assert response.status_code == 200, response.text
        return response.json()["token"]

    async def pull_token(self, *repositories: str, who: dict[str, str] = ALICE) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token(await self.exchange(who), *repositories)}"}

    def seed(self, repo: str, bundle: Bundle, *tags: str, **kwargs) -> None:
        self.upstream.publish(repo, bundle, *tags)
        self.catalog.upsert(catalog_row(repo, bundle.digest, tags=tags, **kwargs))


@pytest_asyncio.fixture
async def make_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    stack: list = []

    async def build(**kwargs) -> Hub:
        upstream = FakeRegistry()
        transport = httpx.MockTransport(upstream)
        monkeypatch.setattr(
            config_module,
            "build_registry_sources",
            lambda configs, **kwargs: build_registry_sources(configs, http_transport=transport, **kwargs),
        )
        app = make_app(Config.parse(settings(tmp_path, **kwargs)))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        stack.append((lifespan, client))
        return Hub(app, client, upstream)

    yield build
    for lifespan, client in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def hub(make_hub) -> Hub:
    return await make_hub()


ALPHA = Bundle("alpha", big=200_000)
BETA = Bundle("beta")


# -- the challenge and the token endpoint ---------------------------------------


async def test_base_endpoint_challenges_then_admits_a_pull_token(hub: Hub):
    for method in ("GET", "HEAD"):
        refused = await hub.request(method, "/v2/")
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == f'Bearer realm="{HUB_URL}/v2/token",service="{HUB_HOST}"'
        assert refused.headers["docker-distribution-api-version"] == "registry/2.0"
    assert (await hub.get("/v2/")).json() == {
        "errors": [{"code": "UNAUTHORIZED", "message": "authentication required", "detail": {}}]
    }

    headers = await hub.pull_token()
    admitted = await hub.get("/v2/", headers=headers)
    assert admitted.status_code == 200 and admitted.json() == {}
    assert admitted.headers["docker-distribution-api-version"] == "registry/2.0"
    assert (await hub.get("/v2", headers=headers)).status_code == 200


async def test_exchange_answers_the_documented_contract(hub: Hub):
    before = datetime.now(UTC)
    response = await hub.request("POST", "/v1/cogs/registry-credentials", headers=ALICE, json={"scope": "pull"})
    assert response.status_code == 201
    body = response.json()
    assert sorted(body) == ["expires_at", "id", "registry", "scope", "secret", "username"]
    assert body["registry"] == HUB_HOST
    assert body["scope"] == "pull"
    assert body["username"] == body["id"]
    assert body["secret"].startswith(CREDENTIAL_SECRET_PREFIX)
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert expires_at.utcoffset() == timedelta(0)
    # The default lifetime: fifteen minutes.
    assert timedelta(minutes=14) < expires_at - before <= timedelta(minutes=15, seconds=5)
    # The session the credential was exchanged from is recorded with it.
    stored, secret_hash = hub.serving.credentials._credentials[body["id"]]
    assert stored.session_id == "session-alice" and stored.user_id == "alice"
    # Only a digest of the secret is kept.
    assert body["secret"] not in secret_hash and len(secret_hash) == 64


async def test_an_exchange_verifies_the_hub_token_once(hub: Hub, monkeypatch):
    """Authentication and the recorded session id read the same verified claims, not two verifications."""

    from collab_hub_api.frames import auth

    decoded: list[str] = []
    decode = auth.decode_bearer_payload
    monkeypatch.setattr(auth, "decode_bearer_payload", lambda token: (decoded.append(token), decode(token))[1])
    body = await hub.exchange()
    assert len(decoded) == 1
    assert hub.serving.credentials._credentials[body["id"]][0].session_id == "session-alice"
    # Per request, never across them: the next request verifies its own token.
    other = await hub.exchange(BOB)
    assert len(decoded) == 2
    assert hub.serving.credentials._credentials[other["id"]][0].session_id is None
    # A token that fails verification is remembered as failed for the request, not retried into a pass.
    refused = await hub.request("POST", "/v1/cogs/registry-credentials", headers={"Authorization": "Bearer nope"})
    assert refused.status_code == 401 and len(decoded) == 3


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_a_spent_budget_on_the_exchange_routes_is_a_503_not_a_500(hub: Hub, monkeypatch, method):
    """``BudgetExhausted`` is a TimeoutError of the serving code's own; no psycopg handler matches it."""

    from collab_hub_api.cogs.deadline import BudgetExhausted

    def spent(*_args, **_kwargs):
        raise BudgetExhausted("the request's time budget is spent")

    for name in ("create_credential", "revoke_all", "revoke_credential"):
        monkeypatch.setattr(hub.serving.credentials, name, spent)
    urls = ["/v1/cogs/registry-credentials"]
    if method == "DELETE":
        urls.append("/v1/cogs/registry-credentials/crc-0123")
    for url in urls:
        response = await hub.request(method, url, headers=ALICE)
        assert response.status_code == 503, (url, response.text)
        assert response.json()["error"]["code"] == "database_unavailable"


def test_a_route_can_omit_more_than_the_anonymous_cut_but_never_less():
    from collab_hub_api.cogs.models import CogLocation, CogReference

    merged = cogs_router._merged_exclude(CogReference.ANONYMOUS_EXCLUDE, CogReference.BACKING_EXCLUDE)
    assert merged == {
        "source_id": True,
        "backing_reference": True,
        "locations": {"__all__": {**CogLocation.ANONYMOUS_EXCLUDE, "backing_reference": True}},
    }
    # Neither input is changed, and a field one side drops whole stays dropped whole.
    assert "backing_reference" not in CogReference.ANONYMOUS_EXCLUDE
    assert "backing_reference" not in CogLocation.ANONYMOUS_EXCLUDE
    assert cogs_router._merged_exclude({"locations": True}, CogReference.BACKING_EXCLUDE)["locations"] is True
    assert cogs_router._merged_exclude({}, {}) == {}


async def test_the_reference_route_applies_every_anonymous_exclusion_its_models_declare(hub: Hub, monkeypatch):
    """The route has no cut of its own: a key added to a model's anonymous cut is dropped here too."""

    from collab_hub_api.cogs.models import CogLocation, CogReference

    hub.seed(REPO, ALPHA, "latest")
    hub.catalog.upsert(catalog_row("mirror/cog-alpha", ALPHA.digest, tags=("latest",)))
    monkeypatch.setattr(CogLocation, "ANONYMOUS_EXCLUDE", {**CogLocation.ANONYMOUS_EXCLUDE, "pushed_at": True})
    monkeypatch.setattr(
        CogReference,
        "ANONYMOUS_EXCLUDE",
        {"source_id": True, "present": True, "locations": {"__all__": CogLocation.ANONYMOUS_EXCLUDE}},
    )
    url = f"/v1/cogs/example/cog-alpha/versions/{ALPHA.digest}/reference"
    hub.app.dependency_overrides[cogs_router.get_catalog_caller] = lambda: None
    answer = (await hub.client.get(url)).json()
    assert "present" not in answer and "source_id" not in answer and "backing_reference" not in answer
    assert answer["locations"] and all(
        "pushed_at" not in location and "source_id" not in location and "backing_reference" not in location
        for location in answer["locations"]
    )
    # A signed-in caller still gets the model whole (less the backing location, which is an operator's).
    hub.app.dependency_overrides.pop(cogs_router.get_catalog_caller)
    answer = (await hub.get(url, headers=ALICE)).json()
    assert answer["present"] is True and "pushed_at" in answer["locations"][0]
    assert "backing_reference" not in answer


async def test_exchange_refuses_unknown_scopes_and_requires_a_hub_session(hub: Hub):
    for body in ({"scope": "publish"}, {"scope": "pull", "extra": 1}):
        refused = await hub.request("POST", "/v1/cogs/registry-credentials", headers=ALICE, json=body)
        assert refused.status_code == 422, body
        assert refused.json()["error"]["code"] == "validation_error"
    anonymous = await hub.request("POST", "/v1/cogs/registry-credentials")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthorized"


async def test_token_endpoint_answers_the_distribution_token_shape(hub: Hub):
    credential = await hub.exchange()
    response = await hub.get(
        "/v2/token",
        params={"service": HUB_HOST, "scope": f"repository:{REPO}:pull", "account": "ignored"},
        headers=basic(credential["username"], credential["secret"]),
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body) == ["access_token", "expires_in", "issued_at", "token"]
    assert body["token"] == body["access_token"] and body["token"].startswith(PULL_TOKEN_PREFIX)
    assert body["expires_in"] == 300
    assert datetime.strptime(body["issued_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert response.headers["cache-control"] == "no-store"


async def test_token_endpoint_also_takes_a_hub_access_token(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    response = await hub.get("/v2/token", params={"scope": f"repository:{REPO}:pull"}, headers=ALICE)
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic not-base64!"},
        {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
        basic("crc-unknown", "chrs_wrong"),
        {"Authorization": "Bearer not-a-jwt"},
    ],
)
async def test_token_endpoint_refuses_without_a_usable_credential(hub: Hub, headers):
    refused = await hub.get("/v2/token", params={"scope": f"repository:{REPO}:pull"}, headers=headers)
    assert refused.status_code == 401
    assert refused.json()["errors"][0]["code"] == "UNAUTHORIZED"
    assert refused.headers["www-authenticate"] == f'Basic realm="{HUB_HOST}"'


async def test_a_wrong_secret_is_refused(hub: Hub):
    credential = await hub.exchange()
    refused = await hub.get("/v2/token", headers=basic(credential["username"], credential["secret"] + "x"))
    assert refused.status_code == 401


async def test_a_token_is_scoped_to_the_repositories_it_was_minted_for(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    hub.seed(OTHER_REPO, BETA, "latest")
    headers = await hub.pull_token(REPO)

    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    refused = await hub.get(f"/v2/{OTHER_REPO}/manifests/latest", headers=headers)
    assert refused.status_code == 401
    challenge = refused.headers["www-authenticate"]
    assert f'scope="repository:{OTHER_REPO}:pull"' in challenge and 'error="insufficient_scope"' in challenge
    # A token minted with no scope proves the login and reads nothing.
    credential = await hub.exchange()
    bare = {"Authorization": f"Bearer {await hub.token(credential)}"}
    assert (await hub.get("/v2/", headers=bare)).status_code == 200
    assert (await hub.get(f"/v2/{REPO}/tags/list", headers=bare)).status_code == 401


def test_only_pull_on_a_repository_is_ever_granted():
    assert requested_repositories(
        [
            "repository:cogs/a:pull",
            "repository:cogs/b:pull,push repository:cogs/c:push",
            "repository:cogs/a:pull",
            "registry:catalog:*",
            "repository:Not/Valid:pull",
            "repository::pull",
            "garbage",
        ]
    ) == ["cogs/a", "cogs/b"]
    many = [f"repository:cogs/r{i}:pull" for i in range(40)]
    assert len(requested_repositories(many)) == registry_router.MAX_TOKEN_SCOPES
    # The input is bounded before it is parsed: a huge scope list is not walked to its end.
    flood = ["repository:cogs/a:push " * 100_000 + "repository:cogs/z:pull"]
    assert requested_repositories(flood) == []
    assert requested_repositories(["garbage"] * 10_000 + ["repository:cogs/z:pull"]) == []
    assert requested_repositories(["x " * 70 + "repository:cogs/z:pull"]) == []


# -- pulling ---------------------------------------------------------------------


async def test_a_nebi_bundle_round_trips_with_only_a_hub_sign_in(hub: Hub):
    """The whole client conversation: challenge, exchange, token, manifest, every blob."""

    hub.seed(REPO, ALPHA, "latest", "1.0.0")

    # 1. A client with no credentials is told where to get a token.
    refused = await hub.get(f"/v2/{REPO}/manifests/latest")
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == (
        f'Bearer realm="{HUB_URL}/v2/token",service="{HUB_HOST}",scope="repository:{REPO}:pull"'
    )
    # 2. It exchanges its Hub session for a registry credential, and that for a pull token.
    headers = await hub.pull_token(REPO)

    # 3. The manifest, by tag and by digest, byte for byte.
    for ref in ("latest", "1.0.0", ALPHA.digest):
        manifest = await hub.get(f"/v2/{REPO}/manifests/{ref}", headers=headers)
        assert manifest.status_code == 200, ref
        assert manifest.content == ALPHA.manifest
        assert manifest.headers["content-type"] == MEDIA_TYPE_OCI_MANIFEST
        assert manifest.headers["docker-content-digest"] == ALPHA.digest
        head = await hub.request("HEAD", f"/v2/{REPO}/manifests/{ref}", headers=headers)
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(ALPHA.manifest))
        assert head.headers["docker-content-digest"] == ALPHA.digest

    # 4. The custom media types arrive exactly as published.
    document = json.loads(manifest.content)
    assert document["config"]["mediaType"] == MEDIA_TYPE_PIXI_CONFIG == "application/vnd.pixi.config.v1+toml"
    assert {layer["mediaType"] for layer in document["layers"]} >= {MEDIA_TYPE_NEBI_ASSET, MEDIA_TYPE_PIXI_TOML}
    assert MEDIA_TYPE_NEBI_ASSET == "application/vnd.nebi.asset.v1"

    # 5. Every blob the manifest names, verified by the client the way a client does.
    for entry in (document["config"], *document["layers"]):
        head = await hub.request("HEAD", f"/v2/{REPO}/blobs/{entry['digest']}", headers=headers)
        assert head.status_code == 200 and head.headers["content-length"] == str(entry["size"])
        blob = await hub.get(f"/v2/{REPO}/blobs/{entry['digest']}", headers=headers)
        assert blob.status_code == 200
        assert sha256(blob.content) == entry["digest"] and len(blob.content) == entry["size"]
        assert blob.headers["docker-content-digest"] == entry["digest"]
        assert blob.headers["content-type"] == "application/octet-stream"

    # The registry credential went to the registry's token endpoint and never to object storage.
    assert not hub.upstream.storage_saw_authorization
    # HEAD on a blob is answered from the manifest: no blob request beyond the GETs.
    assert hub.upstream.served("/blobs/") == len(ALPHA.blobs)


async def test_every_read_costs_at_most_one_request_to_the_source(hub: Hub):
    """Nothing is scanned: a manifest read is one fetch, a blob read one fetch, a HEAD of a blob none."""

    hub.seed(REPO, ALPHA, "latest")
    for index in range(5):
        hub.seed(REPO, Bundle(f"other-{index}"), f"other-{index}", pushed_at=T0 - timedelta(days=index + 1))
    headers = await hub.pull_token(REPO)
    await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)  # the source's token dance happens here
    config = sha256(ALPHA.config)

    async def cost(method: str, url: str, expect: int) -> int:
        before = len(hub.upstream.requests)
        response = await hub.request(method, url, headers=headers)
        assert response.status_code == expect, (method, url, response.status_code)
        return len(hub.upstream.requests) - before

    assert await cost("GET", f"/v2/{REPO}/manifests/latest", 200) == 1
    assert await cost("HEAD", f"/v2/{REPO}/manifests/{ALPHA.digest}", 200) == 1
    assert await cost("GET", f"/v2/{REPO}/tags/list", 200) == 0
    assert await cost("HEAD", f"/v2/{REPO}/blobs/{config}", 200) == 0
    assert await cost("GET", f"/v2/{REPO}/blobs/{config}", 200) == 2, "the registry, then its redirect to storage"
    # What is not there costs the source nothing at all, however many versions the repository holds.
    assert await cost("GET", f"/v2/{REPO}/manifests/{'sha256:' + 'f' * 64}", 404) == 0
    assert await cost("GET", f"/v2/{REPO}/blobs/{'sha256:' + 'f' * 64}", 404) == 0
    assert await cost("HEAD", f"/v2/{REPO}/blobs/{'sha256:' + 'f' * 64}", 404) == 0
    assert await cost("GET", f"/v2/{REPO}/manifests/no-such-tag", 404) == 0


async def test_a_blob_is_pullable_once_a_manifest_that_references_it_has_been_served(hub: Hub):
    """Reachability is stored data: recorded when the manifest is served, and never guessed from the registry."""

    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    config = sha256(ALPHA.config)
    before = len(hub.upstream.requests)
    unknown = await hub.get(f"/v2/{REPO}/blobs/{config}", headers=headers)
    assert unknown.status_code == 404 and unknown.json()["errors"][0]["code"] == "BLOB_UNKNOWN"
    assert len(hub.upstream.requests) == before, "a blob no served manifest references is not asked for upstream"

    assert (await hub.request("HEAD", f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    recorded = hub.catalog.find_blob(REPO, config, ("backing",))
    assert [(blob.manifest_digest, blob.size) for blob in recorded] == [(ALPHA.digest, len(ALPHA.config))]
    assert (await hub.get(f"/v2/{REPO}/blobs/{config}", headers=headers)).content == ALPHA.config
    # Recorded for the repository it was served from, and for no other.
    hub.seed(OTHER_REPO, BETA, "latest")
    other = await hub.pull_token(OTHER_REPO)
    assert (await hub.get(f"/v2/{OTHER_REPO}/blobs/{config}", headers=other)).status_code == 404


async def test_tags_come_from_the_catalog_and_page(hub: Hub):
    hub.seed(REPO, ALPHA, "latest", "1.0.0")
    hub.seed(REPO, Bundle("older"), "0.9.0", pushed_at=T0 - timedelta(days=1))
    # In the registry, but never indexed: not a tag here.
    hub.upstream.publish(REPO, Bundle("stray"), "stray")
    headers = await hub.pull_token(REPO)

    listing = await hub.get(f"/v2/{REPO}/tags/list", headers=headers)
    assert listing.json() == {"name": REPO, "tags": ["0.9.0", "1.0.0", "latest"]}
    assert not any(path.endswith("/tags/list") for path in hub.upstream.paths()), "tags are never listed upstream"

    page = await hub.get(f"/v2/{REPO}/tags/list", params={"n": 2}, headers=headers)
    assert page.json()["tags"] == ["0.9.0", "1.0.0"]
    assert page.headers["link"] == f'</v2/{REPO}/tags/list?n=2&last=1.0.0>; rel="next"'
    rest = await hub.get(f"/v2/{REPO}/tags/list", params={"n": 2, "last": "1.0.0"}, headers=headers)
    assert rest.json()["tags"] == ["latest"] and "link" not in rest.headers
    assert (await hub.get(f"/v2/{REPO}/tags/list", params={"n": 0}, headers=headers)).json()["tags"] == []
    bad = await hub.get(f"/v2/{REPO}/tags/list", params={"n": "many"}, headers=headers)
    assert bad.status_code == 400 and bad.json()["errors"][0]["code"] == "PAGINATION_NUMBER_INVALID"


async def test_a_tag_resolves_to_the_newest_push_across_sources(make_hub, tmp_path):
    """Two sources carrying one repository path are one repository; the catalog's order decides a shared tag."""

    hub = await make_hub()
    newer, older = Bundle("newer"), Bundle("older")
    hub.seed(REPO, older, "latest", pushed_at=T0 - timedelta(days=2))
    hub.seed(REPO, newer, "latest", pushed_at=T0)
    headers = await hub.pull_token(REPO)
    manifest = await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)
    assert manifest.headers["docker-content-digest"] == newer.digest
    # A row whose source is no longer configured is not served.
    orphan = Bundle("orphan")
    hub.upstream.publish(REPO, orphan)
    hub.catalog.upsert(catalog_row(REPO, orphan.digest, tags=("orphan",), source_id="retired"))
    assert (await hub.get(f"/v2/{REPO}/manifests/orphan", headers=headers)).status_code == 404


async def test_an_index_is_served_as_stored_and_its_children_only_if_indexed_themselves(hub: Hub):
    """Indexes are not traversed: a child manifest needs its own pullable row, like any other manifest."""

    amd, arm = Bundle("amd64"), Bundle("arm64")
    hub.upstream.publish(REPO, amd)
    hub.upstream.publish(REPO, arm)
    index_body = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_INDEX,
            "manifests": [
                {"mediaType": MEDIA_TYPE_OCI_MANIFEST, "digest": amd.digest, "size": len(amd.manifest)},
                {"mediaType": MEDIA_TYPE_OCI_MANIFEST, "digest": arm.digest, "size": len(arm.manifest)},
            ],
        }
    ).encode()
    index_digest = hub.upstream.publish_raw(REPO, MEDIA_TYPE_OCI_INDEX, index_body, "multi")
    hub.catalog.upsert(catalog_row(REPO, index_digest, tags=("multi",)))
    # One child is indexed in its own right; the other exists only inside the index.
    hub.catalog.upsert(catalog_row(REPO, amd.digest, tags=("amd64",)))
    headers = await hub.pull_token(REPO)

    index = await hub.get(f"/v2/{REPO}/manifests/multi", headers=headers)
    assert index.content == index_body and index.headers["content-type"] == MEDIA_TYPE_OCI_INDEX
    assert index.headers["docker-content-digest"] == index_digest
    # The index contributes no blobs and no children.
    assert hub.catalog.find_blob(REPO, sha256(arm.config), ("backing",)) == []
    before = len(hub.upstream.requests)
    for url in (f"/v2/{REPO}/manifests/{arm.digest}", f"/v2/{REPO}/blobs/{sha256(arm.config)}"):
        assert (await hub.get(url, headers=headers)).status_code == 404, url
    assert len(hub.upstream.requests) == before, "an unindexed child is not fetched to find out"

    child = await hub.get(f"/v2/{REPO}/manifests/{amd.digest}", headers=headers)
    assert child.status_code == 200 and child.content == amd.manifest
    assert (await hub.get(f"/v2/{REPO}/blobs/{sha256(amd.config)}", headers=headers)).content == amd.config
    # Removing the child's row removes the child and its blobs, whatever the index still lists.
    hub.catalog.mark_removed_one("backing", REPO, amd.digest)
    assert (await hub.get(f"/v2/{REPO}/manifests/{amd.digest}", headers=headers)).status_code == 404
    assert (await hub.get(f"/v2/{REPO}/blobs/{sha256(amd.config)}", headers=headers)).status_code == 404
    assert (await hub.get(f"/v2/{REPO}/manifests/multi", headers=headers)).status_code == 200


# -- only what the catalog holds, never a pass-through ---------------------------


async def test_nothing_outside_the_catalog_is_served(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    stray = Bundle("stray")
    hub.upstream.publish(REPO, stray, "stray")
    hub.upstream.publish(UNINDEXED_REPO, BETA, "latest")
    removed, non_cog, failed = Bundle("removed"), Bundle("non-cog"), Bundle("failed")
    hub.seed(REPO, removed, "removed")
    hub.catalog.mark_removed_one("backing", REPO, removed.digest)
    hub.seed(REPO, non_cog, "noncog", status=STATUS_NON_COG)
    hub.seed(REPO, failed, "failed", status=STATUS_FAILED)
    headers = await hub.pull_token(REPO, UNINDEXED_REPO)
    # The source's own token dance, out of the way of the count below.
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    asked = len(hub.upstream.requests)

    async def refused(url: str, code: str) -> None:
        for method in ("GET", "HEAD"):
            response = await hub.request(method, url, headers=headers)
            assert response.status_code == 404, (method, url)
            if method == "GET":
                assert response.json()["errors"][0]["code"] == code, url

    # A repository the registry holds and the catalog does not.
    await refused(f"/v2/{UNINDEXED_REPO}/manifests/latest", "NAME_UNKNOWN")
    await refused(f"/v2/{UNINDEXED_REPO}/manifests/{BETA.digest}", "NAME_UNKNOWN")
    await refused(f"/v2/{UNINDEXED_REPO}/blobs/{sha256(BETA.config)}", "NAME_UNKNOWN")
    await refused(f"/v2/{UNINDEXED_REPO}/tags/list", "NAME_UNKNOWN")
    # A tag and a digest the registry holds in an indexed repository, never indexed themselves.
    await refused(f"/v2/{REPO}/manifests/stray", "MANIFEST_UNKNOWN")
    # Removed, non-Cog and failed rows are not pullable either.
    for bundle, tag in ((removed, "removed"), (non_cog, "noncog"), (failed, "failed")):
        await refused(f"/v2/{REPO}/manifests/{tag}", "MANIFEST_UNKNOWN")
    await refused(f"/v2/{REPO}/manifests/not a reference", "MANIFEST_UNKNOWN")
    await refused(f"/v2/{REPO}/blobs/not-a-digest", "BLOB_UNKNOWN")
    # A name no token can be scoped to never gets as far as a lookup.
    assert (await hub.get("/v2/Not/A/Repository/manifests/latest", headers=headers)).status_code == 401
    await refused("/v2/nothing-like-a-registry-path", "NAME_UNKNOWN")
    assert len(hub.upstream.requests) == asked, "none of those reached the backing registry"

    # A digest the registry holds but no indexed manifest names.
    await refused(f"/v2/{REPO}/manifests/{stray.digest}", "MANIFEST_UNKNOWN")
    await refused(f"/v2/{REPO}/blobs/{sha256(stray.config)}", "BLOB_UNKNOWN")
    assert len(hub.upstream.requests) == asked, "none of those reached the backing registry either"


async def test_the_registry_surface_is_read_only(hub: Hub):
    for method, url in (
        ("POST", f"/v2/{REPO}/blobs/uploads/"),
        ("PUT", f"/v2/{REPO}/manifests/latest"),
        ("PATCH", f"/v2/{REPO}/blobs/uploads/abc"),
        ("DELETE", f"/v2/{REPO}/manifests/latest"),
        ("POST", "/v2"),
        ("PUT", "/v2/"),
        ("DELETE", "/v2"),
    ):
        response = await hub.request(method, url)
        assert response.status_code == 405
        assert response.json()["errors"][0]["code"] == "UNSUPPORTED"


def test_registry_paths_are_parsed_from_the_right():
    assert parse_registry_path("cogs/a/manifests/latest") == ("manifests", "cogs/a", "latest")
    assert parse_registry_path("cogs/manifests/a/manifests/v1") == ("manifests", "cogs/manifests/a", "v1")
    assert parse_registry_path("blobs/manifests/x/blobs/sha256:ab") == ("blobs", "blobs/manifests/x", "sha256:ab")
    assert parse_registry_path("cogs/a/tags/list") == ("tags", "cogs/a", "")
    assert parse_registry_path("cogs/a") is None
    assert parse_registry_path("manifests/latest") is None


# -- the credential's lifecycle --------------------------------------------------


async def test_revoking_a_credential_stops_its_tokens_at_once(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    credential = await hub.exchange()
    headers = {"Authorization": f"Bearer {await hub.token(credential, REPO)}"}
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200

    # Someone else cannot revoke it, and cannot learn that it exists.
    theirs = await hub.request("DELETE", f"/v1/cogs/registry-credentials/{credential['id']}", headers=BOB)
    assert theirs.status_code == 404
    assert theirs.json()["error"]["code"] == "cog_registry_credential_not_found"
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200

    revoked = await hub.request("DELETE", f"/v1/cogs/registry-credentials/{credential['id']}", headers=ALICE)
    assert revoked.status_code == 204 and revoked.content == b""
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 401
    assert (await hub.get("/v2/token", headers=basic(credential["username"], credential["secret"]))).status_code == 401
    again = await hub.request("DELETE", f"/v1/cogs/registry-credentials/{credential['id']}", headers=ALICE)
    assert again.status_code == 404


async def test_sign_out_revokes_everything_the_caller_holds(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    first, second, bobs = await hub.exchange(), await hub.exchange(), await hub.exchange(BOB)
    tokens = [{"Authorization": f"Bearer {await hub.token(c, REPO)}"} for c in (first, second, bobs)]
    direct = await hub.get("/v2/token", params={"scope": f"repository:{REPO}:pull"}, headers=ALICE)
    tokens.append({"Authorization": f"Bearer {direct.json()['token']}"})

    gone = await hub.request("DELETE", "/v1/cogs/registry-credentials", headers=ALICE)
    assert gone.status_code == 204
    statuses = [(await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code for headers in tokens]
    # Alice's two credentials and her directly minted token are gone; Bob's is untouched.
    assert statuses == [401, 401, 200, 401]
    # Idempotent: nothing left to revoke is still a 204.
    assert (await hub.request("DELETE", "/v1/cogs/registry-credentials", headers=ALICE)).status_code == 204
    assert (await hub.request("DELETE", "/v1/cogs/registry-credentials")).status_code == 401


async def test_credentials_and_tokens_expire(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    now = [datetime.now(UTC)]
    hub.serving.credentials.clock = lambda: now[0]
    credential = await hub.exchange()

    # A token minted late in the credential's life ends with the credential, not five minutes later.
    now[0] += timedelta(minutes=13)
    late = await hub.get(
        "/v2/token",
        params={"scope": f"repository:{REPO}:pull"},
        headers=basic(credential["username"], credential["secret"]),
    )
    assert late.json()["expires_in"] == 120
    headers = {"Authorization": f"Bearer {late.json()['token']}"}
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200

    now[0] += timedelta(minutes=2, seconds=1)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 401
    assert (await hub.get("/v2/token", headers=basic(credential["username"], credential["secret"]))).status_code == 401
    expired = await hub.request("DELETE", f"/v1/cogs/registry-credentials/{credential['id']}", headers=ALICE)
    assert expired.status_code == 404

    # A token's own lifetime, on a fresh credential.
    headers = await hub.pull_token(REPO)
    now[0] += timedelta(minutes=5, seconds=1)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 401


async def test_a_removed_member_cannot_mint_tokens(hub: Hub, monkeypatch):
    """Under membership resolution the credential's owner is re-checked at every mint."""

    credential = await hub.exchange()
    hub.app.state.org_store = object()
    monkeypatch.setattr(registry_router, "org_source_resolves_membership", lambda: True)
    seen: list = []

    def still_a_member(claims, store):
        seen.append((claims, store))

    monkeypatch.setattr(registry_router, "auth_context_from_membership", still_a_member)
    assert (await hub.get("/v2/token", headers=basic(credential["username"], credential["secret"]))).status_code == 200
    assert seen == [({"sub": "alice"}, hub.app.state.org_store)]

    def removed(claims, store):
        raise NoOrganizationError()

    monkeypatch.setattr(registry_router, "auth_context_from_membership", removed)
    refused = await hub.get("/v2/token", headers=basic(credential["username"], credential["secret"]))
    assert refused.status_code == 403 and refused.json()["errors"][0]["code"] == "DENIED"


async def test_a_hub_caller_without_an_organization_is_denied_a_token(hub: Hub, monkeypatch):
    def no_organization(request):
        raise NoOrganizationError()

    monkeypatch.setattr(registry_router, "get_auth_context", no_organization)
    refused = await hub.get("/v2/token", headers=ALICE)
    assert refused.status_code == 403 and refused.json()["errors"][0]["code"] == "DENIED"


HUB_API_PATHS = (
    ("GET", "/v1/cogs"),
    ("GET", "/v1/cogs/catalog.v1.json"),
    ("POST", "/v1/cogs/registry-credentials"),
    ("DELETE", "/v1/cogs/registry-credentials"),
    ("GET", "/v1/frames"),
    ("GET", "/v1/active-frames"),
    ("GET", "/v1/frame-groups"),
    ("GET", "/v1/tasks"),
    ("GET", "/v1/usage/me"),
    ("GET", "/v1/me"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("POST", "/mcp"),
)


async def test_registry_secrets_are_refused_by_every_hub_api(hub: Hub):
    """A registry credential and a pull token open ``/v2/`` and nothing else."""

    credential = await hub.exchange()
    pull = await hub.token(credential, REPO)
    presentations = {
        "credential as Basic": basic(credential["username"], credential["secret"]),
        "secret as Bearer": {"Authorization": f"Bearer {credential['secret']}"},
        "pull token as Bearer": {"Authorization": f"Bearer {pull}"},
    }
    for label, headers in presentations.items():
        for method, path in HUB_API_PATHS:
            response = await hub.request(method, path, headers=headers)
            assert response.status_code == 401, (label, method, path, response.status_code)
    # The same paths do answer a Hub session, so the 401s above are about the credential.
    assert (await hub.get("/v1/cogs", headers=ALICE)).status_code == 200
    # And a pull token is not a registry credential: it mints nothing.
    assert (await hub.get("/v2/token", headers=presentations["pull token as Bearer"])).status_code == 401
    # Nor is a Hub access token accepted on the read API itself.
    assert (await hub.get("/v2/", headers=ALICE)).status_code == 401


# -- nothing about the backing registry leaves the Hub ---------------------------


def assert_no_backing_details(responses: list[httpx.Response]) -> None:
    for response in responses:
        haystack = "\n".join(
            [response.text if "octet-stream" not in response.headers.get("content-type", "") else ""]
            + [f"{name}: {value}" for name, value in response.headers.items()]
        )
        for needle in (*SECRETS, *BACKING_NAMES, UPSTREAM_USER):
            assert needle not in haystack, (needle, response.request.method, str(response.request.url))


async def test_no_backing_host_or_credential_reaches_a_client_or_a_log(hub: Hub, caplog):
    caplog.set_level(logging.DEBUG)
    hub.seed(REPO, ALPHA, "latest")
    broken, gone, unreachable = Bundle("broken"), Bundle("gone"), Bundle("unreachable")
    for bundle, tag in ((broken, "broken"), (gone, "gone"), (unreachable, "unreachable")):
        hub.seed(REPO, bundle, tag, pushed_at=T0 - timedelta(days=1))
    headers = await hub.pull_token(REPO)

    # The happy path, redirect to "object storage" included.
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    for digest in ALPHA.blobs:
        assert (await hub.get(f"/v2/{REPO}/blobs/{digest}", headers=headers)).status_code == 200
    assert (await hub.get(f"/v2/{REPO}/tags/list", headers=headers)).status_code == 200

    # Upstream failures whose bodies and headers name the registry and its secrets.
    noisy = f"internal error at {BACKING_URL} for {UPSTREAM_USER}:{UPSTREAM_PASSWORD} token={UPSTREAM_TOKEN}"
    hub.upstream.fail[f"/v2/{REPO}/manifests/{broken.digest}"] = httpx.Response(
        500, text=noisy, headers={"X-Upstream": BACKING_HOST, "Location": f"{BACKING_URL}/elsewhere"}
    )
    assert (await hub.get(f"/v2/{REPO}/manifests/broken", headers=headers)).status_code == 503
    hub.upstream.fail[f"/v2/{REPO}/manifests/{gone.digest}"] = httpx.Response(404, text=noisy)
    assert (await hub.get(f"/v2/{REPO}/manifests/gone", headers=headers)).status_code == 404

    def refuse_connection(request):
        raise httpx.ConnectError(f"cannot reach {BACKING_URL}", request=request)

    hub.upstream.fail[f"/v2/{REPO}/manifests/{unreachable.digest}"] = httpx.Response(200)
    original = hub.upstream.__call__

    async def flaky(request: httpx.Request) -> httpx.Response:
        if unreachable.digest in request.url.path:
            refuse_connection(request)
        return await original(request)

    for source in hub.serving.front.sources:
        source.oci()._http._transport = httpx.MockTransport(flaky)
    assert (await hub.get(f"/v2/{REPO}/manifests/unreachable", headers=headers)).status_code == 503
    # A blob whose registry answers with an error after the manifest was served.
    config_digest = sha256(ALPHA.config)
    hub.upstream.fail[f"/v2/{REPO}/blobs/{config_digest}"] = httpx.Response(
        502, text=noisy, headers={"Location": f"https://{STORAGE_HOST}/x?sig={STORAGE_SIGNATURE}"}
    )
    assert (await hub.get(f"/v2/{REPO}/blobs/{config_digest}", headers=headers)).status_code == 503
    hub.upstream.fail[f"/v2/{REPO}/blobs/{config_digest}"] = httpx.Response(404, text=noisy)
    assert (await hub.get(f"/v2/{REPO}/blobs/{config_digest}", headers=headers)).status_code == 404
    # Refusals, and the catalog's own answers.
    await hub.get(f"/v2/{REPO}/manifests/latest")
    await hub.get("/v2/token")
    detail = await hub.get("/v1/cogs/example/cog-alpha", headers=ALICE)
    assert detail.status_code == 200
    await hub.get(f"/v1/cogs/example/cog-alpha/versions/{ALPHA.digest}/reference", headers=ALICE)
    await hub.get("/v1/cogs", headers=ALICE)

    assert_no_backing_details(hub.responses)
    assert len(hub.responses) > 15

    # Logs are the operator's, so the backing host may appear there (httpx
    # names the URL it requested); a credential may not, and neither may the
    # signature of a pre-signed storage URL.
    logged = []
    for record in caplog.records:
        logged.append(record.getMessage())
        logged.extend(repr(value) for key, value in vars(record).items() if key not in ("msg", "args"))
    text = "\n".join(logged)
    assert STORAGE_HOST in text, "the storage redirect was logged by httpx, which is what the filter is for"
    for secret in SECRETS:
        assert secret not in text, secret
    # The Hub's own log lines name the source id, never a host.
    for record in caplog.records:
        if record.name.startswith("frames_server.cogs"):
            line = record.getMessage() + repr({k: v for k, v in vars(record).items() if k not in ("msg", "args")})
            assert not any(name in line for name in BACKING_NAMES), line


async def test_catalog_references_name_the_hub(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    hub.catalog.upsert(catalog_row("mirror/cog-alpha", ALPHA.digest, tags=("latest",)))
    both = {f"{HUB_HOST}/{REPO}@{ALPHA.digest}", f"{HUB_HOST}/mirror/cog-alpha@{ALPHA.digest}"}

    listing = (await hub.get("/v1/cogs", headers=ALICE)).json()
    assert len(listing["items"]) == 1 and listing["items"][0]["reference"] in both
    detail = (await hub.get("/v1/cogs/example/cog-alpha", headers=ALICE)).json()
    assert detail["reference"] in both
    assert {version["reference"] for version in detail["versions"]} == both
    version = (await hub.get(f"/v1/cogs/example/cog-alpha/versions/{ALPHA.digest}", headers=ALICE)).json()
    assert version["reference"].startswith(f"{HUB_HOST}/")

    url = f"/v1/cogs/example/cog-alpha/versions/{ALPHA.digest}/reference"
    answer = (await hub.get(url, headers=ALICE)).json()
    assert answer["reference"].startswith(f"{HUB_HOST}/") and answer["source_id"] == "backing"
    assert "backing_reference" not in answer
    assert len(answer["locations"]) == 1 and "backing_reference" not in answer["locations"][0]
    assert BACKING_HOST not in json.dumps(answer)

    # A platform operator is told where the artifact is stored.
    operator = AuthContext(user="op", home_org_id="org-a", workspace_id="ws", platform_role="operator")
    hub.app.dependency_overrides[cogs_router.get_catalog_caller] = lambda: operator
    answer = (await hub.client.get(url)).json()
    assert answer["reference"].startswith(f"{HUB_HOST}/")
    assert answer["backing_reference"].startswith(f"{BACKING_HOST}/")
    assert answer["locations"][0]["backing_reference"].startswith(f"{BACKING_HOST}/")
    assert answer["locations"][0]["reference"].startswith(f"{HUB_HOST}/")
    # An anonymous caller under a public catalog rule gets the Hub reference and no source id.
    hub.app.dependency_overrides[cogs_router.get_catalog_caller] = lambda: None
    answer = (await hub.client.get(url)).json()
    assert "source_id" not in answer and "backing_reference" not in answer
    assert all("source_id" not in loc and "backing_reference" not in loc for loc in answer["locations"])
    assert BACKING_HOST not in json.dumps(answer)


# -- off by default ---------------------------------------------------------------


async def test_serving_is_off_by_default_and_changes_nothing(make_hub):
    hub = await make_hub(serve={})
    assert hub.serving is None
    hub.upstream.publish(REPO, ALPHA, "latest")
    hub.catalog.upsert(catalog_row(REPO, ALPHA.digest))
    hub.catalog.upsert(catalog_row("mirror/cog-alpha", ALPHA.digest))

    # Nothing registry-shaped is mounted: no challenge, no OCI error, no version header.
    for method, path in (("GET", "/v2/"), ("GET", "/v2/token"), ("GET", f"/v2/{REPO}/manifests/latest")):
        response = await hub.request(method, path, headers=ALICE)
        assert response.status_code in (404, 405), (path, response.status_code)
        assert "docker-distribution-api-version" not in response.headers
        assert "www-authenticate" not in response.headers
    assert not any(getattr(route, "path", "").startswith("/v2") for route in hub.app.routes)

    # References keep naming the backing registry, and the answer has no new keys.
    listing = (await hub.get("/v1/cogs", headers=ALICE)).json()
    assert listing["items"][0]["reference"].startswith(f"{BACKING_HOST}/")
    answer = (await hub.get(f"/v1/cogs/example/cog-alpha/versions/{ALPHA.digest}/reference", headers=ALICE)).json()
    assert sorted(answer) == ["digest", "locations", "present", "reference", "repository", "source_id"]
    assert sorted(answer["locations"][0]) == ["pushed_at", "reference", "removed_at", "repository", "source_id"]
    assert answer["reference"].startswith(f"{BACKING_HOST}/")

    # The exchange says so in a way a client can branch on.
    for method, path in (
        ("POST", "/v1/cogs/registry-credentials"),
        ("DELETE", "/v1/cogs/registry-credentials"),
        ("DELETE", "/v1/cogs/registry-credentials/crc-abc"),
    ):
        response = await hub.request(method, path, headers=ALICE)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "cog_registry_not_served"
    assert not hub.upstream.requests


# -- the auth wall does not pre-empt the challenge --------------------------------


async def test_the_hardened_protection_map_leaves_the_challenge_alone(make_hub):
    rules = [rule.model_dump() for rule in recommended_path_rules()]
    # An operator rule that would otherwise put the API credential check in front of /v2.
    rules.append({"path": "/v2", "match": "prefix", "access": "authenticated"})
    hub = await make_hub(security={"default_access": "authenticated", "paths": rules})
    hub.seed(REPO, ALPHA, "latest")

    refused = await hub.get(f"/v2/{REPO}/manifests/latest")
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"].startswith(f'Bearer realm="{HUB_URL}/v2/token"')
    assert refused.json()["errors"][0]["code"] == "UNAUTHORIZED"
    assert (await hub.get("/v2/token")).headers["www-authenticate"].startswith("Basic ")

    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    # The wall is still up everywhere else.
    walled = await hub.get("/v1/frames")
    assert walled.status_code == 401 and walled.json()["error"]["code"] == "unauthorized"


def test_registry_path_is_segment_aware():
    assert registry_router.registry_path("/v2")
    assert registry_router.registry_path("/v2/")
    assert registry_router.registry_path("/v2/cogs/a/manifests/latest")
    assert not registry_router.registry_path("/v2beta")
    assert not registry_router.registry_path("/v1/cogs")


# -- bounded streaming -------------------------------------------------------------


async def test_a_blob_that_fails_verification_is_never_delivered_whole(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    big = sha256(ALPHA.files["model.bin"][1])
    original = ALPHA.files["model.bin"][1]

    # Same length, different bytes: only the hash can tell.
    hub.upstream.tamper[big] = original[:-1] + bytes([original[-1] ^ 0xFF])
    received = bytearray()
    with pytest.raises(BlobStreamAborted) as caught:
        async with hub.client.stream("GET", f"/v2/{REPO}/blobs/{big}", headers=headers) as response:
            async for chunk in response.aiter_bytes():
                received.extend(chunk)
    assert len(received) < len(original), "the last chunk is held back until the digest is checked"
    assert caught.value.__cause__ is None and BACKING_HOST not in str(caught.value)

    # A different length is refused before the response starts.
    hub.upstream.tamper[big] = original + b"extra"
    short = await hub.get(f"/v2/{REPO}/blobs/{big}", headers=headers)
    assert short.status_code == 503 and short.json()["errors"][0]["code"] == "UNAVAILABLE"

    del hub.upstream.tamper[big]
    assert (await hub.get(f"/v2/{REPO}/blobs/{big}", headers=headers)).content == original


async def test_a_blob_over_the_size_limit_is_refused_without_asking_the_registry(make_hub):
    hub = await make_hub(serve={"enabled": True, "public_url": HUB_URL, "max_blob_bytes": 100_000})
    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    big = sha256(ALPHA.files["model.bin"][1])
    for method in ("GET", "HEAD"):
        refused = await hub.request(method, f"/v2/{REPO}/blobs/{big}", headers=headers)
        assert refused.status_code == 403
    assert refused.request.method == "HEAD"
    refused = await hub.get(f"/v2/{REPO}/blobs/{big}", headers=headers)
    error = refused.json()["errors"][0]
    assert error["code"] == "DENIED" and "100000-byte limit" in error["message"]
    assert not any("/blobs/" in path for path in hub.upstream.paths())
    small = await hub.get(f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}", headers=headers)
    assert small.status_code == 200


async def test_a_blob_response_is_bounded_in_time(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    url = f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"
    assert (await hub.get(url, headers=headers)).status_code == 200
    hub.app.state.cog_registry_serving = replace(hub.serving, max_blob_seconds=0.05)

    # The source is slow to answer at all: no response has started, so it is a 503.
    hub.upstream.blob_delay = 0.5
    slow_open = await hub.get(url, headers=headers)
    assert slow_open.status_code == 503 and slow_open.json()["errors"][0]["code"] == "UNAVAILABLE"

    # The body trickles: the headers are out, so the response is cut short instead.
    hub.upstream.blob_delay = 0.0
    hub.upstream.stream_delay = 0.03
    with pytest.raises(BlobStreamAborted, match="time limit"):
        await hub.get(url, headers=headers)


# -- storage and upstream outages ---------------------------------------------------


async def test_a_storage_outage_is_a_503_in_the_registry_format(hub: Hub, monkeypatch):
    import psycopg

    headers = await hub.pull_token(REPO)

    def down(*_args, **_kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(hub.serving.credentials, "find_token", down)
    for url in ("/v2/", f"/v2/{REPO}/manifests/latest"):
        response = await hub.get(url, headers=headers)
        assert response.status_code == 503
        assert response.json() == {
            "errors": [{"code": "UNAVAILABLE", "message": "the registry is temporarily unavailable", "detail": {}}]
        }
    monkeypatch.setattr(hub.serving.credentials, "create_token", down)
    assert (await hub.get("/v2/token", headers=ALICE)).status_code == 503


async def test_a_source_that_cannot_answer_is_unavailable_and_one_that_lost_the_content_is_unknown(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    manifest_path, config = f"/v2/{REPO}/manifests/{ALPHA.digest}", sha256(ALPHA.config)

    hub.upstream.fail[manifest_path] = httpx.Response(500, text="boom")
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 503
    hub.upstream.fail[manifest_path] = httpx.Response(404, text="gone")
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 404

    hub.upstream.fail[f"/v2/{REPO}/blobs/{config}"] = httpx.Response(502, text="boom")
    assert (await hub.get(f"/v2/{REPO}/blobs/{config}", headers=headers)).status_code == 503
    hub.upstream.fail[f"/v2/{REPO}/blobs/{config}"] = httpx.Response(404, text="gone")
    assert (await hub.get(f"/v2/{REPO}/blobs/{config}", headers=headers)).status_code == 404


async def test_metadata_reads_run_under_their_own_deadline(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    hub.app.state.cog_registry_serving = replace(hub.serving, max_metadata_seconds=0.05)

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(5)

    (source,) = hub.serving.front.sources
    source.oci().fetch_manifest = stalled
    for method in ("GET", "HEAD"):
        slow = await hub.request(method, f"/v2/{REPO}/manifests/latest", headers=headers)
        assert slow.status_code == 503, method
    slow = await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)
    assert slow.json()["errors"][0]["code"] == "UNAVAILABLE"


# -- startup ------------------------------------------------------------------------


def test_the_registry_is_the_hub_api_origin_not_a_host_of_its_own(tmp_path):
    """Where the deployment states its external origin twice, the two must agree."""

    values = settings(tmp_path)
    values["web"] = {"public_base_url": "https://elsewhere.example"}
    with pytest.raises(RuntimeError, match="name different hosts"):
        make_app(Config.parse(values))
    # The same origin, spelled with its default port and a trailing slash, agrees.
    values["web"] = {"public_base_url": f"{HUB_URL}:443/"}
    app = make_app(Config.parse(values))
    assert app is not None


async def test_issued_credential_ids_are_one_url_safe_path_segment(hub: Hub):
    import re

    from collab_hub_api.cogs.registry_credentials import CREDENTIAL_ID_PATTERN

    assert CREDENTIAL_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    for _ in range(5):
        credential = await hub.exchange()
        assert re.fullmatch(CREDENTIAL_ID_PATTERN[1:-1], credential["id"]), credential["id"]
        assert credential["registry"] == HUB_HOST
    # An id outside the shape is refused as malformed, never looked up.
    for bad in ("has%20space", "-leading-dash", "a" * 129):
        refused = await hub.request("DELETE", f"/v1/cogs/registry-credentials/{bad}", headers=ALICE)
        assert refused.status_code == 422, bad


def test_serving_requires_a_catalog_store(tmp_path):
    values = settings(tmp_path)
    values["cogs"]["catalog"] = {}
    with pytest.raises(RuntimeError, match="cogs.serve.enabled requires the Cog catalog store"):
        make_app(Config.parse(values))


async def test_serving_closes_its_own_sources_at_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(Config.parse(settings(tmp_path)))
    async with app.router.lifespan_context(app):
        (source,) = app.state.cog_registry_serving.front.sources
        # Built for serving on its own: the indexer an API replica holds for
        # its targeted entry points (issue #148) has sources of its own, and
        # this process does not sweep.
        assert app.state.cog_indexer is not None
        assert all(other is not source for other in app.state.cog_registry_sources)
        assert source.host == BACKING_HOST
    assert source.oci()._http.is_closed


# -- the response owns the upstream; the log filters are opt-in ----------------------


async def test_a_response_that_fails_before_its_first_chunk_still_closes_the_upstream():
    """A generator that never started runs no cleanup, so the response closes the blob itself."""

    from starlette.requests import ClientDisconnect

    from collab_hub_api.cogs.oci import OCIClient
    from collab_hub_api.cogs.serving import ServedBlob
    from collab_hub_api.routers.registry import _BlobResponse

    body = b"blob bytes"
    client = OCIClient(BACKING_URL, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))

    async def respond(send) -> ServedBlob:
        blob = ServedBlob(await client.open_blob(REPO, sha256(body)), len(body), max_bytes=1 << 20)
        response = _BlobResponse(
            blob, deadline=asyncio.get_running_loop().time() + 5, what="x", headers={"Content-Length": str(len(body))}
        )
        assert not blob.closed

        async def receive():
            await asyncio.sleep(3600)

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}
        # Starlette reports a failed send as the client having disconnected.
        with pytest.raises((ConnectionError, ClientDisconnect)):
            await response(scope, receive, send)
        return blob

    async def refuse_everything(message):
        raise ConnectionError("client went away before the response started")

    assert (await respond(refuse_everything)).closed

    async def refuse_the_body(message):
        if message["type"] == "http.response.body":
            raise ConnectionError("client went away mid-body")

    blob = await respond(refuse_the_body)
    assert blob.closed
    await blob.aclose()  # idempotent
    await client.aclose()


def test_log_redaction_is_installed_only_by_a_hub_that_serves(tmp_path, monkeypatch):
    """With serving off, logging is exactly what it was -- whether or not this process indexes."""

    installed: list[int] = []
    monkeypatch.setattr(config_module, "install_log_redaction", lambda: installed.append(1))
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")

    make_app(Config.parse(settings(tmp_path, serve={})))
    assert installed == [], "sources alone change no logging"

    indexing = settings(tmp_path, serve={})
    indexing["cogs"]["index"] = {"enabled": True}
    make_app(Config.parse(indexing))
    assert installed == [], "indexing on with serving off installs nothing"

    make_app(Config.parse(settings(tmp_path)))
    assert installed == [1]

    both = settings(tmp_path)
    both["cogs"]["index"] = {"enabled": True}
    make_app(Config.parse(both))
    assert installed == [1, 1], "once per serving app, not once more for the indexer"


def test_an_index_only_hub_really_has_no_filters():
    import subprocess
    import sys

    probe = (
        "import logging;"
        "from collab_hub_api.config import Config;"
        "from collab_hub_api.core import make_app;"
        "import tempfile;"
        "d = tempfile.mkdtemp();"
        "source = {'id': 's', 'kind': 'static', 'url': 'https://registry.example', 'repositories': ['cogs/a']};"
        "cogs = {'catalog': {'backend': 'memory'}, 'registry_sources': [source], 'index': {'enabled': True}};"
        "make_app(Config.parse({'storage': {'frames_path': d}, 'tasks': {'backend': 'memory'}, 'cogs': cogs,"
        " 'frames': {'mcp_session_manager_enabled': False}}));"
        "names = ['httpx', 'httpcore.http11', 'httpcore.http2', 'httpcore.connection'];"
        "print([len(logging.getLogger(n).filters) for n in names])"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip().splitlines()[-1] == "[0, 0, 0, 0]"


def test_importing_the_oci_client_installs_no_log_filter():
    import subprocess
    import sys

    probe = (
        "import logging, collab_hub_api.cogs.oci, collab_hub_api.config;"
        "names = ['httpx', 'httpcore.http11', 'httpcore.http2', 'httpcore.connection'];"
        "print([len(logging.getLogger(n).filters) for n in names])"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[0, 0, 0, 0]"


# -- the redirect policy is serving's, not the indexer's ---------------------------


async def test_the_same_redirects_index_with_serving_off_and_are_refused_with_serving_on(tmp_path, monkeypatch):
    """An https registry that redirects layers to http storage: main indexes it, and so does a Hub that does not serve.

    Enabling serving holds the indexer to the redirect policy too -- a source
    the Hub could not serve a pull from must not look healthy in the catalog.
    """

    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "cogs" / "pixi-complete"
    files = {
        "pixi.toml": (MEDIA_TYPE_PIXI_TOML, (fixture / "pixi.toml").read_bytes()),
        "COG.md": (MEDIA_TYPE_NEBI_ASSET, (fixture / "COG.md").read_bytes()),
    }
    config_blob = b"{}"
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": descriptor(MEDIA_TYPE_PIXI_CONFIG, config_blob),
            "layers": [descriptor(media_type, data, title) for title, (media_type, data) in files.items()],
        }
    ).encode()

    async def sweep(serve: dict) -> tuple[list, FakeRegistry]:
        upstream = FakeRegistry()
        upstream.storage_scheme = "http"  # the registry is https; its storage is plain http
        upstream.publish_raw(REPO, MEDIA_TYPE_OCI_MANIFEST, manifest, "1.0.0")
        upstream.blobs.update({sha256(data): data for _media_type, data in files.values()})
        upstream.blobs[sha256(config_blob)] = config_blob
        transport = httpx.MockTransport(upstream)
        monkeypatch.setattr(
            config_module,
            "build_registry_sources",
            lambda configs, **kwargs: build_registry_sources(configs, http_transport=transport, **kwargs),
        )
        values = settings(tmp_path, serve=serve)
        values["cogs"]["index"] = {"enabled": True}
        config = Config.parse(values)
        store = config_module.build_cog_catalog_store(config, None)
        indexing = config_module.build_cog_indexing(config, store)
        try:
            await indexing.indexer.sweep()
        finally:
            indexing.indexer.close()
            for source in indexing.indexer.sources:
                await source.aclose()
        return store.locations(sha256(manifest)), upstream

    (row,), upstream = await sweep({})
    assert row.status == STATUS_INDEXED and row.cog_id == "example/cog-audio-transcriber", row.read_errors
    assert [r.cog_id for r in (row,)] and any(request.url.scheme == "http" for request in upstream.requests)

    (row,), upstream = await sweep({"enabled": True, "public_url": HUB_URL})
    assert row.status == STATUS_FAILED and row.cog_id is None
    assert any("refusing a redirect from https to http" in error for error in row.read_errors), row.read_errors
    assert not any(request.url.host == STORAGE_HOST for request in upstream.requests), "storage was never contacted"
