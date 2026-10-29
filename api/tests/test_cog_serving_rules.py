"""The rules a pull through the Hub is held to (issue #179), each pinned by a request that would break it.

Written against the HTTP surface with the fake backing registry of
``test_cog_serving``, so each test states an outcome a client would see
rather than how the Hub arrives at it:

- a pull token is worth nothing once its owner loses catalog access;
- an indexed pin is pullable however many newer versions exist;
- a blob is pullable only while a pullable manifest references it;
- a response that ends early closes the upstream it was relaying from.
"""

# ruff: noqa: F811 - the fixtures imported from test_cog_serving are used as parameters

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from test_cog_serving import (
    ALPHA,
    MEDIA_TYPE_OCI_MANIFEST,
    REPO,
    T0,
    Bundle,
    FakeRegistry,
    Hub,
    basic,
    bearer,
    catalog_row,
    descriptor,
    hub,  # noqa: F401 - fixture
    make_hub,  # noqa: F401 - fixture
    settings,
    sha256,
)

from collab_hub_api import config as config_module
from collab_hub_api.cogs.oci import MEDIA_TYPE_NEBI_ASSET, MEDIA_TYPE_PIXI_CONFIG, OCIClient
from collab_hub_api.cogs.registry import build_registry_sources
from collab_hub_api.cogs.serving import CogRegistryFront
from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV
from collab_hub_api.frames.orgs import MEMBERSHIP_REMOVED, OrgsUnavailableError
from collab_hub_api.routers.registry import BlobStreamAborted

MEMBER = "sub-member-0001"
MEMBER_TOKEN = bearer({"sub": MEMBER, "sid": "session-member"})
ORG = "org-1111"


@pytest_asyncio.fixture
async def membership_hubs(tmp_path, monkeypatch):
    """Two replicas of one membership-resolving Hub: separate apps, one catalog, one credential store, one org store."""

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
    values = settings(tmp_path)
    values["frames"]["orgs"] = {"backend": "memory"}
    stack, hubs = [], []
    for _ in range(2):
        app = make_app(Config.parse(values))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        stack.append((lifespan, client))
        hubs.append(Hub(app, client, upstream))
    first, second = hubs
    # What a shared database gives two replicas. Nothing else is shared: each
    # app has its own router, its own sources and its own process state.
    serving = second.app.state.cog_registry_serving
    second.app.state.org_store = first.app.state.org_store
    second.app.state.cog_catalog_store = first.catalog
    second.app.state.cog_registry_serving = type(serving)(
        front=CogRegistryFront(first.catalog, serving.front.sources, max_blob_bytes=1 << 30),
        credentials=first.serving.credentials,
        host=serving.host,
        token_url=serving.token_url,
        credential_ttl_seconds=serving.credential_ttl_seconds,
        token_ttl_seconds=serving.token_ttl_seconds,
        max_blob_seconds=serving.max_blob_seconds,
    )
    first.app.state.org_store.set_membership(MEMBER, ORG)
    yield first, second
    for lifespan, client in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


