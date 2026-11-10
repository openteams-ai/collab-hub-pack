"""Publishing through the Hub (issue #180): the push half of ``/v2/`` against a fake backing registry.

Same construction as ``test_cog_serving``: the fake registry is an
``httpx.MockTransport`` behind a real ``static`` source, so the Hub's router,
its publisher, the generic OCI client and the indexer's reader are all the
code that runs in production. The fake hands out absolute, stateful upload
URLs on its own host, as real registries do, so the tests can show none of
that reaches a client.
"""

# ruff: noqa: F811 - the fixtures imported from test_cog_serving are used as parameters

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from test_cog_serving import (
    ALICE,
    BACKING_NAMES,
    HUB_HOST,
    MEDIA_TYPE_NEBI_ASSET,
    MEDIA_TYPE_OCI_INDEX,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_CONFIG,
    MEDIA_TYPE_PIXI_TOML,
    SECRETS,
    UPSTREAM_USER,
    FakeRegistry,
    Hub,
    assert_no_backing_details,
    basic,
    bearer,
    catalog_row,
    descriptor,
    make_hub,  # noqa: F401 - fixture
    settings,
    sha256,
)

from collab_hub_api import config as config_module
from collab_hub_api.cogs.catalog import STATUS_INDEXED
from collab_hub_api.cogs.publishing import PublishPolicy
from collab_hub_api.cogs.registry import build_registry_sources
from collab_hub_api.cogs.registry_credentials import PULL_TOKEN_PREFIX
from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV
from collab_hub_api.frames.orgs import MEMBERSHIP_REMOVED, ROLE_MEMBER, ROLE_OWNER

FIXTURE = Path(__file__).parent / "fixtures" / "cogs" / "pixi-complete"
REPO = "cogs/cog-audio-transcriber"
COG_ID = "example/cog-audio-transcriber"
BOB = bearer({"preferred_username": "bob", "org_id": "org-b", "workspace_id": "ws"})
CAROL = bearer({"preferred_username": "carol", "org_id": "org-a", "workspace_id": "ws"})
EVERYONE = {"allowed_users": ["alice", "bob", "carol"]}


class CogBundle:
    """A real Cog bundle (the sanitized fixture), in the shape ``nebi publish`` pushes."""

    def __init__(self, *, extra: bytes = b"weights", cog_md: bytes | None = None, pixi_toml: bytes | None = None):
        self.config = b"{}"
        self.files = {
            "pixi.toml": (
                MEDIA_TYPE_PIXI_TOML,
                pixi_toml if pixi_toml is not None else (FIXTURE / "pixi.toml").read_bytes(),
            ),
            "COG.md": (MEDIA_TYPE_NEBI_ASSET, cog_md if cog_md is not None else (FIXTURE / "COG.md").read_bytes()),
            "weights.bin": (MEDIA_TYPE_NEBI_ASSET, extra),
        }
        document = {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": descriptor(MEDIA_TYPE_PIXI_CONFIG, self.config),
            "layers": [descriptor(media_type, data, title) for title, (media_type, data) in self.files.items()],
        }
        self.manifest = json.dumps(document).encode()
        self.digest = sha256(self.manifest)

    @property
    def blobs(self) -> dict[str, bytes]:
        blobs = {sha256(self.config): self.config}
        blobs.update({sha256(data): data for _media_type, data in self.files.values()})
        return blobs


COG = CogBundle(extra=bytes(range(256)) * 600)  # 150 KiB: more than one stream chunk


class Pusher:
    """The requests a registry client makes to push, against one Hub, as one caller."""

    def __init__(self, hub: Hub, headers: dict[str, str]) -> None:
        self.hub = hub
        self.headers = headers

    async def start(self, repo: str = REPO, **params) -> httpx.Response:
        return await self.hub.request("POST", f"/v2/{repo}/blobs/uploads/", headers=self.headers, params=params or None)

    async def blob(self, data: bytes, repo: str = REPO, *, chunks: int = 1) -> httpx.Response:
        """Upload one blob: POST, ``chunks`` PATCHes (0 means the body rides the closing PUT), PUT."""

        started = await self.start(repo)
        assert started.status_code == 202, started.text
        location = started.headers["location"]
        if chunks == 0:
            return await self.hub.request(
                "PUT", location, headers=self.headers, params={"digest": sha256(data)}, content=data
            )
        size = max(1, -(-len(data) // chunks))
        offset = 0
        for start in range(0, len(data), size):
            piece = data[start : start + size]
            headers = {**self.headers, "Content-Range": f"{offset}-{offset + len(piece) - 1}"}
            patched = await self.hub.request("PATCH", location, headers=headers, content=piece)
            assert patched.status_code == 202, patched.text
            offset += len(piece)
            assert patched.headers["range"] == f"0-{offset - 1}"
            location = patched.headers["location"]
        return await self.hub.request("PUT", location, headers=self.headers, params={"digest": sha256(data)})

    async def manifest(self, bundle, reference: str, repo: str = REPO) -> httpx.Response:
        return await self.hub.request(
            "PUT",
            f"/v2/{repo}/manifests/{reference}",
            headers={**self.headers, "Content-Type": MEDIA_TYPE_OCI_MANIFEST},
            content=bundle.manifest,
        )

    async def bundle(self, bundle, reference: str = "1.0.0", repo: str = REPO, *, chunks: int = 1) -> httpx.Response:
        for data in bundle.blobs.values():
            closed = await self.blob(data, repo, chunks=chunks)
            assert closed.status_code == 201, closed.text
        return await self.manifest(bundle, reference, repo)


async def publish_credential(hub: Hub, who: dict[str, str] = ALICE) -> dict:
    response = await hub.request("POST", "/v1/cogs/registry-credentials", headers=who, json={"scope": "publish"})
    assert response.status_code == 201, response.text
    return response.json()


async def push_token(hub: Hub, credential: dict, *repositories: str) -> dict[str, str]:
    response = await hub.get(
        "/v2/token",
        params={"service": HUB_HOST, "scope": [f"repository:{name}:pull,push" for name in repositories]},
        headers=basic(credential["username"], credential["secret"]),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def pusher(hub: Hub, who: dict[str, str] = ALICE, *repositories: str) -> Pusher:
    return Pusher(hub, await push_token(hub, await publish_credential(hub, who), *(repositories or (REPO,))))


@pytest_asyncio.fixture
async def hub(make_hub) -> Hub:
    return await make_hub(publish=EVERYONE)


# -- the whole push ------------------------------------------------------------------


@pytest.mark.parametrize("chunks", [3, 1, 0])
async def test_a_bundle_published_through_the_hub_is_listed_at_once_with_its_publisher(hub: Hub, chunks, caplog):
    """Chunked, single-chunk and monolithic uploads; then the manifest; then the catalog, with no sweep."""

    caplog.set_level(logging.DEBUG)
    client = await pusher(hub)
    assert (await hub.get("/v1/cogs", headers=ALICE)).json()["items"] == []

    created = await client.bundle(COG, "1.0.0", chunks=chunks)
    assert created.status_code == 201 and created.content == b""
    assert created.headers["docker-content-digest"] == COG.digest
    assert created.headers["location"] == f"/v2/{REPO}/manifests/{COG.digest}"

    # In the backing registry, byte for byte, under the tag and the digest.
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == COG.manifest
    assert hub.upstream.manifests[(REPO, COG.digest)][1] == COG.manifest
    for digest, data in COG.blobs.items():
        assert hub.upstream.blobs[digest] == data
    assert hub.upstream.uploads == {}, "every upload session was closed at the registry"

    # Listed immediately, with the authenticated publisher beside the card's own declaration.
    (item,) = (await hub.get("/v1/cogs", headers=ALICE)).json()["items"]
    assert item["cog_id"] == COG_ID and item["digest"] == COG.digest and item["tags"] == ["1.0.0"]
    assert (item["published_by"], item["published_org"]) == ("alice", "org-a")
    assert item["card"]["publisher"] == "Example Organization", "what the bundle says about itself is untouched"
    assert item["reference"] == f"{HUB_HOST}/{REPO}@{COG.digest}"
    # And pullable immediately: the blobs were recorded with the manifest, no manifest read needed first.
    for digest, data in COG.blobs.items():
        blob = await hub.get(f"/v2/{REPO}/blobs/{digest}", headers=client.headers)
        assert blob.status_code == 200 and blob.content == data
    assert (await hub.get(f"/v2/{REPO}/manifests/1.0.0", headers=client.headers)).content == COG.manifest
    # The repository is now its publisher's organization's.
    record = hub.app.state.cog_registry_serving.publisher._store.get_repository(REPO)
    assert (record.owner_org_id, record.created_by, record.source_id) == ("org-a", "alice", "backing")

    # Nothing about the backing registry reached the client: not its host, not its upload URLs or their state.
    assert_no_backing_details(hub.responses)
    for response in hub.responses:
        location = response.headers.get("location", "")
        assert location == "" or location.startswith("/v2/"), location
    text = "\n".join(record.getMessage() + repr(vars(record).get("extra", "")) for record in caplog.records)
    for secret in SECRETS:
        assert secret not in text, secret


async def test_a_second_tag_for_the_same_digest_keeps_the_first(hub: Hub):
    client = await pusher(hub)
    assert (await client.bundle(COG, "1.0.0")).status_code == 201
    assert (await client.manifest(COG, "latest")).status_code == 201
    assert (await client.manifest(COG, COG.digest)).status_code == 201
    (item,) = (await hub.get("/v1/cogs", headers=ALICE)).json()["items"]
    assert item["tags"] == ["1.0.0", "latest"] and item["published_by"] == "alice"


async def test_the_whole_blob_may_ride_the_opening_request(hub: Hub):
    client = await pusher(hub)
    data = b"all in one request"
    created = await hub.request(
        "POST", f"/v2/{REPO}/blobs/uploads/", headers=client.headers, params={"digest": sha256(data)}, content=data
    )
    assert created.status_code == 201 and created.headers["location"] == f"/v2/{REPO}/blobs/{sha256(data)}"
    assert hub.upstream.blobs[sha256(data)] == data


async def test_a_mount_request_is_answered_as_an_ordinary_upload(hub: Hub):
    client = await pusher(hub)
    mounted = await client.start(mount=sha256(COG.config), **{"from": "cogs/elsewhere"})
    assert mounted.status_code == 202 and mounted.headers["location"].startswith(f"/v2/{REPO}/blobs/uploads/up-")
    assert mounted.headers["range"] == "0-0" and mounted.headers["docker-upload-uuid"].startswith("up-")
    assert sha256(COG.config) not in hub.upstream.blobs, "nothing was mounted"


async def test_upload_status_cancel_and_the_headers_a_client_follows(hub: Hub):
    client = await pusher(hub)
    started = await client.start()
    location, upload_id = started.headers["location"], started.headers["docker-upload-uuid"]
    assert location == f"/v2/{REPO}/blobs/uploads/{upload_id}"
    patched = await hub.request("PATCH", location, headers=client.headers, content=b"0123456789")
    assert patched.status_code == 202 and patched.headers["range"] == "0-9"

    status = await hub.get(location, headers=client.headers)
    assert status.status_code == 204 and status.headers["range"] == "0-9"
    assert status.headers["docker-upload-uuid"] == upload_id and status.headers["location"] == location

    cancelled = await hub.request("DELETE", location, headers=client.headers)
    assert cancelled.status_code == 204
    assert hub.upstream.uploads == {}, "the registry's session was dropped too"
    for method in ("GET", "PATCH", "DELETE"):
        gone = await hub.request(method, location, headers=client.headers, content=b"x" if method == "PATCH" else None)
        assert gone.status_code == 404 and gone.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN", method
    gone = await hub.request("PUT", location, headers=client.headers, params={"digest": sha256(b"x")})
    assert gone.status_code == 404


async def test_a_pusher_may_ask_whether_a_blob_is_already_there(hub: Hub):
    client = await pusher(hub)
    data = b"already uploaded"
    assert (await client.blob(data)).status_code == 201
    head = await hub.request("HEAD", f"/v2/{REPO}/blobs/{sha256(data)}", headers=client.headers)
    assert head.status_code == 200 and head.headers["content-length"] == str(len(data))
    assert (
        await hub.request("HEAD", f"/v2/{REPO}/blobs/{sha256(b'never')}", headers=client.headers)
    ).status_code == 404
    # It answers a pusher's HEAD and nothing else: the blob is not pullable until a manifest lists it.
    assert (await hub.get(f"/v2/{REPO}/blobs/{sha256(data)}", headers=client.headers)).status_code == 404
    reader = await hub.pull_token(REPO)
    assert (await hub.request("HEAD", f"/v2/{REPO}/blobs/{sha256(data)}", headers=reader)).status_code == 404
    # The registry is asked about this repository and no other: what another repository holds is not disclosed.
    asked = [request.url.path for request in hub.upstream.requests if request.method == "HEAD"]
    assert asked and all(path.startswith(f"/v2/{REPO}/blobs/sha256:") for path in asked)
    elsewhere = await pusher(hub, ALICE, "cogs/elsewhere")
    head = await hub.request("HEAD", f"/v2/cogs/elsewhere/blobs/{sha256(data)}", headers=elsewhere.headers)
    assert head.status_code == 200, "the fake registry keeps one blob store; a real one answers per repository"
    assert hub.upstream.requests[-1].url.path == f"/v2/cogs/elsewhere/blobs/{sha256(data)}"


async def test_push_paths_that_are_not_repository_names_reach_nothing(hub: Hub):
    """Encoded separators, traversal, uppercase and over-long names: refused, and never forwarded."""

    client = await pusher(hub)
    writes = len(hub.upstream.writes())
    for name in (
        "Cogs/Upper",
        "cogs/..",
        "cogs/%2e%2e/secret",
        "cogs//double",
        "cogs/" + "a" * 256,
        "cogs/trailing-",
        "cogs%00/nul",
    ):
        for method, path, body in PUSHES:
            url = path.format(repo=name, upload="up-" + "0" * 32)
            response = await hub.request(method, url, headers=client.headers, content=body)
            assert response.status_code in (400, 401, 404, 405), (method, url, response.status_code)
    # A separator that arrives encoded is the same repository, and passes the same checks.
    encoded = await hub.request("POST", f"/v2/{REPO.replace('/', '%2F')}/blobs/uploads/", headers=client.headers)
    assert encoded.status_code == 202 and encoded.headers["location"].startswith(f"/v2/{REPO}/blobs/uploads/")
    other = await hub.request("POST", "/v2/cogs%2Fnot-on-the-token/blobs/uploads/", headers=client.headers)
    assert other.status_code == 401
    assert [write for write in hub.upstream.writes()[writes:]] == [f"POST /v2/{REPO}/blobs/uploads/"]


# -- who may push ---------------------------------------------------------------------


PUSHES = (
    ("POST", "/v2/{repo}/blobs/uploads/", None),
    ("POST", "/v2/{repo}/blobs/uploads/?digest=" + sha256(b"x"), b"x"),
    ("PATCH", "/v2/{repo}/blobs/uploads/{upload}", b"x"),
    ("PUT", "/v2/{repo}/blobs/uploads/{upload}?digest=" + sha256(b"x"), b"x"),
    ("GET", "/v2/{repo}/blobs/uploads/{upload}", None),
    ("DELETE", "/v2/{repo}/blobs/uploads/{upload}", None),
    ("PUT", "/v2/{repo}/manifests/1.0.0", COG.manifest),
    ("PUT", "/v2/{repo}/manifests/" + COG.digest, COG.manifest),
)


async def refused_everywhere(hub: Hub, headers: dict[str, str], status: int, code: str, repo: str = REPO) -> None:
    """Every push request is refused the same way, and none of them reaches the backing registry.

    The pusher's ``HEAD`` of a blob is the one push-side request that is a
    read: refused, it is the ordinary answer about a blob nobody may pull,
    and it too asks the backing registry nothing.
    """

    writes, asked = len(hub.upstream.writes()), len(hub.upstream.requests)
    for method, path, body in PUSHES:
        url = path.format(repo=repo, upload="up-" + "0" * 32)
        response = await hub.request(method, url, headers=headers, content=body)
        assert response.status_code == status, (method, url, response.status_code, response.text)
        assert response.json()["errors"][0]["code"] == code, (method, url)
    assert len(hub.upstream.writes()) == writes, "a refused push sent nothing upstream"
    unlisted = sha256(b"a blob no manifest in the catalog lists")
    hub.upstream.blobs[unlisted] = b"a blob no manifest in the catalog lists"
    head = await hub.request("HEAD", f"/v2/{repo}/blobs/{unlisted}", headers=headers)
    # 401 without a usable token; otherwise 404 -- or 403 for an account that may not read at all any more.
    assert head.status_code in ((401,) if status == 401 else (403, 404)), head.status_code
    assert len(hub.upstream.requests) == asked, "nor was the registry asked whether it holds a blob"


async def test_push_needs_a_token_and_the_challenge_asks_for_push(hub: Hub):
    await refused_everywhere(hub, {}, 401, "UNAUTHORIZED")
    challenge = (await hub.request("POST", f"/v2/{REPO}/blobs/uploads/")).headers["www-authenticate"]
    assert f'scope="repository:{REPO}:pull,push"' in challenge
    # A token for another repository is told so, with the scope to ask for.
    other = await pusher(hub, ALICE, "cogs/another")
    refused = await hub.request("POST", f"/v2/{REPO}/blobs/uploads/", headers=other.headers)
    assert refused.status_code == 401 and 'error="insufficient_scope"' in refused.headers["www-authenticate"]


async def test_a_pull_credential_never_pushes_whatever_it_asks_for(hub: Hub):
    credential = await hub.exchange(ALICE)
    asked = await push_token(hub, credential, REPO)  # asks for pull,push; a pull credential is given pull
    await refused_everywhere(hub, asked, 403, "DENIED")
    assert (await hub.get(f"/v2/{REPO}/tags/list", headers=asked)).status_code == 404, "it still reads"
    # Nor does a token minted straight from the Hub session, which has no credential at all.
    direct = await hub.get("/v2/token", params={"scope": f"repository:{REPO}:pull,push"}, headers=ALICE)
    assert direct.json()["token"].startswith(PULL_TOKEN_PREFIX)
    await refused_everywhere(hub, {"Authorization": f"Bearer {direct.json()['token']}"}, 403, "DENIED")
    # A Hub access token itself is not a registry credential on any push route.
    await refused_everywhere(hub, ALICE, 401, "UNAUTHORIZED")


async def test_nobody_may_publish_by_default_and_the_permission_is_checked_on_every_request(make_hub):
    hub = await make_hub(publish={})
    refused = await hub.request("POST", "/v1/cogs/registry-credentials", headers=ALICE, json={"scope": "publish"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "cog_publish_forbidden"
    assert (await hub.exchange(ALICE))["scope"] == "pull", "pulling is unaffected"

    # Granted, then withdrawn while a publish credential and its token are still live.
    publisher = hub.serving.publisher
    publisher.policy = PublishPolicy(allowed_users=frozenset({"alice"}))
    client = await pusher(hub)
    started = await client.start()
    assert started.status_code == 202
    publisher.policy = PublishPolicy()
    await refused_everywhere(hub, client.headers, 403, "DENIED")
    message = (await client.start()).json()["errors"][0]["message"]
    assert "permission to publish" in message


async def test_publishing_is_off_without_a_publish_source(make_hub):
    hub = await make_hub()
    assert hub.serving.publisher is None
    credential = await hub.exchange(ALICE)
    token = await push_token(hub, credential, REPO)
    for method, path, body in PUSHES:
        if method == "GET":
            continue
        response = await hub.request(method, path.format(repo=REPO, upload="up-1"), headers=token, content=body)
        assert response.status_code == 405 and response.json()["errors"][0]["code"] == "UNSUPPORTED", (method, path)
    off = await hub.request("POST", "/v1/cogs/registry-credentials", headers=ALICE, json={"scope": "publish"})
    assert off.status_code == 422 and off.json()["error"]["code"] == "validation_error"
    assert hub.upstream.requests == []
    # And the catalog's answers have no publication keys at all: what they were before publishing existed.
    hub.catalog.note_publication("pub-1", "backing", REPO, COG.digest, user_id="alice", org_id="org-a")
    hub.catalog.accept_publication("pub-1")
    hub.catalog.record_published(catalog_row(REPO, COG.digest), tag="latest")
    assert hub.catalog.get(COG.digest).published_by == "alice"
    cog = "example/cog-audio-transcriber"
    for url in ("/v1/cogs", f"/v1/cogs/{cog}", f"/v1/cogs/{cog}/versions/{COG.digest}"):
        assert "published_by" not in (await hub.get(url, headers=ALICE)).text, url


async def test_content_is_never_deleted_through_the_hub(hub: Hub):
    client = await pusher(hub)
    assert (await client.bundle(COG)).status_code == 201
    for url in (
        f"/v2/{REPO}/manifests/1.0.0",
        f"/v2/{REPO}/manifests/{COG.digest}",
        f"/v2/{REPO}/blobs/{sha256(COG.config)}",
    ):
        refused = await hub.request("DELETE", url, headers=client.headers)
        assert refused.status_code == 405 and refused.json()["errors"][0]["code"] == "UNSUPPORTED", url
    for method, url in (
        ("POST", f"/v2/{REPO}/manifests/1.0.0"),
        ("PUT", f"/v2/{REPO}/blobs/{sha256(b'x')}"),
        ("PUT", "/v2/"),
    ):
        assert (await hub.request(method, url, headers=client.headers, content=b"x")).status_code == 405, url
    assert (REPO, "1.0.0") in hub.upstream.manifests


# -- whose repository it is -----------------------------------------------------------


async def test_a_repository_belongs_to_the_organization_that_first_published_it(hub: Hub):
    alice = await pusher(hub, ALICE)
    assert (await alice.bundle(COG)).status_code == 201

    # Bob holds the permission too, in another organization: every push to it is refused.
    bob = await pusher(hub, BOB)
    await refused_everywhere(hub, bob.headers, 403, "DENIED")
    assert "another organization" in (await bob.start()).json()["errors"][0]["message"]
    # Carol, in Alice's organization, may publish a new version to it.
    carol = await pusher(hub, CAROL)
    newer = CogBundle(extra=b"version two")
    assert (await carol.bundle(newer, "2.0.0")).status_code == 201
    versions = (await hub.get(f"/v1/cogs/{COG_ID}", headers=ALICE)).json()["versions"]
    assert {(v["tags"][0], v["published_by"]) for v in versions} == {("1.0.0", "alice"), ("2.0.0", "carol")}
    # Bob's own, new repository is his organization's.
    bobs = await pusher(hub, BOB, "cogs/bobs-cog")
    assert (await bobs.bundle(COG, "1.0.0", "cogs/bobs-cog")).status_code == 201
    await refused_everywhere(hub, (await pusher(hub, ALICE, "cogs/bobs-cog")).headers, 403, "DENIED", "cogs/bobs-cog")


async def test_a_repository_that_was_not_published_through_the_hub_accepts_no_pushes(hub: Hub):
    """Indexed from the registry, with no owner on record: a non-operator may not publish over it."""

    hub.catalog.upsert(catalog_row("cogs/out-of-band", "sha256:" + "a" * 64))
    client = await pusher(hub, ALICE, "cogs/out-of-band")
    await refused_everywhere(hub, client.headers, 403, "DENIED", "cogs/out-of-band")
    assert "not published through this Hub" in (await client.start("cogs/out-of-band")).json()["errors"][0]["message"]
    # A removed out-of-band artifact still holds the name.
    hub.catalog.mark_removed_one("backing", "cogs/out-of-band", "sha256:" + "a" * 64)
    await refused_everywhere(hub, client.headers, 403, "DENIED", "cogs/out-of-band")


async def test_two_organizations_racing_for_a_new_name_leave_one_owner(hub: Hub):
    """Uploads claim nothing; the first accepted manifest does, and the other organization's manifest is refused."""

    alice, bob = await pusher(hub, ALICE), await pusher(hub, BOB)
    theirs = CogBundle(extra=b"bob's bytes")
    for client, bundle in ((alice, COG), (bob, theirs)):
        for data in bundle.blobs.values():
            assert (await client.blob(data)).status_code == 201
    first, second = await asyncio.gather(alice.manifest(COG, "1.0.0"), bob.manifest(theirs, "1.0.0"))
    assert sorted((first.status_code, second.status_code)) == [201, 403]
    winner = COG if first.status_code == 201 else theirs
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == winner.manifest
    (item,) = (await hub.get("/v1/cogs", headers=ALICE)).json()["items"]
    assert item["digest"] == winner.digest


# -- roles, on a membership-resolving Hub --------------------------------------------------

OWNER, MEMBER, OPERATOR, OUTSIDER = "sub-owner", "sub-member", "sub-operator", "sub-outsider"


def member_token(sub: str) -> dict[str, str]:
    return bearer({"sub": sub, "sid": f"session-{sub}"})


@pytest_asyncio.fixture
async def membership_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    upstream = FakeRegistry()
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(
        config_module,
        "build_registry_sources",
        lambda configs, **kwargs: build_registry_sources(configs, http_transport=transport, **kwargs),
    )
    values = settings(tmp_path, publish={"allowed_roles": ["owner", "operator"]})
    values["frames"]["orgs"] = {"backend": "memory"}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        orgs = app.state.org_store
        orgs.set_membership(OWNER, "org-1", role=ROLE_OWNER)
        orgs.set_membership(MEMBER, "org-1", role=ROLE_MEMBER)
        orgs.set_membership(OUTSIDER, "org-2", role=ROLE_OWNER)
        orgs.set_platform_role(OPERATOR)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield Hub(app, client, upstream)


async def test_the_permission_follows_the_role_and_a_role_change_takes_effect_on_the_next_request(membership_hub):
    hub = membership_hub
    # A member holds no publish permission here: owners and operators do.
    refused = await hub.request(
        "POST", "/v1/cogs/registry-credentials", headers=member_token(MEMBER), json={"scope": "publish"}
    )
    assert refused.status_code == 403
    owner = await pusher(hub, member_token(OWNER))
    assert (await owner.bundle(COG)).status_code == 201
    (item,) = (await hub.get("/v1/cogs", headers=member_token(MEMBER))).json()["items"]
    assert (item["published_by"], item["published_org"]) == (OWNER, "org-1")

    # Demoted to member with a live publish token in hand: the very next push is refused.
    orgs = hub.app.state.org_store
    orgs.set_membership(OWNER, "org-1", role=ROLE_MEMBER)
    await refused_everywhere(hub, owner.headers, 403, "DENIED")
    # Restored, then removed from the organization altogether.
    orgs.set_membership(OWNER, "org-1", role=ROLE_OWNER)
    assert (await owner.start()).status_code == 202
    orgs.set_membership(OWNER, "org-1", role=ROLE_OWNER, status=MEMBERSHIP_REMOVED)
    await refused_everywhere(hub, owner.headers, 403, "DENIED")
    assert "not part of an organization" in (await owner.start()).json()["errors"][0]["message"]


async def test_the_organization_is_read_now_and_an_operator_may_publish_anywhere(membership_hub):
    hub = membership_hub
    owner = await pusher(hub, member_token(OWNER))
    assert (await owner.bundle(COG)).status_code == 201

    # Another organization's owner is refused; so is the first owner once moved to that organization.
    outsider = await pusher(hub, member_token(OUTSIDER))
    await refused_everywhere(hub, outsider.headers, 403, "DENIED")
    hub.app.state.org_store.set_membership(OWNER, "org-2", role=ROLE_OWNER)
    await refused_everywhere(hub, owner.headers, 403, "DENIED")

    # A platform operator with no organization publishes to an owned repository and to an out-of-band one.
    operator = await pusher(hub, member_token(OPERATOR), REPO, "cogs/out-of-band")
    newer = CogBundle(extra=b"from the operator")
    assert (await operator.bundle(newer, "2.0.0")).status_code == 201
    hub.catalog.upsert(catalog_row("cogs/out-of-band", "sha256:" + "a" * 64))
    assert (await operator.bundle(COG, "1.0.0", "cogs/out-of-band")).status_code == 201
    record = hub.serving.publisher._store.get_repository("cogs/out-of-band")
    assert (record.owner_org_id, record.created_by) == (None, OPERATOR)
    row = hub.catalog.get(newer.digest)
    assert (row.published_by, row.published_org) == (OPERATOR, None)
    # A repository an operator claimed belongs to no organization: owners cannot push to it.
    hub.app.state.org_store.set_membership(OWNER, "org-1", role=ROLE_OWNER)
    await refused_everywhere(
        hub, (await pusher(hub, member_token(OWNER), "cogs/out-of-band")).headers, 403, "DENIED", "cogs/out-of-band"
    )


def test_the_publish_policy_grants_by_user_or_by_role_and_to_nobody_by_default():
    nobody = PublishPolicy()
    assert not nobody.permits("alice", "owner", "operator")
    by_user = PublishPolicy(allowed_users=frozenset({"alice"}))
    assert by_user.permits("alice", None, None) and not by_user.permits("bob", "owner", "operator")
    owners = PublishPolicy(allowed_roles=frozenset({"owner"}))
    assert owners.permits("x", "owner", None) and not owners.permits("x", "member", None)
    assert not owners.permits("x", None, "operator"), "the platform role is granted only by name"
    operators = PublishPolicy(allowed_roles=frozenset({"operator"}))
    assert operators.permits("x", None, "operator") and not operators.permits("x", "owner", None)
    # An organization role cannot be spelled "operator" to pass for the platform role.
    assert not operators.permits("x", "operator", None)


# -- validated before it is committed ------------------------------------------------------


async def test_a_bundle_the_catalog_would_not_index_is_refused_and_nothing_is_written_or_listed(hub: Hub):
    client = await pusher(hub)
    not_a_cog = CogBundle()
    not_a_cog.files = {"README.txt": ("text/plain", b"hello")}
    not_a_cog.manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": descriptor(MEDIA_TYPE_PIXI_CONFIG, b"{}"),
            "layers": [descriptor("text/plain", b"hello", "README.txt")],
        }
    ).encode()
    no_id = CogBundle(
        pixi_toml=(FIXTURE / "pixi.toml").read_bytes().replace(b'id = "example/cog-audio-transcriber"\n', b"")
    )
    broken = CogBundle(cog_md=b"---\nname: [unclosed\n---\n# broken\n")
    for bundle, expect in ((not_a_cog, "no COG.md"), (no_id, None), (broken, None)):
        refused = await client.bundle(bundle, "1.0.0")
        assert refused.status_code == 400, refused.text
        errors = refused.json()["errors"]
        assert errors and all(error["code"] == "MANIFEST_INVALID" for error in errors)
        if expect:
            assert any(expect in error["message"] for error in errors), errors
    assert not any(ref == "1.0.0" for (_repo, ref) in hub.upstream.manifests), "no manifest reached the registry"
    assert not any("manifests" in write for write in hub.upstream.writes())
    assert (await hub.get("/v1/cogs", headers=ALICE)).json()["items"] == []
    assert hub.catalog.locations(not_a_cog.digest) == []
    assert hub.serving.publisher._store.get_repository(REPO) is None, "a refused manifest claims no repository"

    # A layer that was never uploaded is the bundle's problem too, and says which.
    missing = CogBundle(extra=b"never uploaded")
    for digest, data in missing.blobs.items():
        if data not in (b"never uploaded",) and digest not in hub.upstream.blobs:
            assert (await client.blob(data)).status_code == 201
    hub.upstream.blobs.pop(sha256((FIXTURE / "COG.md").read_bytes()))
    refused = await client.manifest(missing, "1.0.0")
    assert refused.status_code == 400 and "fetch:" in refused.json()["errors"][0]["message"]


async def test_manifest_shapes_that_are_refused_outright(hub: Hub):
    client = await pusher(hub)
    index = RawManifest(json.dumps({"schemaVersion": 2, "mediaType": MEDIA_TYPE_OCI_INDEX, "manifests": []}).encode())
    refused = await client.manifest(index, "multi")
    assert refused.status_code == 400 and "single manifest" in refused.json()["errors"][0]["message"]
    garbage = await client.manifest(RawManifest(b"not json"), "1.0.0")
    assert garbage.status_code == 400 and garbage.json()["errors"][0]["code"] == "MANIFEST_INVALID"
    wrong_digest = await client.manifest(COG, "sha256:" + "0" * 64)
    assert wrong_digest.status_code == 400 and wrong_digest.json()["errors"][0]["code"] == "DIGEST_INVALID"
    huge = await client.manifest(RawManifest(b" " * (5 * 1024 * 1024 + 1)), "1.0.0")
    assert huge.status_code == 413 and huge.json()["errors"][0]["code"] == "MANIFEST_INVALID"
    assert not hub.upstream.writes(), "none of those was forwarded"


class RawManifest:
    """Whatever bytes a client chooses to PUT as a manifest."""

    def __init__(self, manifest: bytes) -> None:
        self.manifest = manifest


async def test_an_outage_while_validating_is_a_503_not_a_verdict_on_the_bundle(hub: Hub):
    client = await pusher(hub)
    for data in COG.blobs.values():
        assert (await client.blob(data)).status_code == 201
    cog_md = sha256((FIXTURE / "COG.md").read_bytes())
    hub.upstream.fail[f"/v2/{REPO}/blobs/{cog_md}"] = httpx.Response(401, text="who are you")
    unavailable = await client.manifest(COG, "1.0.0")
    assert unavailable.status_code == 503 and unavailable.json()["errors"][0]["code"] == "UNAVAILABLE"
    del hub.upstream.fail[f"/v2/{REPO}/blobs/{cog_md}"]
    # The registry refusing the manifest itself is reported without its words.
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(400, text=f"bad manifest at {BACKING_NAMES[0]}")
    refused = await client.manifest(COG, "1.0.0")
    assert refused.status_code == 400 and refused.json()["errors"][0]["message"] == "the registry refused the manifest"
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    assert (await client.manifest(COG, "1.0.0")).status_code == 503
    assert (await hub.get("/v1/cogs", headers=ALICE)).json()["items"] == []
    assert_no_backing_details(hub.responses)


# -- upload sessions ---------------------------------------------------------------------


async def test_an_upload_session_is_its_owners_and_its_repositorys(hub: Hub):
    alice = await pusher(hub, ALICE, REPO, "cogs/other")
    carol = await pusher(hub, CAROL)
    location = (await alice.start()).headers["location"]
    upload_id = location.rsplit("/", 1)[-1]
    # Another member of the same organization, and the same user under another repository name.
    writes = len(hub.upstream.writes())
    for client, url in ((carol, location), (alice, f"/v2/cogs/other/blobs/uploads/{upload_id}")):
        for method in ("GET", "PATCH", "DELETE"):
            response = await hub.request(
                method, url, headers=client.headers, content=b"x" if method == "PATCH" else None
            )
            assert response.status_code == 404, (method, url)
        # Nor may either of them close it as a blob of their own.
        closed = await hub.request("PUT", url, headers=client.headers, params={"digest": sha256(b"x")}, content=b"x")
        assert closed.status_code == 404 and closed.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN", url
    assert len(hub.upstream.writes()) == writes and sha256(b"x") not in hub.upstream.blobs
    for bad in ("not%20an%20id", "up-" + "f" * 32, "..", "a" * 200):
        assert (await hub.get(f"/v2/{REPO}/blobs/uploads/{bad}", headers=alice.headers)).status_code == 404
    assert (await hub.get(location, headers=alice.headers)).status_code == 204


async def test_chunks_must_arrive_in_order(hub: Hub):
    client = await pusher(hub)
    location = (await client.start()).headers["location"]
    first = await hub.request("PATCH", location, headers={**client.headers, "Content-Range": "0-4"}, content=b"01234")
    assert first.status_code == 202
    skipped = await hub.request("PATCH", location, headers={**client.headers, "Content-Range": "9-12"}, content=b"9abc")
    assert skipped.status_code == 416 and skipped.headers["range"] == "0-4"
    assert skipped.json()["errors"][0]["code"] == "BLOB_UPLOAD_INVALID"
    malformed = await hub.request(
        "PATCH", location, headers={**client.headers, "Content-Range": "five-nine"}, content=b"x"
    )
    assert malformed.status_code == 400
    # The session is where it was, and can be finished.
    assert (await hub.get(location, headers=client.headers)).headers["range"] == "0-4"
    closed = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(b"0123456789")}, content=b"56789"
    )
    assert closed.status_code == 201 and hub.upstream.blobs[sha256(b"0123456789")] == b"0123456789"


async def test_a_blob_that_does_not_match_its_digest_is_refused_by_the_registry_and_reported_plainly(hub: Hub):
    client = await pusher(hub)
    location = (await client.start()).headers["location"]
    wrong = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(b"claimed")}, content=b"actual"
    )
    assert wrong.status_code == 400
    assert wrong.json()["errors"] == [
        {"code": "DIGEST_INVALID", "message": "the uploaded content does not match the digest", "detail": {}}
    ]
    assert (await hub.get(location, headers=client.headers)).status_code == 404, "the session is gone"
    malformed = await hub.request(
        "PUT", (await client.start()).headers["location"], headers=client.headers, params={"digest": "md5:abc"}
    )
    assert malformed.status_code == 400 and malformed.json()["errors"][0]["code"] == "DIGEST_INVALID"
    missing = await hub.request("PUT", (await client.start()).headers["location"], headers=client.headers)
    assert missing.status_code == 400 and missing.json()["errors"][0]["code"] == "DIGEST_INVALID"
    assert_no_backing_details(hub.responses)