READS = (
    ("GET", "/v2/"),
    ("HEAD", "/v2/"),
    ("GET", f"/v2/{REPO}/tags/list"),
    ("GET", f"/v2/{REPO}/manifests/latest"),
    ("HEAD", f"/v2/{REPO}/manifests/latest"),
    ("GET", f"/v2/{REPO}/manifests/{ALPHA.digest}"),
    ("HEAD", f"/v2/{REPO}/manifests/{ALPHA.digest}"),
    ("GET", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
    ("HEAD", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
)


async def test_a_token_stops_working_everywhere_when_its_owner_loses_catalog_access(membership_hubs):
    """Mint while a member, remove the membership, then use the token on every read route of another replica."""

    first, second = membership_hubs
    first.seed(REPO, ALPHA, "latest")
    # Both kinds of token: from a registry credential, and straight from the Hub session.
    credential = await first.exchange(MEMBER_TOKEN)
    from_credential = {"Authorization": f"Bearer {await first.token(credential, REPO)}"}
    direct = await first.get("/v2/token", params={"scope": f"repository:{REPO}:pull"}, headers=MEMBER_TOKEN)
    from_session = {"Authorization": f"Bearer {direct.json()['token']}"}

    for headers in (from_credential, from_session):
        for replica in (first, second):
            for method, path in READS:
                response = await replica.request(method, path, headers=headers)
                assert response.status_code == 200, (method, path, response.status_code)
    assert (await second.get("/v1/cogs", headers=MEMBER_TOKEN)).status_code == 200

    first.app.state.org_store.set_membership(MEMBER, ORG, status=MEMBERSHIP_REMOVED)

    # The catalog refuses this caller now, so the registry surface does too:
    # same token, same replicas, no bytes and no tags.
    catalog = await second.get("/v1/cogs", headers=MEMBER_TOKEN)
    assert catalog.status_code == 403 and catalog.json()["error"]["code"] == "no_organization"
    asked = len(first.upstream.requests)
    for headers in (from_credential, from_session):
        for replica in (second, first):
            for method, path in READS:
                response = await replica.request(method, path, headers=headers)
                assert response.status_code == 403, (method, path, response.status_code)
                assert response.content == b"" or response.json()["errors"][0]["code"] == "DENIED"
                assert "docker-content-digest" not in response.headers
    assert len(first.upstream.requests) == asked, "a refused caller costs the source nothing"
    # And the credential mints nothing more.
    refused = await second.get("/v2/token", headers=basic(credential["username"], credential["secret"]))
    assert refused.status_code == 403
    assert (await second.get("/v2/token", headers=MEMBER_TOKEN)).status_code == 403

    # Restored, the same token works again: it was the owner's standing, not the token, that changed.
    first.app.state.org_store.set_membership(MEMBER, ORG)
    assert (await second.get(f"/v2/{REPO}/manifests/latest", headers=from_credential)).status_code == 200


async def test_a_membership_lookup_that_fails_admits_nobody(membership_hubs, monkeypatch):
    first, second = membership_hubs
    first.seed(REPO, ALPHA, "latest")
    headers = await first.pull_token(REPO, who=MEMBER_TOKEN)
    assert (await second.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200

    def down(_user):
        raise OrgsUnavailableError("organization storage is down")

    monkeypatch.setattr(first.app.state.org_store, "resolve_principal", down)
    for method, path in READS:
        response = await second.request(method, path, headers=headers)
        assert response.status_code == 503, (method, path, response.status_code)
        if method == "GET":
            assert response.json()["errors"][0]["code"] == "UNAVAILABLE"


async def test_an_old_pin_stays_pullable_however_many_newer_versions_exist(hub: Hub):
    """No window stands between an indexed digest and its row: version 1 of 300 pulls like version 300."""

    oldest = Bundle("oldest")
    hub.seed(REPO, oldest, "v0", pushed_at=T0 - timedelta(days=400))
    for index in range(299):
        digest = "sha256:" + f"{index + 1:064x}"
        hub.catalog.upsert(catalog_row(REPO, digest, tags=(f"v{index + 1}",), pushed_at=T0 - timedelta(days=index)))
    # Rows of a source this Hub no longer has do not crowd it out either.
    for index in range(20):
        retired = catalog_row(REPO, oldest.digest, tags=("v0",), source_id=f"retired-{index}", pushed_at=T0)
        hub.catalog.upsert(retired)
    headers = await hub.pull_token(REPO)

    card = await hub.get(f"/v1/cogs/example/cog-alpha/versions/{oldest.digest}", headers=bearer_alice())
    assert card.status_code == 200
    for reference in (oldest.digest, "v0"):
        manifest = await hub.get(f"/v2/{REPO}/manifests/{reference}", headers=headers)
        assert manifest.status_code == 200 and manifest.content == oldest.manifest, reference
    for digest, data in oldest.blobs.items():
        blob = await hub.get(f"/v2/{REPO}/blobs/{digest}", headers=headers)
        assert blob.status_code == 200 and blob.content == data
    tags = (await hub.get(f"/v2/{REPO}/tags/list", headers=headers)).json()["tags"]
    assert len(tags) == 300 and "v0" in tags and "v299" in tags


def bearer_alice() -> dict[str, str]:
    return bearer({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "ws"})


async def test_removing_a_version_takes_its_blobs_unless_another_version_shares_them(hub: Hub):
    first = Bundle("first")
    unique = b"only in the second manifest"
    second_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": descriptor(MEDIA_TYPE_PIXI_CONFIG, first.config),
            "layers": [descriptor(MEDIA_TYPE_NEBI_ASSET, unique, "COG.md")],
        }
    ).encode()
    hub.seed(REPO, first, "first")
    second_digest = hub.upstream.publish_raw(REPO, MEDIA_TYPE_OCI_MANIFEST, second_manifest, "second")
    hub.upstream.blobs[sha256(unique)] = unique
    hub.catalog.upsert(catalog_row(REPO, second_digest, tags=("second",), pushed_at=T0 - timedelta(days=1)))
    headers = await hub.pull_token(REPO)
    for tag in ("first", "second"):
        assert (await hub.get(f"/v2/{REPO}/manifests/{tag}", headers=headers)).status_code == 200
    shared, own = sha256(first.config), sha256(first.files["pixi.toml"][1])

    async def status(digest: str) -> int:
        head = await hub.request("HEAD", f"/v2/{REPO}/blobs/{digest}", headers=headers)
        get = await hub.get(f"/v2/{REPO}/blobs/{digest}", headers=headers)
        assert head.status_code == get.status_code
        return get.status_code

    assert [await status(d) for d in (shared, own, sha256(unique))] == [200, 200, 200]

    hub.catalog.mark_removed_one("backing", REPO, first.digest)
    asked = len(hub.upstream.requests)
    assert await status(own) == 404, "its own blob went with it, at once"
    assert len(hub.upstream.requests) == asked
    assert await status(shared) == 200, "the second manifest still references this one"

    hub.catalog.mark_removed_one("backing", REPO, second_digest)
    assert [await status(d) for d in (shared, sha256(unique))] == [404, 404]
    # Back in the registry and reindexed: its descriptors went with the removal, so the blob
    # is pullable again as soon as the manifest is read, which is what a client does first.
    hub.catalog.upsert(catalog_row(REPO, first.digest, tags=("first",)))
    assert await status(own) == 404
    assert (await hub.request("HEAD", f"/v2/{REPO}/manifests/first", headers=headers)).status_code == 200
    assert await status(own) == 200


async def test_a_blob_falls_back_to_another_source_holding_the_same_manifest():
    """One digest in two sources: whichever answers the manifest, both can then be asked for its blobs."""

    from collab_hub_api.cogs.catalog import InMemoryCogCatalogStore
    from collab_hub_api.cogs.serving import BlobUnknown, UpstreamUnavailable

    bundle = Bundle("shared")
    config = sha256(bundle.config)
    blob_status = {"a": 200, "b": 200}
    asked: list[tuple[str, str]] = []

    def registry(name: str):
        def handler(request: httpx.Request) -> httpx.Response:
            kind = "manifest" if "/manifests/" in request.url.path else "blob"
            asked.append((name, kind))
            if kind == "manifest":
                return httpx.Response(200, content=bundle.manifest, headers={"Content-Type": MEDIA_TYPE_OCI_MANIFEST})
            if blob_status[name] != 200:
                return httpx.Response(blob_status[name])
            return httpx.Response(200, content=bundle.config)

        client = OCIClient(f"https://{name}.example", transport=httpx.MockTransport(handler))
        return type("Source", (), {"id": name, "host": f"{name}.example", "oci": lambda self: client})()

    class Counting(InMemoryCogCatalogStore):
        writes = 0

        def record_manifest_blobs(self, *args):
            type(self).writes += 1
            return super().record_manifest_blobs(*args)

    store = Counting()
    store.upsert(catalog_row(REPO, bundle.digest, source_id="a", pushed_at=T0))
    store.upsert(catalog_row(REPO, bundle.digest, source_id="b", pushed_at=T0 - timedelta(days=1)))
    front = CogRegistryFront(store, [registry("a"), registry("b")], max_blob_bytes=1 << 20)

    assert (await front.manifest(REPO, bundle.digest)).body == bundle.manifest
    assert asked == [("a", "manifest")], "only the first source was asked for the manifest"
    # Recorded for both sources, though only one served it: same digest, same descriptors.
    assert [found.source_id for found in store.find_blob(REPO, config, ("a", "b"))] == ["a", "b"]
    assert Counting.writes == 2

    # Read again, by tag and by digest: nothing is written a second time.
    await front.manifest(REPO, "latest")
    await front.manifest(REPO, bundle.digest)
    assert Counting.writes == 2

    for status, error in ((404, None), (500, None)):
        blob_status["a"] = status
        asked.clear()
        served = await front.blob(REPO, config)
        assert b"".join([chunk async for chunk in served.chunks]) == bundle.config
        await served.aclose()
        assert asked == [("a", "blob"), ("b", "blob")], status
    # Both gone is unknown; one gone and one failing is unavailable, not unknown.
    blob_status.update(a=404, b=404)
    with pytest.raises(BlobUnknown):
        await front.blob(REPO, config)
    blob_status.update(a=404, b=503)
    with pytest.raises(UpstreamUnavailable):
        await front.blob(REPO, config)


async def test_a_manifest_read_writes_its_descriptors_once(hub: Hub, monkeypatch):
    """HEAD then GET, then every later pull: one write for the life of the digest."""

    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    writes: list[str] = []
    record = hub.catalog.record_manifest_blobs
    monkeypatch.setattr(
        hub.catalog, "record_manifest_blobs", lambda *args: (writes.append(args[2]), record(*args))[1]
    )
    for method, reference in (("HEAD", "latest"), ("GET", ALPHA.digest), ("GET", "latest"), ("HEAD", ALPHA.digest)):
        assert (await hub.request(method, f"/v2/{REPO}/manifests/{reference}", headers=headers)).status_code == 200
    assert writes == [ALPHA.digest]
    assert (await hub.get(f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}", headers=headers)).content == ALPHA.config


async def test_a_reader_that_stops_reading_does_not_keep_the_upstream_open(hub: Hub, monkeypatch):
    """The deadline passes while the response is blocked sending to the client: the upstream is closed.

    The stuck client connection itself is the server's to reap (the response
    sits behind middleware that is blocked in ``send``); what the Hub owns,
    and must not leak, is the connection to the source.
    """

    # Large enough that the relay cannot have read it all before the client stalls.
    large = Bundle("stalled", big=4_000_000)
    hub.seed(REPO, large, "latest")
    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    big = sha256(large.files["model.bin"][1])
    # The source's body as a real stream, a chunk at a time, so the relay cannot have drained it early.
    async def body(data: bytes):
        for offset in range(0, len(data), 65536):
            await asyncio.sleep(0)
            yield data[offset : offset + 65536]

    hub.upstream._slowly = body
    hub.upstream.stream_delay = 1
    opened = []
    real_open = OCIClient.open_blob

    async def recording_open(self, repo, digest):
        stream = await real_open(self, repo, digest)
        opened.append(stream)
        return stream

    monkeypatch.setattr(OCIClient, "open_blob", recording_open)
    serving = hub.serving
    hub.app.state.cog_registry_serving = type(serving)(
        **{**{f: getattr(serving, f) for f in serving.__dataclass_fields__}, "max_blob_seconds": 0.3}
    )

    sent: list[dict] = []
    stalled = asyncio.Event()

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            stalled.set()
            await asyncio.sleep(3600)  # a client that has stopped reading

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/v2/{REPO}/blobs/{big}",
        "raw_path": f"/v2/{REPO}/blobs/{big}".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"authorization", headers["Authorization"].encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }
    exchange = asyncio.create_task(hub.app(scope, receive, send))
    try:
        await asyncio.wait_for(stalled.wait(), timeout=5)
        (stream,) = opened
        assert not stream._closed, "still relaying when the client stopped reading"
        # The deadline (0.3 s) passes with the response blocked on the client.
        for _ in range(100):
            if stream._closed:
                break
            await asyncio.sleep(0.05)
        assert stream._closed and stream._response.is_closed, "the upstream response must not outlive the deadline"
    finally:
        # The server (not the app) owns the stuck client connection; here the test does.
        exchange.cancel()
        with pytest.raises((asyncio.CancelledError, BlobStreamAborted)):
            await exchange
    received = sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
    assert received < len(large.files["model.bin"][1])


async def test_tags_page_all_the_way_to_the_end(hub: Hub):
    """10,001 tags: a default page is bounded, and following ``Link`` reaches the last one."""

    hub.seed(REPO, ALPHA, "latest")
    tags = tuple(f"v{index:05d}" for index in range(10_001))
    hub.catalog.upsert(catalog_row(REPO, "sha256:" + "9" * 64, tags=tags, pushed_at=T0 - timedelta(days=1)))
    headers = await hub.pull_token(REPO)

    first = await hub.get(f"/v2/{REPO}/tags/list", headers=headers)
    assert len(first.json()["tags"]) == 1000 and first.json()["tags"][0] == "latest"
    assert first.headers["link"] == f'</v2/{REPO}/tags/list?n=1000&last=v00998>; rel="next"'

    after = await hub.get(f"/v2/{REPO}/tags/list", params={"last": "v09999"}, headers=headers)
    assert after.json()["tags"] == ["v10000"] and "link" not in after.headers
    assert (await hub.get(f"/v2/{REPO}/tags/list", params={"last": "v10000"}, headers=headers)).json()["tags"] == []

    collected, url, pages = [], f"/v2/{REPO}/tags/list?n=1000", 0
    while url:
        page = await hub.get(url, headers=headers)
        assert page.status_code == 200
        collected.extend(page.json()["tags"])
        link = page.headers.get("link")
        url = link[1 : link.index(">")] if link else None
        pages += 1
    assert collected == sorted(("latest", *tags)) and pages == 11
    # n is capped at the page size, and still links onward.
    capped = await hub.get(f"/v2/{REPO}/tags/list", params={"n": 50_000}, headers=headers)
    assert len(capped.json()["tags"]) == 1000 and "link" in capped.headers


async def test_a_blocked_database_call_answers_503_in_time_and_releases_its_worker_and_connection(hub: Hub):
    """The request budget reaches the database: the store call is bounded by the server, not by the coroutine.

    The connection here is a fake that behaves as Postgres does under a
    ``statement_timeout``: the statement blocks, and is cancelled when the
    timeout the Hub set for it elapses. What this proves is the Hub's half --
    that it sets the timeout from the request budget, bounds the pool wait,
    answers 503 in time, and that once the statement is cancelled the worker
    thread unwinds and the connection is returned. That Postgres honours
    ``statement_timeout`` is its own, and is exercised by the live test below.
    """

    import threading
    import time
    from contextlib import contextmanager

    import psycopg

    from collab_hub_api.cogs.catalog import PostgresCogCatalogStore

    events: dict[str, float] = {}
    state = {"timeout_ms": None, "acquire_timeout": None, "checked_out": 0}
    released = threading.Event()

    class BlockingConnection:
        def execute(self, sql, params=None):
            if "set_config('statement_timeout'" in sql:
                state["timeout_ms"] = int(params[0])
                return self
            events["blocked_at"] = time.monotonic()
            time.sleep(state["timeout_ms"] / 1000)  # the server gives up exactly when it was told to
            events["cancelled_at"] = time.monotonic()
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            state["acquire_timeout"] = timeout
            state["checked_out"] += 1
            try:
                yield BlockingConnection()
            finally:
                state["checked_out"] -= 1
                released.set()

    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    serving = hub.serving
    blocked = CogRegistryFront(PostgresCogCatalogStore(Database()), serving.front.sources, max_blob_bytes=1 << 30)
    fields = {name: getattr(serving, name) for name in serving.__dataclass_fields__}
    hub.app.state.cog_registry_serving = type(serving)(**{**fields, "front": blocked, "max_metadata_seconds": 0.4})

    for method, path in (
        ("GET", f"/v2/{REPO}/manifests/latest"),
        ("HEAD", f"/v2/{REPO}/manifests/{ALPHA.digest}"),
        ("GET", f"/v2/{REPO}/tags/list"),
        ("HEAD", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
        ("GET", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
    ):
        released.clear()
        started = time.monotonic()
        response = await hub.request(method, path, headers=headers)
        elapsed = time.monotonic() - started
        assert response.status_code == 503, (method, path, response.status_code)
        assert elapsed < 2.0, f"{method} {path} took {elapsed:.2f}s against a 0.4s budget"
        # The budget, not a default: the statement timeout and the pool wait are what is left of 0.4 s.
        assert 1 <= state["timeout_ms"] <= 400 and 0 < state["acquire_timeout"] <= 0.4
        # The worker was not abandoned mid-statement: it ran to the cancellation and gave the connection back.
        assert await asyncio.to_thread(released.wait, 2.0), "the connection was never released"
        assert state["checked_out"] == 0
        assert events["cancelled_at"] - events["blocked_at"] <= 0.45


def test_live_postgres_ends_a_statement_at_the_request_budget():
    """The other half of the test above, against a real server: ``statement_timeout`` cancels the statement."""

    import os
    import time

    url = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to run the live statement-timeout test")
    import psycopg

    from collab_hub_api.cogs import deadline
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(url, min_size=0, max_size=1, timeout_seconds=10.0)
    token = deadline.request_deadline.set(time.monotonic() + 0.3)
    try:
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            with deadline.bounded_connection(database) as conn:
                conn.execute("SELECT pg_sleep(10)")
        assert time.monotonic() - started < 2.0
        # The one pooled connection came back usable, with no timeout left on it.
        deadline.request_deadline.set(None)
        with database.connection() as conn:
            assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"
            assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
        # And a pool with no free connection is not waited on past the budget.
        deadline.request_deadline.set(time.monotonic() + 0.3)
        with database.connection():
            started = time.monotonic()
            with pytest.raises(psycopg.OperationalError):
                with deadline.bounded_connection(database):
                    pass
            assert time.monotonic() - started < 2.0
    finally:
        deadline.request_deadline.reset(token)
        database.close()


async def test_a_blocked_membership_lookup_is_bounded_like_every_other_store_call(membership_hubs):
    """The owner's standing is re-read on every request; that query spends from the same budget, in the database."""

    import threading
    import time
    from contextlib import contextmanager

    import psycopg

    from collab_hub_api.frames.orgs import PostgresOrgStore

    first, _second = membership_hubs
    first.seed(REPO, ALPHA, "latest")
    credential = await first.exchange(MEMBER_TOKEN)
    headers = {"Authorization": f"Bearer {await first.token(credential, REPO)}"}
    state = {"timeouts": [], "acquire": [], "checked_out": 0, "plain": 0}
    released = threading.Event()

    class LockedTable:
        def execute(self, sql, params=None):
            if "set_config('statement_timeout'" in sql:
                state["timeouts"].append(int(params[0]))
                return self
            assert "collab_org_members" in sql
            time.sleep(state["timeouts"][-1] / 1000)  # waits on the lock until the server cancels it
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            state["acquire"].append(timeout)
            state["checked_out"] += 1
            try:
                yield LockedTable()
            finally:
                state["checked_out"] -= 1
                released.set()

    first.app.state.org_store = PostgresOrgStore(Database())
    serving = first.serving
    fields = {name: getattr(serving, name) for name in serving.__dataclass_fields__}
    first.app.state.cog_registry_serving = type(serving)(**{**fields, "max_metadata_seconds": 0.4})

    requests = [
        ("GET", "/v2/", headers),
        ("GET", f"/v2/{REPO}/manifests/latest", headers),
        ("HEAD", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}", headers),
        ("GET", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}", headers),
        # The token endpoint re-reads the owner too, on both of its paths.
        ("GET", "/v2/token", basic(credential["username"], credential["secret"])),
    ]
    for method, path, auth in requests:
        released.clear()
        started = time.monotonic()
        response = await first.request(method, path, headers=auth)
        assert response.status_code == 503, (method, path, response.status_code)
        assert time.monotonic() - started < 2.0, (method, path)
        assert await asyncio.to_thread(released.wait, 2.0), "the membership query kept its connection"
        assert state["checked_out"] == 0
        assert 1 <= state["timeouts"][-1] <= 400 and 0 < state["acquire"][-1] <= 0.4, (method, path)
    assert len(state["timeouts"]) == len(requests)


def test_the_membership_lookup_is_unchanged_outside_a_registry_request():
    from contextlib import contextmanager

    from collab_hub_api.frames.orgs import PostgresOrgStore

    seen: list = []

    class Connection:
        def execute(self, sql, params=None):
            seen.append(" ".join(sql.split())[:40])
            return self

        def fetchone(self):
            return None

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            seen.append(("checkout", timeout))
            yield Connection()

    principal = PostgresOrgStore(Database()).resolve_principal("someone")
    assert principal.membership is None and principal.platform_role is None
    # The ordinary checkout and exactly one statement: no timeout, no preamble.
    assert seen[0] == ("checkout", None) and len(seen) == 2 and seen[1].startswith("SELECT m.user_id")


async def test_falling_back_to_another_source_is_not_a_way_past_the_blob_size_limit():
    """Two sources hold a blob under descriptors that disagree about its size; the cap holds for both."""

    from collab_hub_api.cogs.catalog import BlobDescriptor, InMemoryCogCatalogStore
    from collab_hub_api.cogs.serving import BlobTooLarge, BlobUnknown, ServedBlob

    body = b"0123456789"
    blob = sha256(body)
    asked: list[str] = []

    def registry(name: str, has_blob: bool):
        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(name)
            return httpx.Response(200, content=body) if has_blob else httpx.Response(404)

        client = OCIClient(f"https://{name}.example", transport=httpx.MockTransport(handler))
        return type("Source", (), {"id": name, "host": f"{name}.example", "oci": lambda self: client})()

    store = InMemoryCogCatalogStore()
    # The newer manifest (source a) understates the size and its registry has lost the blob;
    # the older one (source b) records the true ten bytes and still has it.
    store.upsert(catalog_row(REPO, "sha256:" + "a" * 64, source_id="a", pushed_at=T0))
    store.upsert(catalog_row(REPO, "sha256:" + "b" * 64, source_id="b", pushed_at=T0 - timedelta(days=1)))
    store.record_manifest_blobs("a", REPO, "sha256:" + "a" * 64, [BlobDescriptor(blob, 5)])
    store.record_manifest_blobs("b", REPO, "sha256:" + "b" * 64, [BlobDescriptor(blob, 10)])
    sources = [registry("a", False), registry("b", True)]

    capped = CogRegistryFront(store, sources, max_blob_bytes=5)
    assert await capped.blob_size(REPO, blob) == 5
    with pytest.raises(BlobUnknown):
        await capped.blob(REPO, blob)
    assert asked == ["a"], "the over-limit candidate in source b was never opened"

    # With no candidate inside the limit, it is refused outright, before any registry is asked.
    asked.clear()
    with pytest.raises(BlobTooLarge, match="over this registry's 4-byte limit"):
        await CogRegistryFront(store, sources, max_blob_bytes=4).blob(REPO, blob)
    assert asked == []

    # Under a limit that admits both, the fallback serves the real ten bytes.
    roomy = await CogRegistryFront(store, sources, max_blob_bytes=10).blob(REPO, blob)
    assert roomy.size == 10 and b"".join([chunk async for chunk in roomy.chunks]) == body

    # And the cap is enforced on the bytes themselves, whatever a descriptor claimed.
    stream = await sources[1].oci().open_blob(REPO, blob)
    lying = ServedBlob(stream, 10, max_bytes=5)
    received = bytearray()
    with pytest.raises(Exception, match="exceeds the 5-byte cap"):
        async for chunk in lying.chunks:
            received.extend(chunk)
    assert len(received) <= 5 and lying.closed


async def test_a_malformed_page_number_is_a_400_not_a_500(hub: Hub):
    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    for bad in ("²", "٣", "9" * 4301, "1" * 10, "-1", "1.5", " 1", "", "1e3"):
        response = await hub.get(f"/v2/{REPO}/tags/list", params={"n": bad}, headers=headers)
        if bad == "":
            assert response.status_code == 400, "an empty n is not a number either"
        assert response.status_code == 400, repr(bad[:12])
        assert response.json()["errors"][0]["code"] == "PAGINATION_NUMBER_INVALID"
    for good, count in (("1", 1), ("999999999", 1), ("0", 0), ("0001", 1)):
        response = await hub.get(f"/v2/{REPO}/tags/list", params={"n": good}, headers=headers)
        assert response.status_code == 200 and len(response.json()["tags"]) == count, good


def test_first_sign_in_provisioning_spends_from_the_request_budget_inside_a_registry_request():
    """A single-org first sign-in can be admitted at the token endpoint; that write is bounded there, and only there."""

    from contextlib import contextmanager

    import psycopg

    from collab_hub_api.cogs import deadline
    from collab_hub_api.frames.orgs import PostgresOrgStore

    seen: list = []

    class ConflictingInsertHeldElsewhere:
        def execute(self, sql, params=None):
            if "set_config('statement_timeout'" in sql:
                seen.append(("timeout", int(params[0])))
                return self
            seen.append(("sql", " ".join(sql.split())[:30]))
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            seen.append(("checkout", timeout))
            yield ConflictingInsertHeldElsewhere()

    store = PostgresOrgStore(Database())
    # Outside a registry request: the ordinary checkout, no timeout, the statement sent as it always was.
    with pytest.raises(psycopg.errors.QueryCanceled):
        store.provision_member("sub-1", "org-1", "Org One")
    assert seen[0] == ("checkout", None) and seen[1][0] == "sql" and len(seen) == 2

    seen.clear()
    token = deadline.request_deadline.set(deadline.time.monotonic() + 0.4)
    try:
        with pytest.raises(psycopg.errors.QueryCanceled):
            store.provision_member("sub-1", "org-1", "Org One")
    finally:
        deadline.request_deadline.reset(token)
    checkout, timeout, statement = seen
    assert checkout[0] == "checkout" and 0 < checkout[1] <= 0.4
    assert timeout[0] == "timeout" and 1 <= timeout[1] <= 400 and statement[0] == "sql"


def test_a_statement_is_not_sent_when_installing_its_timeout_used_up_the_budget(monkeypatch):
    from contextlib import contextmanager

    from collab_hub_api.cogs import deadline

    clock = [50.0]
    monkeypatch.setattr(deadline.time, "monotonic", lambda: clock[0])
    sent: list[str] = []

    class Connection:
        def execute(self, sql, params=None):
            sent.append(sql.split("(")[0].strip())
            if sql.startswith("SELECT set_config"):
                clock[0] += 2.0  # the setup round trip alone takes two seconds
            return self

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            yield Connection()

    token = deadline.request_deadline.set(clock[0] + 1.0)
    try:
        with pytest.raises(deadline.BudgetExhausted):
            with deadline.bounded_connection(Database()) as conn:
                conn.execute("SELECT application_query()")
    finally:
        deadline.request_deadline.reset(token)
    assert sent == ["SELECT set_config"], "the application statement never started after the deadline"


async def test_token_and_version_endpoints_do_not_wait_for_a_worker_past_their_budget(hub: Hub):
    """Every threadpool slot is busy: the budget covers the wait for one, not only the store calls."""

    import threading
    import time

    import anyio.to_thread

    credential = await hub.exchange()
    headers = {"Authorization": f"Bearer {await hub.token(credential, REPO)}"}
    serving = hub.serving
    fields = {name: getattr(serving, name) for name in serving.__dataclass_fields__}
    hub.app.state.cog_registry_serving = type(serving)(**{**fields, "max_metadata_seconds": 0.05})

    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    release = threading.Event()
    limiter.total_tokens = 1
    occupied = asyncio.create_task(anyio.to_thread.run_sync(release.wait))
    try:
        await asyncio.sleep(0.05)
        assert limiter.borrowed_tokens == 1, "the only worker slot is taken"
        for path, auth in (
            ("/v2/token", basic(credential["username"], credential["secret"])),
            ("/v2/", headers),
            (f"/v2/{REPO}/manifests/latest", headers),
        ):
            started = time.monotonic()
            response = await hub.request("GET", path, headers=auth)
            elapsed = time.monotonic() - started
            assert response.status_code == 503, (path, response.status_code)
            assert response.json()["errors"][0]["code"] == "UNAVAILABLE"
            assert elapsed < 1.0, f"{path} answered after {elapsed:.2f}s against a 0.05s budget"
            assert not release.is_set()
    finally:
        release.set()
        await occupied
        limiter.total_tokens = original
    # With a worker free again the same requests succeed.
    hub.app.state.cog_registry_serving = serving
    assert (await hub.get("/v2/", headers=headers)).status_code == 200