async def test_the_blob_size_limit_holds_for_declared_and_undeclared_bodies(make_hub):
    hub = await make_hub(
        publish=EVERYONE, serve={"enabled": True, "public_url": "https://hub.example", "max_blob_bytes": 1000}
    )
    client = await pusher(hub)

    async def trickle(size: int):
        for _ in range(size // 100):
            yield b"x" * 100

    # Declared too large: refused before anything is sent to the registry.
    location = (await client.start()).headers["location"]
    writes = len(hub.upstream.writes())
    declared = await hub.request("PATCH", location, headers=client.headers, content=b"x" * 1500)
    assert declared.status_code == 413 and declared.json()["errors"][0]["code"] == "SIZE_INVALID"
    assert len(hub.upstream.writes()) == writes
    # Undeclared (chunked): cut off as it streams, and the session is dropped on both sides.
    streamed = await hub.request("PATCH", location, headers=client.headers, content=trickle(1500))
    assert streamed.status_code == 413
    assert (await hub.get(location, headers=client.headers)).status_code == 404 and hub.upstream.uploads == {}
    # Across chunks: 600 + 600 is over the limit too.
    location = (await client.start()).headers["location"]
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"x" * 600)).status_code == 202
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"x" * 600)).status_code == 413
    closing = (await client.start()).headers["location"]
    over = await hub.request(
        "PUT", closing, headers=client.headers, params={"digest": sha256(b"x" * 1500)}, content=trickle(1500)
    )
    assert over.status_code == 413
    # A manifest naming a layer over the limit is refused as well.
    big = CogBundle(extra=b"y" * 2000)
    refused = await client.manifest(big, "1.0.0")
    assert refused.status_code == 400 and "limit" in refused.json()["errors"][0]["message"]
    # Within the limit, a blob goes through.
    assert (await client.blob(b"z" * 1000)).status_code == 201


async def test_registry_failures_during_an_upload_name_nothing_upstream(hub: Hub):
    client = await pusher(hub)
    hub.upstream.refuse_writes = httpx.Response(500, text=f"boom at {BACKING_NAMES[0]} as {UPSTREAM_USER}")
    assert (await client.start()).status_code == 503
    hub.upstream.refuse_writes = None
    location = (await client.start()).headers["location"]
    hub.upstream.refuse_writes = httpx.Response(403, text="the robot may not push")
    forbidden = await hub.request("PATCH", location, headers=client.headers, content=b"x")
    assert forbidden.status_code == 503 and forbidden.json()["errors"][0]["code"] == "UNAVAILABLE"
    hub.upstream.refuse_writes = httpx.Response(307, headers={"Location": "https://elsewhere.example/upload"})
    redirected = await hub.request("PATCH", location, headers=client.headers, content=b"x")
    assert redirected.status_code == 503, "a write is never redirected"
    assert not any(request.url.host == "elsewhere.example" for request in hub.upstream.requests)
    hub.upstream.refuse_writes = None
    hub.upstream.uploads.clear()  # the registry lost the session
    lost = await hub.request("PATCH", location, headers=client.headers, content=b"x")
    assert lost.status_code == 404 and lost.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"
    assert_no_backing_details(hub.responses)


async def test_a_long_upload_does_not_spend_the_budget_of_the_store_calls_after_it(hub: Hub):
    """The transfer may take longer than a metadata budget; the bookkeeping after it still gets its own."""

    client = await pusher(hub)
    location = (await client.start()).headers["location"]
    hub.app.state.cog_registry_serving = replace(hub.serving, max_metadata_seconds=0.2)
    hub.upstream.upload_delay = 0.5
    patched = await hub.request("PATCH", location, headers=client.headers, content=b"slow but fine")
    assert patched.status_code == 202 and patched.headers["range"] == "0-12"
    closed = await hub.request("PUT", location, headers=client.headers, params={"digest": sha256(b"slow but fine")})
    assert closed.status_code == 201


async def test_a_push_that_outlives_its_deadline_is_a_503(hub: Hub):
    client = await pusher(hub)
    location = (await client.start()).headers["location"]
    hub.app.state.cog_registry_serving = replace(hub.serving, max_blob_seconds=0.1, max_metadata_seconds=0.1)
    hub.upstream.upload_delay = 2.0
    slow = await hub.request("PATCH", location, headers=client.headers, content=b"x")
    assert slow.status_code == 503 and slow.json()["errors"][0]["code"] == "UNAVAILABLE"


# -- what a sweep does afterwards ----------------------------------------------------------


async def test_a_sweep_finds_published_repositories_without_a_list_and_keeps_the_publisher(tmp_path, monkeypatch):
    """The publish source has no repository list; the sweep enumerates what was published, and reconciles it."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    upstream = FakeRegistry()
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(
        config_module,
        "build_registry_sources",
        lambda configs, **kwargs: build_registry_sources(configs, http_transport=transport, **kwargs),
    )
    values = settings(tmp_path, publish=EVERYONE)
    del values["cogs"]["registry_sources"][0]["repositories"]  # nothing configured to enumerate
    values["cogs"]["index"] = {"enabled": True, "run_on_startup": False, "interval_seconds": 3600}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            hub = Hub(app, http, upstream)
            client = await pusher(hub)
            assert (await client.bundle(COG, "1.0.0")).status_code == 201
            # Pushed to the registry directly afterwards, into the same repository: only a sweep can find it.
            direct = CogBundle(extra=b"pushed behind the hub's back")
            upstream.publish_raw(REPO, MEDIA_TYPE_OCI_MANIFEST, direct.manifest, "2.0.0")
            upstream.blobs.update(direct.blobs)

            summary = await app.state.cog_indexer.sweep()
            assert summary.errors == [], summary.errors
            versions = {
                version["tags"][0]: version
                for version in (await hub.get(f"/v1/cogs/{COG_ID}", headers=ALICE)).json()["versions"]
            }
            assert set(versions) == {"1.0.0", "2.0.0"}
            assert versions["1.0.0"]["published_by"] == "alice", "the sweep left the publisher alone"
            assert versions["2.0.0"]["published_by"] is None, "an out-of-band push has no authenticated publisher"
            row = hub.catalog.get(COG.digest)
            assert row.status == STATUS_INDEXED and row.published_org == "org-a"
            # A second sweep changes nothing.
            await app.state.cog_indexer.sweep()
            assert hub.catalog.get(COG.digest).published_by == "alice"


async def test_a_publish_that_lands_during_a_sweep_is_not_declared_gone_by_it(tmp_path, monkeypatch):
    """The sweep listed the registry before the publish; its removal step must not tombstone the new row."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    upstream = FakeRegistry()
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(
        config_module,
        "build_registry_sources",
        lambda configs, **kwargs: build_registry_sources(configs, http_transport=transport, **kwargs),
    )
    values = settings(tmp_path, publish=EVERYONE)
    del values["cogs"]["registry_sources"][0]["repositories"]
    values["cogs"]["index"] = {"enabled": True, "run_on_startup": False, "interval_seconds": 3600}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            hub = Hub(app, http, upstream)
            client = await pusher(hub)
            older = CogBundle(extra=b"published before the sweep")
            assert (await client.bundle(older, "0.9.0")).status_code == 201
            indexer = app.state.cog_indexer
            enumerate_source = indexer._enumerate

            async def enumerate_then_publish(source):
                listing = await enumerate_source(source)  # the registry as it was...
                assert (await client.bundle(COG, "1.0.0")).status_code == 201  # ...and then a publish lands
                return listing

            indexer._enumerate = enumerate_then_publish
            summary = await indexer.sweep()
            assert summary.errors == [] and summary.removed == 0
            assert hub.catalog.get(COG.digest).removed_at is None, "the sweep did not tombstone the new version"
            assert hub.catalog.get(older.digest).removed_at is None
            listed = (await hub.get(f"/v1/cogs/{COG_ID}", headers=ALICE)).json()
            assert listed["digest"] == COG.digest and len(listed["versions"]) == 2
            # A version that really is gone from the registry is still removed, by this and later sweeps.
            indexer._enumerate = enumerate_source
            for key in [key for key in upstream.manifests if key[1] in ("0.9.0", older.digest)]:
                del upstream.manifests[key]
            assert (await indexer.sweep()).removed == 1
            assert hub.catalog.get(older.digest).removed_at is not None
            assert hub.catalog.get(COG.digest).removed_at is None


async def test_an_anonymous_catalog_reader_is_not_told_who_published(make_hub):
    rules = [{"path": "/v1/cogs", "match": "prefix", "access": "public"}]
    hub = await make_hub(publish=EVERYONE, security={"default_access": "authenticated", "paths": rules})
    client = await pusher(hub)
    assert (await client.bundle(COG)).status_code == 201
    for url in ("/v1/cogs", f"/v1/cogs/{COG_ID}", f"/v1/cogs/{COG_ID}/versions/{COG.digest}"):
        signed_in = (await hub.get(url, headers=ALICE)).text
        anonymous = await hub.get(url)
        assert anonymous.status_code == 200, url
        assert '"published_by":"alice"' in signed_in.replace(" ", "") and "published" not in anonymous.text, url


async def test_blocked_publishing_storage_is_bounded_like_every_other_store_call(hub: Hub):
    """A push's own store calls (ownership, sessions) spend from the request budget in the database."""

    import threading
    import time
    from contextlib import contextmanager

    import psycopg

    from collab_hub_api.cogs.publish_store import PostgresPublishStore

    state = {"timeouts": [], "checked_out": 0}
    released = threading.Event()

    class Blocked:
        def execute(self, sql, params=None):
            if "set_config('statement_timeout'" in sql:
                state["timeouts"].append(int(params[0]))
                return self
            time.sleep(state["timeouts"][-1] / 1000)
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            state["checked_out"] += 1
            try:
                yield Blocked()
            finally:
                state["checked_out"] -= 1
                released.set()

    client = await pusher(hub)
    hub.serving.publisher._store = PostgresPublishStore(Database())
    hub.app.state.cog_registry_serving = replace(hub.serving, max_metadata_seconds=0.3, max_blob_seconds=0.3)
    writes = len(hub.upstream.writes())
    for method, path, body in PUSHES:
        released.clear()
        started = time.monotonic()
        response = await hub.request(
            method, path.format(repo=REPO, upload="up-" + "0" * 32), headers=client.headers, content=body
        )
        assert response.status_code == 503, (method, path, response.status_code)
        assert time.monotonic() - started < 2.0
        assert await asyncio.to_thread(released.wait, 2.0) and state["checked_out"] == 0
        assert 1 <= state["timeouts"][-1] <= 300
    assert len(hub.upstream.writes()) == writes, "authorization failed closed before any write"
