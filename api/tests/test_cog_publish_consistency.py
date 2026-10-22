"""Publishing through the Hub (issue #180): what stays true when a push goes wrong, or two happen at once.

The happy paths and the authorization matrix are ``test_cog_publishing``.
These are the edges between the Hub's records and the backing registry's:
what a token may push to, repository by repository; what a refused or lost
manifest leaves behind; where a moved tag ends up; what a client is told
when the registry has a manifest the catalog could not list; upload sessions
at the cap, past their expiry and under two requests at once; a sweep racing
a publish; and what the Hub's HTTP client logs about the registry it writes
to.
"""

# ruff: noqa: F811 - the fixtures imported from the sibling suites are used as parameters

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from test_cog_publishing import (
    BOB,
    CAROL,
    COG,
    COG_ID,
    EVERYONE,
    OPERATOR,
    OWNER,
    PUSHES,
    REPO,
    CogBundle,
    hub,  # noqa: F401 - fixture
    member_token,
    membership_hub,  # noqa: F401 - fixture
    publish_credential,
    pusher,
    refused_everywhere,
)
from test_cog_serving import (
    ALICE,
    BACKING_HOST,
    HUB_HOST,
    MEDIA_TYPE_OCI_MANIFEST,
    UPLOAD_STATE,
    FakeRegistry,
    Hub,
    assert_no_backing_details,
    basic,
    make_hub,  # noqa: F401 - fixture
    settings,
    sha256,
)

from collab_hub_api import config as config_module
from collab_hub_api.cogs import deadline, publish_store, publishing
from collab_hub_api.cogs.catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    CogCatalogDataError,
    CogCatalogUnavailableError,
    PostgresCogCatalogStore,
)
from collab_hub_api.cogs.oci import OCITransportError
from collab_hub_api.cogs.registry import RegistryRepositoryNotFound, build_registry_sources
from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.routers import registry as registry_router

OTHER = "cogs/another-cog"


def store_of(hub: Hub):
    return hub.serving.publisher._store


async def upload(client, bundle, repo: str = REPO) -> None:
    for data in bundle.blobs.values():
        assert (await client.blob(data, repo)).status_code == 201


def intercept_catalog_write(hub: Hub, make):
    """Put ``make(original)`` in place of the catalog write a publish makes; returns a function that undoes it.

    That write is ``record_published``. ``upsert`` is replaced the same way
    so that these tests say the same thing about a publisher that writes its
    row with the sweep's statement.
    """

    originals = {name: getattr(hub.catalog, name, None) for name in ("record_published", "upsert")}
    for name, original in originals.items():
        setattr(hub.catalog, name, make(original))

    def restore() -> None:
        for name in originals:
            vars(hub.catalog).pop(name, None)

    return restore


def errors(response: httpx.Response) -> tuple[int, str, str]:
    """``(status, code, message)`` of a refusal; a response with no error body has neither."""

    if not response.content:
        return response.status_code, "", ""
    error = response.json()["errors"][0]
    return response.status_code, error["code"], error["message"]


# -- push is per repository ---------------------------------------------------------------


async def scoped(hub: Hub, credential: dict, *scopes: str) -> dict[str, str]:
    response = await hub.get(
        "/v2/token",
        params={"service": HUB_HOST, "scope": list(scopes)},
        headers=basic(credential["username"], credential["secret"]),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def test_push_on_one_repository_never_grants_writes_to_another_asked_for_pull_only(hub: Hub):
    """One token, ``pull,push`` on A and ``pull`` on B: with a publish credential, B is still read-only."""

    credential = await publish_credential(hub)
    for scopes in (
        (f"repository:{REPO}:pull,push", f"repository:{OTHER}:pull"),
        (f"repository:{REPO}:pull,push repository:{OTHER}:pull",),  # one parameter, space-separated
        (f"repository:{OTHER}:pull", f"repository:{REPO}:push"),  # in either order
    ):
        token = await scoped(hub, credential, *scopes)
        await refused_everywhere(hub, token, 403, "DENIED", OTHER)
        message = (await hub.request("POST", f"/v2/{OTHER}/blobs/uploads/", headers=token)).json()["errors"][0]
        assert "may not push to this repository" in message["message"]
        # It was asked to pull from B, and may: the refusal is the push, not the name.
        assert (await hub.get(f"/v2/{OTHER}/tags/list", headers=token)).status_code == 404
        # And it does push to A, the repository push was asked for.
        assert (await hub.request("POST", f"/v2/{REPO}/blobs/uploads/", headers=token)).status_code == 202
    assert not any(OTHER in write for write in hub.upstream.writes())

    # Asked for push alone, it pushes -- and does not thereby read.
    push_only = await scoped(hub, credential, f"repository:{REPO}:push")
    assert (await hub.request("POST", f"/v2/{REPO}/blobs/uploads/", headers=push_only)).status_code == 202
    assert (await hub.get(f"/v2/{REPO}/tags/list", headers=push_only)).status_code == 401
    # A repository the token does not name at all is a matter of scope, and says which to ask for.
    unnamed = await hub.request("POST", "/v2/cogs/unnamed/blobs/uploads/", headers=push_only)
    assert unnamed.status_code == 401 and 'error="insufficient_scope"' in unnamed.headers["www-authenticate"]

    # A pull credential asking to push is given nothing for that repository, not pull on it.
    reader = await hub.exchange(ALICE)
    asked = await scoped(hub, reader, f"repository:{REPO}:push", f"repository:{OTHER}:pull")
    assert (await hub.get(f"/v2/{REPO}/tags/list", headers=asked)).status_code == 401
    assert (await hub.get(f"/v2/{OTHER}/tags/list", headers=asked)).status_code == 404


def test_scopes_are_read_repository_by_repository():
    scopes = [
        "repository:cogs/a:pull",
        "repository:cogs/b:pull,push repository:cogs/c:push",
        "repository:cogs/a:pull",
        "repository:cogs/d:delete",
        "registry:catalog:*",
        "repository:Not/Valid:pull,push",
        "repository::push",
        "garbage",
    ]
    assert registry_router.requested_scope(scopes) == (["cogs/a", "cogs/b"], ["cogs/b", "cogs/c"])
    # A repository named twice gets what each entry asks, and nothing leaks between names.
    assert registry_router.requested_scope(["repository:cogs/a:pull repository:cogs/a:push"]) == (
        ["cogs/a"],
        ["cogs/a"],
    )
    # On a Hub that accepts no publishes, push names nothing: exactly the parsing it had before.
    assert registry_router.requested_scope(scopes, pushing=False) == (["cogs/a", "cogs/b"], [])
    assert registry_router.requested_repositories(scopes) == ["cogs/a", "cogs/b"]
    # The bound on names counts a repository once, whichever list it is in.
    many = [f"repository:cogs/r{i}:{'pull' if i % 2 else 'push'}" for i in range(40)]
    pull, push = registry_router.requested_scope(many)
    assert len(set(pull) | set(push)) == registry_router.MAX_TOKEN_SCOPES and not set(pull) & set(push)
    flood = ["repository:cogs/a:delete " * 100_000 + "repository:cogs/z:push"]
    assert registry_router.requested_scope(flood) == ([], [])


# -- whose repository it is: decided before the manifest is forwarded, never reassigned -----------


FOREVER = timedelta(days=30)


def manifest_puts(hub: Hub) -> list[str]:
    return [write for write in hub.upstream.writes() if "/manifests/" in write]


async def test_a_manifest_the_registry_refuses_claims_and_enumerates_nothing(hub: Hub):
    alice, bob = await pusher(hub, ALICE), await pusher(hub, BOB)
    await upload(alice, COG)
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(400, text="no")
    assert errors(await alice.manifest(COG, "1.0.0")) == (400, "MANIFEST_INVALID", "the registry refused the manifest")
    store = store_of(hub)
    assert store.get_repository(REPO) is None, "a refused manifest owns nothing"
    assert store.published_repositories("backing") == [], "and adds nothing to what a sweep enumerates"
    assert store._repositories == {}, "definitely refused: the pending row is gone, not left behind"
    assert hub.catalog._attempts == {}, "and so is the attempt: nobody is on record as publishing that digest"
    # The registry refusing the Hub's own credential is as definite: nothing was stored.
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(403, text="the robot may not push")
    assert (await alice.manifest(COG, "1.0.0")).status_code == 503
    assert store._repositories == {} and hub.catalog._attempts == {}
    del hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"]

    # So the name is still anybody's: another organization publishes to it and owns it.
    theirs = CogBundle(extra=b"bob's bytes")
    assert (await bob.bundle(theirs, "1.0.0")).status_code == 201
    assert store.get_repository(REPO).owner_org_id == "org-b"
    assert store.published_repositories("backing") == [REPO]
    await refused_everywhere(hub, alice.headers, 403, "DENIED")


async def test_accepted_content_never_becomes_another_organizations_repository(sweeping_hub: Hub):
    """The registry accepts A's manifest; recording that fails; B then tries to take the name. Never."""

    hub, store = sweeping_hub, store_of(sweeping_hub)
    now = [datetime.now(UTC)]
    store.clock = lambda: now[0]
    alice, bob = await pusher(hub, ALICE), await pusher(hub, BOB)
    theirs = CogBundle(extra=b"bob's bytes")
    await upload(alice, COG)
    await upload(bob, theirs)

    commit = store.commit_repository

    def down(*args, **kwargs):
        raise CogCatalogUnavailableError("the database is away")

    store.commit_repository = down
    status, code, message = errors(await alice.manifest(COG, "1.0.0"))
    assert (status, code) == (503, "UNAVAILABLE") and "stored in the registry" in message
    store.commit_repository = commit
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == COG.manifest, "A's manifest is in the registry"

    # However long B waits, the name is not B's to take, and nothing of B's reaches the registry.
    for wait in (timedelta(0), timedelta(minutes=6), FOREVER):
        now[0] += wait
        before = len(hub.upstream.writes())
        refused = await bob.manifest(theirs, "1.0.0")
        assert errors(refused)[:2] == (403, "DENIED"), wait
        assert len(hub.upstream.writes()) == before, "refused before anything was written to the registry"
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == COG.manifest
    record = store.get_repository(REPO)
    assert record is not None and (record.owner_org_id, record.committed) == ("org-a", False)
    assert (await bob.start()).status_code == 403, "not even an upload"

    # The sweep is sent there (the grace period is long past), finds A's content, lists it as A's, and settles
    # the repository as A's organization's.
    assert store.published_repositories("backing") == [REPO]
    summary = await hub.app.state.cog_indexer.sweep()
    assert summary.errors == []
    row = hub.catalog.get(COG.digest)
    assert (row.status, row.published_by, row.published_org) == (STATUS_INDEXED, "alice", "org-a")
    record = store.get_repository(REPO)
    assert (record.owner_org_id, record.committed) == ("org-a", True)
    assert (await bob.manifest(theirs, "1.0.0")).status_code == 403
    assert (await (await pusher(hub, CAROL)).manifest(COG, "1.0.1")).status_code == 201


async def test_an_unknown_outcome_leaves_the_name_pending_and_owned(sweeping_hub: Hub):
    hub, store = sweeping_hub, store_of(sweeping_hub)
    now = [datetime.now(UTC)]
    store.clock = lambda: now[0]
    alice, bob, carol = await pusher(hub, ALICE), await pusher(hub, BOB), await pusher(hub, CAROL)
    theirs = CogBundle(extra=b"bob's bytes")
    await upload(alice, COG)
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    assert (await alice.manifest(COG, "1.0.0")).status_code == 503
    del hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"]

    # The registry may or may not hold the manifest. The name is that organization's, unsettled.
    record = store.get_repository(REPO)
    assert record is not None and (record.owner_org_id, record.committed) == ("org-a", False)
    assert store.published_repositories("backing") == [], "a retry may still be on its way: no sweep yet"
    writes = len(hub.upstream.writes())
    await refused_everywhere(hub, bob.headers, 403, "DENIED")
    assert "another organization" in errors(await bob.manifest(theirs, "1.0.0"))[2]
    assert len(hub.upstream.writes()) == writes
    # A platform operator is another organization too, for a name that is not settled.
    # The same organization may try again; a refusal of that retry does not free a name the first
    # attempt may have written to.
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(400, text="no")
    assert (await carol.manifest(COG, "1.0.0")).status_code == 400
    del hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"]
    assert store.get_repository(REPO) == record

    # Past the grace period sweeps look there. Nothing landed: an empty repository, not an error, and it
    # stays pending and owned -- for as long as nobody settles it.
    now[0] += FOREVER
    assert store.published_repositories("backing") == [REPO]
    summary = await hub.app.state.cog_indexer.sweep()
    assert summary.errors == [] and summary.sources_failed == 0
    assert store.get_repository(REPO) == record
    assert (await bob.manifest(theirs, "1.0.0")).status_code == 403

    # The organization that holds it publishes, and it is settled.
    assert (await carol.manifest(COG, "1.0.0")).status_code == 201
    assert store.get_repository(REPO).committed is True
    assert hub.catalog.get(COG.digest).published_by == "carol"

    # A name that really is stuck is released by an operator (docs/cog-registry.md), by deleting the pending row.
    other = await pusher(hub, ALICE, OTHER)
    await upload(other, COG, OTHER)
    hub.upstream.fail[f"/v2/{OTHER}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    assert (await other.manifest(COG, "1.0.0", OTHER)).status_code == 503
    del hub.upstream.fail[f"/v2/{OTHER}/manifests/1.0.0"]
    assert not store._repositories.pop(OTHER).record.committed
    bobs = await pusher(hub, BOB, OTHER)
    assert (await bobs.bundle(theirs, "1.0.0", OTHER)).status_code == 201
    assert store.get_repository(OTHER).owner_org_id == "org-b"


async def test_a_refused_publish_does_not_release_a_concurrent_publish_of_the_same_organization(hub: Hub):
    """Alice's manifest is refused while Carol's, for the same new name, is on its way to the registry."""

    alice, carol, bob = await pusher(hub, ALICE), await pusher(hub, CAROL), await pusher(hub, BOB)
    newer, theirs = CogBundle(extra=b"carol's version"), CogBundle(extra=b"bob's bytes")
    await upload(alice, COG)
    await upload(carol, newer)
    oci = hub.serving.publisher._source.oci()
    forward, arrived, proceed = oci.put_manifest, asyncio.Event(), asyncio.Event()

    async def held(repo, ref, body, media_type):
        if ref == "2.0.0":
            arrived.set()
            await proceed.wait()
        return await forward(repo, ref, body, media_type)

    oci.put_manifest = held
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(400, text="no")
    carols = asyncio.create_task(carol.manifest(newer, "2.0.0"))
    await arrived.wait()
    assert (await alice.manifest(COG, "1.0.0")).status_code == 400
    # Carol's attempt still holds the name: it is not free for another organization in between.
    assert (await bob.manifest(theirs, "3.0.0")).status_code == 403
    proceed.set()
    assert (await carols).status_code == 201
    assert store_of(hub).get_repository(REPO).owner_org_id == "org-a"


async def test_a_name_released_by_an_operator_under_a_publish_in_flight_is_reported_and_logged(hub: Hub, caplog):
    """The one way a name can change hands: by hand. A publish that was in flight then is told, not listed."""

    alice = await pusher(hub, ALICE)
    await upload(alice, COG)
    store, oci = store_of(hub), hub.serving.publisher._source.oci()
    forward = oci.put_manifest

    async def released_meanwhile(repo, ref, body, media_type):
        del store._repositories[repo]
        store.commit_repository(repo, source_id="backing", owner_org_id="org-b", created_by="bob")
        return await forward(repo, ref, body, media_type)

    oci.put_manifest = released_meanwhile
    with caplog.at_level(logging.ERROR, logger="frames_server.cogs.publishing"):
        assert errors(await alice.manifest(COG, "1.0.0")) == (403, "DENIED", f"{REPO} belongs to another organization")
    logged = [record.message for record in caplog.records if record.name == "frames_server.cogs.publishing"]
    assert logged == ["cog_publish_ownership_lost"]
    assert store.get_repository(REPO).owner_org_id == "org-b" and hub.catalog.locations(COG.digest) == []


# -- a tag is on exactly one digest ------------------------------------------------------------


async def test_a_moved_tag_leaves_the_digest_it_was_on_and_a_put_by_digest_does_not_bring_it_back(hub: Hub):
    client = await pusher(hub)
    older, newer = CogBundle(extra=b"A"), CogBundle(extra=b"B")
    assert (await client.bundle(older, "latest")).status_code == 201
    assert (await client.manifest(older, "1.0.0")).status_code == 201
    assert (await client.bundle(newer, "latest")).status_code == 201

    def tags(bundle) -> tuple[str, ...]:
        return hub.catalog.get(bundle.digest, source_id="backing", repository=REPO).tags

    assert (tags(older), tags(newer)) == (("1.0.0",), ("latest",))
    # The older manifest is put again, by digest: the registry's `latest` is still the newer one.
    assert (await client.manifest(older, older.digest)).status_code == 201
    assert hub.upstream.manifests[(REPO, "latest")][1] == newer.manifest
    assert (tags(older), tags(newer)) == (("1.0.0",), ("latest",))
    served = await hub.get(f"/v2/{REPO}/manifests/latest", headers=client.headers)
    assert served.headers["docker-content-digest"] == newer.digest and served.content == newer.manifest
    listed = (await hub.get(f"/v2/{REPO}/tags/list", headers=client.headers)).json()["tags"]
    assert listed == ["1.0.0", "latest"]
    by_tag = {tag: v["digest"] for v in (await hub.get(f"/v1/cogs/{COG_ID}", headers=ALICE)).json()["versions"]
              for tag in v["tags"]}
    assert by_tag == {"1.0.0": older.digest, "latest": newer.digest}
    # Moved back by tag, it is on the older digest alone again.
    assert (await client.manifest(older, "latest")).status_code == 201
    assert (tags(older), tags(newer)) == (("1.0.0", "latest"), ())


# -- accepted by the registry, not listed by the catalog ----------------------------------------


async def test_a_manifest_the_catalog_cannot_store_is_not_reported_as_published(hub: Hub, caplog):
    client = await pusher(hub)
    await upload(client, COG)
    refusals = iter([CogCatalogDataError("DataError")])

    def refusing_the_card(write):
        def refuse_once(artifact, **kwargs):
            for refusal in refusals:
                raise refusal
            return write(artifact, **kwargs)

        return refuse_once

    restore = intercept_catalog_write(hub, refusing_the_card)
    caplog.set_level(logging.INFO, logger="frames_server.cogs.publishing")
    answer = await client.manifest(COG, "1.0.0")
    status, code, message = errors(answer)
    assert (status, code) == (500, "UNKNOWN")
    assert "stored in the registry" in message and "does not list it" in message
    assert "docker-content-digest" not in answer.headers and "location" not in answer.headers
    logged = [record.message for record in caplog.records]
    assert "cog_publish_unlisted" in logged and "cog_published" not in logged
    # The registry has it; the catalog shows nobody a Cog; and the failed row carries who published it.
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == COG.manifest
    assert (await hub.get("/v1/cogs", headers=ALICE)).json()["items"] == []
    assert (await hub.get(f"/v2/{REPO}/manifests/1.0.0", headers=client.headers)).status_code == 404
    row = hub.catalog.get(COG.digest)
    assert (row.status, row.published_by, row.published_org, row.tags) == (STATUS_FAILED, "alice", "org-a", ("1.0.0",))
    assert store_of(hub).get_repository(REPO).owner_org_id == "org-a", "the registry accepted it: the name is owned"
    # A sweep that later manages to index the digest keeps that attribution.
    restore()
    hub.catalog.upsert(replace(row, status=STATUS_INDEXED, card={}, cog_id=COG_ID, read_errors=()))
    assert hub.catalog.get(COG.digest).published_by == "alice"
    assert_no_backing_details(hub.responses)


async def test_an_outage_after_acceptance_says_the_manifest_is_stored_and_keeps_the_publisher(hub: Hub):
    client = await pusher(hub)
    await upload(client, COG)

    def down(_write):
        def unavailable(artifact, **kwargs):
            raise CogCatalogUnavailableError("the database is away")

        return unavailable

    restore = intercept_catalog_write(hub, down)
    status, code, message = errors(await client.manifest(COG, "1.0.0"))
    assert (status, code) == (503, "UNAVAILABLE")
    assert "stored in the registry but could not be listed" in message and "database" not in message
    assert hub.catalog.locations(COG.digest) == [], "no row at all"
    assert (REPO, "1.0.0") in hub.upstream.manifests
    assert store_of(hub).published_repositories("backing") == [REPO], "so a sweep is sent to it"
    # The attempt was marked accepted before anything else: whichever write lists the digest attributes it.
    # Here, the sweep's.
    restore()
    hub.catalog.upsert(
        replace(COG_ROW(hub), tags=("1.0.0",))  # what a sweep reads from the registry and stores
    )
    row = hub.catalog.get(COG.digest)
    assert (row.published_by, row.published_org) == ("alice", "org-a")

    # Or the client's own retry, once the catalog is back.
    newer = CogBundle(extra=b"second")
    await upload(client, newer)
    restore = intercept_catalog_write(hub, down)
    assert (await client.manifest(newer, "2.0.0")).status_code == 503
    restore()
    assert (await client.manifest(newer, "2.0.0")).status_code == 201
    assert hub.catalog.get(newer.digest).published_by == "alice"


async def test_a_publish_that_never_reached_the_registry_never_attributes_a_later_push_of_that_digest(hub: Hub):
    """Alice's attempt is on record; the connection fails before her manifest is sent; somebody else pushes it."""

    alice = await pusher(hub, ALICE)
    await upload(alice, COG)
    oci = hub.serving.publisher._source.oci()
    forward = oci.put_manifest

    async def connection_lost(repo, ref, body, media_type):
        raise OCITransportError("manifest put: ConnectError")

    oci.put_manifest = connection_lost
    assert (await alice.manifest(COG, "1.0.0")).status_code == 503
    oci.put_manifest = forward
    assert (REPO, "1.0.0") not in hub.upstream.manifests
    # The same digest arrives in the registry some other way, and a sweep's write lists it.
    hub.upstream.publish_raw(REPO, MEDIA_TYPE_OCI_MANIFEST, COG.manifest, "1.0.0")
    hub.catalog.upsert(replace(COG_ROW(hub), tags=("1.0.0",)))
    row = hub.catalog.get(COG.digest)
    assert (row.published_by, row.published_org) == (None, None), "nobody is known to have published it"
    # Not later either: an attempt that was never accepted attributes nothing, to this write or any other.
    hub.catalog.upsert(replace(COG_ROW(hub), tags=("1.0.0", "latest")))
    assert hub.catalog.get(COG.digest).published_by is None


async def test_a_refused_retry_does_not_take_the_publisher_of_an_accepted_attempt(hub: Hub):
    """PUT accepted, indexing fails; the same PUT again is definitely refused; the sweep then lists the digest."""

    alice, carol = await pusher(hub, ALICE), await pusher(hub, CAROL)
    await upload(alice, COG)

    def down(_write):
        def unavailable(artifact, **kwargs):
            raise CogCatalogUnavailableError("the database is away")

        return unavailable

    restore = intercept_catalog_write(hub, down)
    assert (await alice.manifest(COG, "1.0.0")).status_code == 503  # accepted by the registry, not listed
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(400, text="tag is immutable")
    assert (await alice.manifest(COG, "1.0.0")).status_code == 400  # the retry: definitely refused
    # A colleague's attempt at the same digest, whose outcome is never known, changes nothing either.
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    assert (await carol.manifest(COG, "1.0.0")).status_code == 503
    del hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"]
    restore()
    hub.catalog.upsert(replace(COG_ROW(hub), tags=("1.0.0",)))
    row = hub.catalog.get(COG.digest)
    assert (row.published_by, row.published_org) == ("alice", "org-a")
    assert store_of(hub).get_repository(REPO).owner_org_id == "org-a"


async def test_an_attempt_that_could_not_even_be_recorded_forwards_nothing_and_holds_nothing(hub: Hub):
    client, store = await pusher(hub), store_of(hub)
    await upload(client, COG)
    note = hub.catalog.note_publication

    def down(*args, **kwargs):
        raise CogCatalogUnavailableError("the database is away")

    hub.catalog.note_publication = down
    assert (await client.manifest(COG, "1.0.0")).status_code == 503
    hub.catalog.note_publication = note
    assert manifest_puts(hub) == [], "nothing was forwarded"
    assert store._repositories == {}, "so the name is not left pending behind it"


async def test_when_acceptance_cannot_be_recorded_the_publisher_stays_unknown_and_an_operator_settles_it(hub: Hub):
    client, store = await pusher(hub, ALICE, REPO, OTHER), store_of(hub)
    await upload(client, COG)
    await upload(client, COG, OTHER)
    accept = hub.catalog.accept_publication

    def down(attempt_id):
        raise CogCatalogUnavailableError("the database is away")

    hub.catalog.accept_publication = down
    for repo in (REPO, OTHER):
        status, _code, message = errors(await client.manifest(COG, "1.0.0", repo))
        assert status == 503 and "stored in the registry" in message
    hub.catalog.accept_publication = accept

    # Putting the manifest again before any sweep has been there settles everything: this time it is recorded.
    assert (await client.manifest(COG, "1.0.0", OTHER)).status_code == 201
    assert store.get_repository(OTHER).committed is True
    assert hub.catalog.get(COG.digest, repository=OTHER).published_by == "alice"

    # Otherwise the sweep lists the digest, and settles nothing by it: that the organization attempted this
    # digest does not show that its attempt is how the digest got there.
    hub.catalog.upsert(replace(COG_ROW(hub), tags=("1.0.0",)))
    publishing.settle_found(store, hub.catalog)("backing", {REPO: [COG.digest]})
    assert store.get_repository(REPO).committed is False
    assert hub.catalog.get(COG.digest, repository=REPO).published_by is None, "and nobody is guessed as publisher"
    # From then on it is an operator's to resolve: the organization's own retry is refused.
    assert errors(await client.manifest(COG, "1.0.0"))[:2] == (403, "DENIED")


async def test_a_digest_pushed_directly_does_not_settle_a_pending_repository_for_whoever_attempted_it(
    sweeping_hub: Hub,
):
    """A's PUT of digest D gets a 500 and stores nothing; somebody pushes D there directly; a sweep runs."""

    hub, store = sweeping_hub, store_of(sweeping_hub)
    now = [datetime.now(UTC)]
    store.clock = lambda: now[0]
    alice = await pusher(hub, ALICE)
    await upload(alice, COG)
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    assert (await alice.manifest(COG, "1.0.0")).status_code == 503
    del hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"]
    assert (REPO, "1.0.0") not in hub.upstream.manifests, "the registry stored nothing"
    # The very same digest, pushed to the registry by somebody else.
    hub.upstream.publish_raw(REPO, MEDIA_TYPE_OCI_MANIFEST, COG.manifest, "1.0.0")

    now[0] += FOREVER
    for _ in range(2):
        assert (await hub.app.state.cog_indexer.sweep()).errors == []
        record = store.get_repository(REPO)
        assert (record.owner_org_id, record.committed) == ("org-a", False), "identical content is not acceptance"
    row = hub.catalog.get(COG.digest)
    assert (row.status, row.published_by) == (STATUS_INDEXED, None)
    # So the organization cannot move the tag to something else of its own.
    newer = CogBundle(extra=b"what A would overwrite it with")
    await_upload = await alice.start()
    assert await_upload.status_code == 403
    assert errors(await alice.manifest(newer, "1.0.0"))[:2] == (403, "DENIED")
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == COG.manifest


def COG_ROW(hub: Hub):
    from test_cog_serving import catalog_row

    return catalog_row(REPO, COG.digest)


async def test_the_publish_write_runs_on_the_indexers_worker_under_the_requests_deadline(hub: Hub):
    client = await pusher(hub)
    await upload(client, COG)
    seen = []

    def watching(write):
        def watched(artifact, **kwargs):
            seen.append((threading.current_thread().name, deadline.request_deadline.get()))
            return write(artifact, **kwargs)

        return watched

    intercept_catalog_write(hub, watching)
    before = time.monotonic()
    assert (await client.manifest(COG, "1.0.0")).status_code == 201
    ((thread, budget),) = seen
    assert thread.startswith("cog-index-store"), "the lock-less targeted path, on the indexer's own executor"
    assert budget is not None and before < budget <= time.monotonic() + hub.serving.max_metadata_seconds


async def test_a_blocked_catalog_after_acceptance_is_bounded_by_the_request_and_says_what_happened(hub: Hub):
    """The publish write spends the request's budget in the database, not the sweep's."""

    import psycopg

    state = {"timeouts": [], "checked_out": 0}
    released = threading.Event()

    class Blocked:
        def execute(self, sql, params=None):
            if "set_config('statement_timeout'" in sql:
                state["timeouts"].append(int(params[0]))
                return self
            time.sleep(min(state["timeouts"][-1] / 1000, 3.0))
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
    await upload(client, COG)
    blocked = PostgresCogCatalogStore(Database())
    for name in ("record_published", "upsert"):
        setattr(hub.catalog, name, getattr(blocked, name, None))
    hub.app.state.cog_registry_serving = replace(hub.serving, max_metadata_seconds=0.4)
    started = time.monotonic()
    status, code, message = errors(await client.manifest(COG, "1.0.0"))
    assert (status, code) == (503, "UNAVAILABLE") and "stored in the registry" in message
    assert time.monotonic() - started < 2.0
    assert await asyncio.to_thread(released.wait, 2.0) and state["checked_out"] == 0
    assert state["timeouts"] and all(1 <= ms <= 400 for ms in state["timeouts"])


# -- upload sessions: the cap, expiry, and the registry's side of each ---------------------------


async def test_the_slot_is_taken_first_and_a_session_past_the_cap_is_cancelled_at_the_registry(hub: Hub, monkeypatch):
    monkeypatch.setattr(publish_store, "MAX_UPLOAD_SESSIONS_PER_USER", 3)
    monkeypatch.setattr(publish_store, "MAX_UPLOAD_ROWS_PER_USER", 5, raising=False)
    now = [datetime.now(UTC)]
    client, store = await pusher(hub), store_of(hub)
    store.clock = lambda: now[0]
    async def start() -> httpx.Response:
        now[0] += timedelta(seconds=1)
        return await client.start()

    locations = [(await start()).headers["location"] for _ in range(4)]
    assert len(hub.upstream.uploads) == 3, "the fourth pushed the first out, at the registry too"
    assert "upstream-1" not in hub.upstream.uploads and len(store._uploads) == 3
    assert (await hub.get(locations[0], headers=client.headers)).status_code == 404
    assert (await hub.get(locations[1], headers=client.headers)).status_code == 204

    # The registry will not cancel: the Hub keeps the record -- it is what remembers where to cancel.
    for upload_id in ("upstream-2", "upstream-3"):
        hub.upstream.fail[f"/v2/{REPO}/blobs/uploads/{upload_id}"] = httpx.Response(500, text="boom")
    assert (await start()).status_code == 202 and (await start()).status_code == 202
    assert len(store._uploads) == 5 and len(hub.upstream.uploads) == 5
    assert (await hub.get(locations[1], headers=client.headers)).status_code == 404, "retired all the same"
    # Now every slot is taken by a session that is live or still waiting to be cancelled: nothing more is opened.
    writes = len(hub.upstream.writes())
    full = await client.start()
    assert errors(full)[:2] == (429, "TOOMANYREQUESTS")
    opened = [write for write in hub.upstream.writes()[writes:] if write.startswith("POST")]
    assert opened == [] and len(hub.upstream.uploads) == 5, "refused before the registry opened anything"
    assert (await pusher(hub, BOB)) is not None and (await (await pusher(hub, BOB)).start()).status_code == 202

    # The registry answers again. A cancellation that failed is not retried at once (the claim is its backoff);
    # once it is due, the next open cancels what was waiting, and then has its slot.
    hub.upstream.fail.clear()
    assert (await client.start()).status_code == 429
    now[0] += timedelta(seconds=publishing.STALE_LEASE_SECONDS + 1)
    assert (await client.start()).status_code == 202
    assert "upstream-2" not in hub.upstream.uploads and "upstream-3" not in hub.upstream.uploads
    live = [key for key, stored in store._uploads.items() if stored.session.user_id == "alice"]
    assert len(live) == 3 and len(hub.upstream.uploads) == 4  # alice's three and bob's one
    assert_no_backing_details(hub.responses)


async def test_an_expired_session_is_cancelled_at_the_registry_before_it_is_forgotten(hub: Hub):
    now = [datetime.now(UTC)]
    store = store_of(hub)
    store.clock = lambda: now[0]
    alice, bob = await pusher(hub, ALICE), await pusher(hub, BOB)
    location = (await alice.start()).headers["location"]
    assert (await hub.request("PATCH", location, headers=alice.headers, content=b"partial")).status_code == 202
    now[0] += timedelta(seconds=publish_store.UPLOAD_SESSION_TTL_SECONDS + 1)
    for method in ("GET", "PATCH", "DELETE"):
        gone = await hub.request(method, location, headers=alice.headers, content=b"x" if method == "PATCH" else None)
        assert gone.status_code == 404, method
    assert list(hub.upstream.uploads) == ["upstream-1"], "expired here, still open there"
    hub.upstream.fail[f"/v2/{REPO}/blobs/uploads/upstream-1"] = httpx.Response(503, text="later")
    assert (await bob.start()).status_code == 202
    assert len(store._uploads) == 2 and "upstream-1" in hub.upstream.uploads, "kept until the registry lets go"
    hub.upstream.fail.clear()
    now[0] += timedelta(seconds=publishing.STALE_LEASE_SECONDS + 1)
    assert (await bob.start()).status_code == 202
    assert "upstream-1" not in hub.upstream.uploads and len(store._uploads) == 2


async def test_an_upload_the_registry_would_not_open_or_the_hub_could_not_record_leaves_nothing(hub: Hub, caplog):
    client, store = await pusher(hub), store_of(hub)
    hub.upstream.refuse_writes = httpx.Response(500, text="boom")
    assert (await client.start()).status_code == 503
    assert store._uploads == {}, "the slot was given back"
    hub.upstream.refuse_writes = None

    # The registry opened a session and the Hub then lost its slot (or its database): cancelled there.
    attach = store.attach_upload
    store.attach_upload = lambda upload_id, **kwargs: False
    assert errors(await client.start())[:2] == (503, "UNAVAILABLE")
    assert hub.upstream.uploads == {}

    def broken(upload_id, **kwargs):
        raise CogCatalogUnavailableError("the database is away")

    store.attach_upload = broken
    assert (await client.start()).status_code == 503
    assert hub.upstream.uploads == {}

    # And if the registry will not cancel it either, where it is must not be lost: it is all that can
    # ever cancel it. It is kept as a dead session, and the cleanup gets it done later.
    store.attach_upload = lambda upload_id, **kwargs: False
    hub.upstream.fail[f"/v2/{REPO}/blobs/uploads/upstream-3"] = httpx.Response(500, text="boom")
    assert (await client.start()).status_code == 503
    assert list(hub.upstream.uploads) == ["upstream-3"]
    kept = [stored.session.upstream_location for stored in store._uploads.values()]
    assert [location for location in kept if location and "upstream-3" in location], "its location is remembered"
    store.attach_upload = attach
    hub.upstream.fail.clear()
    assert (await client.start()).status_code == 202
    assert list(hub.upstream.uploads) == ["upstream-4"], "cancelled by the next request's cleanup"

    # Cleaning up after other sessions never fails the request that does it.
    def failing(**kwargs):
        raise CogCatalogUnavailableError("the database is away")

    claim, store.claim_stale_uploads = store.claim_stale_uploads, failing
    with caplog.at_level(logging.WARNING, logger="frames_server.cogs.publishing"):
        assert (await client.start()).status_code == 202
    assert "cog_publish_stale_cleanup_failed" in [record.message for record in caplog.records]
    store.claim_stale_uploads = claim
    assert_no_backing_details(hub.responses)


async def test_an_upload_that_is_still_opening_is_not_discarded_by_the_cap_or_the_cleanup(hub: Hub, monkeypatch):
    """The registry is slow to open one session; meanwhile the same user opens enough to pass the cap."""

    monkeypatch.setattr(publish_store, "MAX_UPLOAD_SESSIONS_PER_USER", 2)
    client, store = await pusher(hub), store_of(hub)
    oci = hub.serving.publisher._source.oci()
    start_upload, arrived, proceed = oci.start_upload, asyncio.Event(), asyncio.Event()

    async def slow_once(repository):
        if not arrived.is_set():
            arrived.set()
            await proceed.wait()
        return await start_upload(repository)

    oci.start_upload = slow_once
    opening = asyncio.create_task(client.start())
    await arrived.wait()
    # Three more, each over the cap and each running the cleanup: the slot that is still opening stays.
    others = [await client.start() for _ in range(3)]
    assert [response.status_code for response in others] == [202, 202, 202]
    assert sum(stored.session.upstream_location is None for stored in store._uploads.values()) == 1
    proceed.set()
    opened = await opening
    assert opened.status_code == 202, "its slot was still there to take the registry's location"
    location = opened.headers["location"]
    assert (await hub.get(location, headers=client.headers)).status_code == 204
    patched = await hub.request("PATCH", location, headers=client.headers, content=b"usable")
    assert patched.status_code == 202
    # Every session the registry holds is one the Hub can account for.
    tracked = {stored.session.upstream_location.split("?")[0].rsplit("/", 1)[-1] for stored in store._uploads.values()}
    assert set(hub.upstream.uploads) <= tracked


async def test_a_cancel_the_registry_refuses_keeps_the_session_for_cleanup(hub: Hub):
    client, store = await pusher(hub), store_of(hub)
    location = (await client.start()).headers["location"]
    hub.upstream.fail[f"/v2/{REPO}/blobs/uploads/upstream-1"] = httpx.Response(500, text="boom")
    assert (await hub.request("DELETE", location, headers=client.headers)).status_code == 204
    assert (await hub.get(location, headers=client.headers)).status_code == 404, "gone for the client"
    assert len(store._uploads) == 1 and "upstream-1" in hub.upstream.uploads, "remembered for the cleanup"
    hub.upstream.fail.clear()
    assert (await client.start()).status_code == 202
    assert "upstream-1" not in hub.upstream.uploads and len(store._uploads) == 1


# -- one request at a time writes to a session ---------------------------------------------------


@pytest_asyncio.fixture
async def small_hub(make_hub) -> Hub:
    return await make_hub(
        publish=EVERYONE, serve={"enabled": True, "public_url": "https://hub.example", "max_blob_bytes": 1000}
    )


async def test_a_chunk_and_a_completion_sent_at_once_cannot_exceed_the_blob_limit(small_hub: Hub):
    """PATCH 600 bytes; while the registry is taking them, PUT 600 more with the digest of all 1,200."""

    hub = small_hub
    client = await pusher(hub)
    location = (await client.start()).headers["location"]
    first, second = b"a" * 600, b"b" * 600
    hub.upstream.upload_gate = asyncio.Event()
    patch = asyncio.create_task(hub.request("PATCH", location, headers=client.headers, content=first))
    async with asyncio.timeout(10):
        while not any(request.method == "PATCH" for request in hub.upstream.requests):
            await asyncio.sleep(0.01)  # until the chunk is at the registry; the Hub has not recorded it yet
    for method, kwargs in (
        ("PUT", {"params": {"digest": sha256(first + second)}, "content": second}),
        ("PATCH", {"content": second}),
        ("DELETE", {}),
    ):
        request = asyncio.create_task(hub.request(method, location, headers=client.headers, **kwargs))
        answered, _ = await asyncio.wait({request}, timeout=5)
        if not answered:
            hub.upstream.upload_gate.set()  # it was forwarded alongside the chunk, and is waiting at the registry
        status, code, message = errors(await request)
        assert (status, code) == (400, "BLOB_UPLOAD_INVALID") and "retry" in message, method
    assert (await hub.get(location, headers=client.headers)).status_code == 204, "reading the status is not a write"
    hub.upstream.upload_gate.set()
    assert (await patch).status_code == 202
    assert sha256(first + second) not in hub.upstream.blobs
    assert bytes(hub.upstream.uploads["upstream-1"]) == first, "only the holder's bytes reached the session"

    # The retry, now that the chunk is accounted for, meets the limit like any other.
    over = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(first + second)}, content=second
    )
    assert errors(over)[:2] == (413, "SIZE_INVALID")
    closed = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(first + b"c" * 400)}, content=b"c" * 400
    )
    assert closed.status_code == 201


TEN_BYTES = {"enabled": True, "public_url": "https://hub.example", "max_blob_bytes": 10}


async def lost_answer(hub: Hub, method: str):
    """Make the registry take the next ``method`` on an upload and lose its answer on the way back."""

    oci = hub.serving.publisher._source.oci()
    name = {"PATCH": "upload_chunk", "PUT": "finish_upload"}[method]
    forward = getattr(oci, name)

    async def forwarded_then_lost(*args, **kwargs):
        setattr(oci, name, forward)
        await forward(*args, **kwargs)
        raise OCITransportError(f"{name}: ReadError")

    setattr(oci, name, forwarded_then_lost)


async def test_a_chunk_whose_answer_was_lost_ends_the_upload_instead_of_being_forgotten(make_hub):
    """Ten-byte limit. The registry takes an 8-byte PATCH and its answer is lost; a PUT then brings 8 more."""

    hub = await make_hub(publish=EVERYONE, serve=TEN_BYTES)
    client, store = await pusher(hub), store_of(hub)
    location = (await client.start()).headers["location"]
    first, second = b"01234567", b"89abcdef"
    await lost_answer(hub, "PATCH")
    lost = await hub.request("PATCH", location, headers=client.headers, content=first)
    assert errors(lost)[:2] == (503, "UNAVAILABLE")
    # The Hub cannot say how many bytes the registry holds, so the upload is over: unknown to its client,
    # cancelled at the registry, and nothing can be added to it.
    assert store._uploads == {} and hub.upstream.uploads == {}
    closing = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(first + second)}, content=second
    )
    assert errors(closing)[:2] == (404, "BLOB_UPLOAD_UNKNOWN")
    assert sha256(first + second) not in hub.upstream.blobs, "sixteen bytes under a ten-byte limit"
    for method in ("GET", "PATCH", "DELETE"):
        gone = await hub.request(method, location, headers=client.headers, content=b"x" if method == "PATCH" else None)
        assert errors(gone)[:2] in ((404, "BLOB_UPLOAD_UNKNOWN"), (404, "")), method
    # Starting again is all it takes.
    assert (await client.blob(first)).status_code == 201


async def test_an_upload_is_retired_after_every_kind_of_write_whose_outcome_is_unknown(make_hub, caplog):
    hub = await make_hub(publish=EVERYONE, serve=TEN_BYTES)
    client, store = await pusher(hub), store_of(hub)
    serving = hub.serving

    async def unknown_afterwards(location: str, *, cancelled: bool = True) -> None:
        assert (await hub.get(location, headers=client.headers)).status_code == 404
        more = await hub.request("PATCH", location, headers=client.headers, content=b"89")
        assert errors(more)[:2] == (404, "BLOB_UPLOAD_UNKNOWN")
        if cancelled:
            assert hub.upstream.uploads == {}, "and cancelled at the registry"

    # The registry answers 5xx: it may have taken the bytes first.
    location = (await client.start()).headers["location"]
    hub.upstream.refuse_writes = httpx.Response(500, text="boom")
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"01234567")).status_code == 503
    hub.upstream.refuse_writes = None
    # (The registry would not take the cancellation either, just then: the record is kept, dead, for the cleanup.)
    await unknown_afterwards(location, cancelled=False)
    assert list(hub.upstream.uploads) == ["upstream-1"] and len(store._uploads) == 1

    # The request runs out of time while the registry is taking the chunk.
    location = (await client.start()).headers["location"]
    assert list(hub.upstream.uploads) == ["upstream-2"], "the cleanup got the first one cancelled"
    hub.app.state.cog_registry_serving = replace(serving, max_blob_seconds=0.1, max_metadata_seconds=0.1)
    hub.upstream.upload_delay = 0.5
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"01234567")).status_code == 503
    hub.app.state.cog_registry_serving = serving
    await asyncio.sleep(0.6)  # the registry finishes taking the chunk it was sent
    hub.upstream.upload_delay = 0.0
    await unknown_afterwards(location)

    # The registry took the chunk, and the Hub could not record that it had.
    location = (await client.start()).headers["location"]
    advance = store.advance_upload

    def down(*args, **kwargs):
        raise CogCatalogUnavailableError("the database is away")

    store.advance_upload = down
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"01234567")).status_code == 503
    store.advance_upload = advance
    await unknown_afterwards(location)

    # The closing PUT's answer is lost: the blob may be stored, the session is over either way.
    location = (await client.start()).headers["location"]
    await lost_answer(hub, "PUT")
    closing = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(b"0123")}, content=b"0123"
    )
    assert closing.status_code == 503
    assert (await hub.get(location, headers=client.headers)).status_code == 404

    # Nothing can be recorded at all -- not even that the session is dead. The lease is never given back, and
    # a lease that runs out is not passed on: the session dies with it.
    now = [datetime.now(UTC)]
    store.clock = lambda: now[0]
    location = (await client.start()).headers["location"]
    upload_id = location.rsplit("/", 1)[-1]
    close, retire = store.close_upload, store.retire_upload
    store.advance_upload = store.close_upload = store.retire_upload = down
    with caplog.at_level(logging.WARNING, logger="frames_server.cogs.publishing"):
        assert (await hub.request("PATCH", location, headers=client.headers, content=b"01234567")).status_code == 503
    assert "cog_publish_bookkeeping_failed" in [record.message for record in caplog.records]
    store.advance_upload, store.close_upload, store.retire_upload = advance, close, retire
    assert store._uploads[upload_id].leased_until is not None, "still held: never released with a stale count"
    busy = await hub.request("PATCH", location, headers=client.headers, content=b"89")
    assert errors(busy)[:2] == (400, "BLOB_UPLOAD_INVALID")
    now[0] += timedelta(seconds=hub.serving.publisher._lease_seconds + 1)
    over = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(b"0123456789abcdef")}, content=b"89abcdef"
    )
    assert errors(over)[:2] == (404, "BLOB_UPLOAD_UNKNOWN")
    assert sha256(b"0123456789abcdef") not in hub.upstream.blobs


async def test_a_cancellation_that_times_out_or_cannot_be_recorded_does_not_leave_the_upload_writable(hub: Hub):
    """Open, DELETE runs out of time at the registry, then PATCH: the session must not take the chunk."""

    client, store = await pusher(hub), store_of(hub)
    oci, serving = hub.serving.publisher._source.oci(), hub.serving
    location = (await client.start()).headers["location"]
    cancel_upload = oci.cancel_upload

    async def slow_once(repository, upstream_location):
        oci.cancel_upload = cancel_upload
        await asyncio.sleep(5)

    oci.cancel_upload = slow_once
    hub.app.state.cog_registry_serving = replace(serving, max_metadata_seconds=0.1)
    assert (await hub.request("DELETE", location, headers=client.headers)).status_code == 503
    hub.app.state.cog_registry_serving = serving
    patched = await hub.request("PATCH", location, headers=client.headers, content=b"after the cancel")
    assert errors(patched)[:2] == (404, "BLOB_UPLOAD_UNKNOWN")
    assert not any(bytes(received) for received in hub.upstream.uploads.values()), "the registry took nothing more"
    assert store._uploads == {} and hub.upstream.uploads == {}, "retired, and cancelled when the registry answered"

    # The registry cancelled it and the Hub could not record that: held, never handed back as usable.
    location = (await client.start()).headers["location"]
    close, retire = store.close_upload, store.retire_upload

    def down(*args, **kwargs):
        raise CogCatalogUnavailableError("the database is away")

    store.close_upload = store.retire_upload = down
    assert (await hub.request("DELETE", location, headers=client.headers)).status_code == 503
    store.close_upload, store.retire_upload = close, retire
    patched = await hub.request("PATCH", location, headers=client.headers, content=b"after the cancel")
    assert patched.status_code in (400, 404) and patched.status_code != 202
    now = [datetime.now(UTC) + timedelta(seconds=hub.serving.publisher._lease_seconds + 1)]
    store.clock = lambda: now[0]
    late = await hub.request("PATCH", location, headers=client.headers, content=b"after the lease")
    assert errors(late)[:2] == (404, "BLOB_UPLOAD_UNKNOWN")


async def test_a_write_the_registry_definitely_refused_gives_the_session_back_unchanged(hub: Hub, caplog):
    client, store = await pusher(hub), store_of(hub)
    location = (await client.start()).headers["location"]
    upload_id = location.rsplit("/", 1)[-1]
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"01234")).status_code == 202

    def leased() -> bool:
        return store._uploads[upload_id].leased_until is not None

    # The registry refuses the Hub's credential, or the chunk itself: it took nothing, and says so.
    for refusal, status in ((httpx.Response(403, text="no"), 503), (httpx.Response(416, text="no"), 416)):
        hub.upstream.refuse_writes = refusal
        refused = await hub.request("PATCH", location, headers=client.headers, content=b"x")
        assert refused.status_code == status and not leased()
        if status == 503:
            closing = await hub.request("PUT", location, headers=client.headers, params={"digest": sha256(b"01234")})
            assert closing.status_code == 503 and not leased()
    hub.upstream.refuse_writes = None
    assert (await hub.get(location, headers=client.headers)).headers["range"] == "0-4", "where it was"
    # Giving the lease back is bookkeeping: if it fails, the answer is still the answer, and the failure is logged.
    release = store.release_upload

    def failing(upload_id, *, lease):
        raise CogCatalogUnavailableError("the database is away")

    store.release_upload = failing
    hub.upstream.refuse_writes = httpx.Response(403, text="no")
    with caplog.at_level(logging.WARNING, logger="frames_server.cogs.publishing"):
        assert (await hub.request("PATCH", location, headers=client.headers, content=b"x")).status_code == 503
    assert "cog_publish_bookkeeping_failed" in [record.message for record in caplog.records]
    store.release_upload = release
    hub.upstream.refuse_writes = None
    store._uploads[upload_id].leased_until = None
    closed = await hub.request(
        "PUT", location, headers=client.headers, params={"digest": sha256(b"0123456789")}, content=b"56789"
    )
    assert closed.status_code == 201 and hub.upstream.blobs[sha256(b"0123456789")] == b"0123456789"

    # The registry no longer knows a session: unknown here too, and forgotten without asking it to cancel.
    for method in ("PATCH", "PUT"):
        location = (await client.start()).headers["location"]
        hub.upstream.uploads.clear()
        writes = len(hub.upstream.writes())
        lost = await hub.request(
            method, location, headers=client.headers, params={"digest": sha256(b"x")}, content=b"x"
        )
        assert errors(lost)[:2] == (404, "BLOB_UPLOAD_UNKNOWN"), method
        assert location.rsplit("/", 1)[-1] not in store._uploads
        assert [write.split()[0] for write in hub.upstream.writes()[writes:]] == [method]


async def test_an_opening_cut_off_by_its_deadline_gives_the_slot_back(hub: Hub):
    client, store = await pusher(hub), store_of(hub)
    oci = hub.serving.publisher._source.oci()

    async def never(repository):
        await asyncio.sleep(5)

    oci.start_upload = never
    hub.app.state.cog_registry_serving = replace(hub.serving, max_blob_seconds=0.1, max_metadata_seconds=0.1)
    assert (await client.start()).status_code == 503
    assert store._uploads == {}


async def test_the_lease_outlasts_the_longest_request_that_can_hold_it(make_hub):
    hub = await make_hub(
        publish=EVERYONE, serve={"enabled": True, "public_url": "https://hub.example", "max_blob_seconds": 120}
    )
    assert hub.serving.publisher._lease_seconds == 120 + publishing.LEASE_MARGIN_SECONDS
    # A sweep is not sent to a pending repository while the publish that will settle it can still be running.
    assert publish_store.PENDING_ENUMERATION_GRACE_SECONDS > 2 * hub.serving.max_metadata_seconds


# -- Content-Range --------------------------------------------------------------------------------


async def chunked(data: bytes):
    """A body with no declared length."""

    for offset in range(0, len(data), 4):
        yield data[offset : offset + 4]


async def test_a_content_range_is_checked_at_both_ends_and_against_the_bytes(hub: Hub):
    client = await pusher(hub)
    location = (await client.start()).headers["location"]

    async def patch(content_range: str, content) -> httpx.Response:
        return await hub.request(
            "PATCH", location, headers={**client.headers, "Content-Range": content_range}, content=content
        )

    writes = len(hub.upstream.writes())
    for content_range, body, why in (
        ("0-0", b"0123456789", "does not match the number of bytes"),
        ("0-99", b"0123456789", "does not match the number of bytes"),
        ("9-0", b"0123456789", "ends before it starts"),
        ("0-", b"0123456789", "malformed"),
        ("bytes=0-9", b"0123456789", "malformed"),
    ):
        refused = await patch(content_range, body)
        status, code, message = errors(refused)
        assert (status, code) == (400, "BLOB_UPLOAD_INVALID") and why in message, content_range
    assert len(hub.upstream.writes()) == writes, "none of those was forwarded"
    assert (await hub.get(location, headers=client.headers)).headers["range"] == "0-0", "the session has not moved"
    assert (await patch("0-9", b"0123456789")).status_code == 202
    assert (await patch("bytes 10-13", chunked(b"abcd"))).status_code == 202, "a body of no declared length, in step"

    # Closing with an offset: it must be where the upload is, and as long as the bytes sent.
    async def close(content_range: str, content, digest_of: bytes) -> httpx.Response:
        return await hub.request(
            "PUT",
            location,
            headers={**client.headers, "Content-Range": content_range},
            params={"digest": sha256(digest_of)},
            content=content,
        )

    whole = b"0123456789abcdXYZ"
    wrong_offset = await close("0-2", b"XYZ", whole)
    assert wrong_offset.status_code == 416 and wrong_offset.headers["range"] == "0-13"
    assert errors(await close("14-20", b"XYZ", whole))[:2] == (400, "BLOB_UPLOAD_INVALID")
    assert errors(await close("14-16", None, whole))[:2] == (400, "BLOB_UPLOAD_INVALID"), "a range with no bytes"
    assert (await close("14-16", b"XYZ", whole)).status_code == 201
    assert hub.upstream.blobs[sha256(whole)] == whole

    # The whole blob in the opening request states its range from zero.
    opening = await hub.request(
        "POST",
        f"/v2/{REPO}/blobs/uploads/",
        headers={**client.headers, "Content-Range": "5-9"},
        params={"digest": sha256(b"01234")},
        content=b"01234",
    )
    assert opening.status_code == 416 and hub.upstream.uploads == {}


async def test_a_streamed_body_that_is_not_the_length_its_range_states_ends_the_upload(hub: Hub):
    """With no Content-Length the mismatch is only known as the bytes pass: the session cannot be trusted after."""

    client = await pusher(hub)
    for body, content_range in ((b"0123456789ab", "0-7"), (b"0123", "0-7")):
        location = (await client.start()).headers["location"]
        refused = await hub.request(
            "PATCH", location, headers={**client.headers, "Content-Range": content_range}, content=chunked(body)
        )
        assert errors(refused)[:2] == (400, "BLOB_UPLOAD_INVALID"), content_range
        assert (await hub.get(location, headers=client.headers)).status_code == 404
        assert hub.upstream.uploads == {}, "cancelled at the registry too"
    location = (await client.start()).headers["location"]
    closing = await hub.request(
        "PUT",
        location,
        headers={**client.headers, "Content-Range": "0-7"},
        params={"digest": sha256(b"0123")},
        content=chunked(b"0123"),
    )
    assert errors(closing)[:2] == (400, "BLOB_UPLOAD_INVALID") and sha256(b"0123") not in hub.upstream.blobs


# -- a sweep racing a publish ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def sweeping_hub(tmp_path, monkeypatch):
    """A Hub that publishes and indexes, whose publish source has no configured repository list."""

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
            yield Hub(app, http, upstream)


def gone_from(upstream: FakeRegistry, bundle, *tags: str) -> None:
    for key in [key for key in upstream.manifests if key[1] in (bundle.digest, *tags)]:
        del upstream.manifests[key]


async def test_a_version_published_again_during_a_sweep_is_not_tombstoned_by_it(sweeping_hub: Hub):
    """The digest was in the catalog before the sweep (removed); it is republished after the registry was listed."""

    hub, indexer = sweeping_hub, sweeping_hub.app.state.cog_indexer
    client = await pusher(hub)
    older = CogBundle(extra=b"published, deleted, and published again")
    assert (await client.bundle(older, "0.9.0")).status_code == 201
    assert (await client.bundle(COG, "1.0.0")).status_code == 201
    gone_from(hub.upstream, older, "0.9.0")
    assert (await indexer.sweep()).removed == 1
    assert hub.catalog.get(older.digest).removed_at is not None

    enumerate_source = indexer._enumerate

    async def enumerate_then_publish(source):
        listing = await enumerate_source(source)  # the registry without it...
        assert (await client.manifest(older, "0.9.0")).status_code == 201  # ...and then it is published again
        return listing

    indexer._enumerate = enumerate_then_publish
    summary = await indexer.sweep()
    assert summary.errors == [] and summary.removed == 0
    row = hub.catalog.get(older.digest)
    assert row.removed_at is None and row.tags == ("0.9.0",), "a successful publish is not undone by a stale listing"
    assert (await hub.get(f"/v2/{REPO}/manifests/0.9.0", headers=client.headers)).status_code == 200

    # The condition is the removal's own: a publish that lands after everything else the sweep does is safe too.
    newest = CogBundle(extra=b"lands between the sweep's last read and its removal")
    await upload(client, newest)
    indexer._enumerate = enumerate_source
    mark_removed = hub.catalog.mark_removed

    def publish_then_remove(source_id, present, **kwargs):
        published = asyncio.run_coroutine_threadsafe(client.manifest(newest, "2.0.0"), loop).result(10)
        assert published.status_code == 201
        return mark_removed(source_id, present, **kwargs)

    loop = asyncio.get_running_loop()
    hub.catalog.mark_removed = publish_then_remove
    assert (await indexer.sweep()).removed == 0
    hub.catalog.mark_removed = mark_removed
    assert hub.catalog.get(newest.digest).removed_at is None

    # What really is gone is still removed -- by the next sweep, whose listing can speak for it.
    gone_from(hub.upstream, older, "0.9.0")
    assert (await indexer.sweep()).removed == 1
    assert hub.catalog.get(older.digest).removed_at is not None and hub.catalog.get(COG.digest).removed_at is None


async def test_a_sweep_is_never_sent_to_a_repository_whose_publish_was_refused(sweeping_hub: Hub):
    hub, store = sweeping_hub, store_of(sweeping_hub)
    now = [datetime.now(UTC)]
    store.clock = lambda: now[0]
    client = await pusher(hub, ALICE, OTHER)
    await upload(client, COG, OTHER)
    hub.upstream.fail[f"/v2/{OTHER}/manifests/1.0.0"] = httpx.Response(400, text="no")
    assert (await client.manifest(COG, "1.0.0", OTHER)).status_code == 400
    now[0] += FOREVER
    asked = len(hub.upstream.requests)
    summary = await hub.app.state.cog_indexer.sweep()
    assert summary.errors == [] and not hub.catalog.repository_known(OTHER)
    assert not any(OTHER in request.url.path for request in hub.upstream.requests[asked:])


# -- content that was not published through the Hub is nobody's to claim -------------------------


def out_of_band(hub: Hub, repo: str, tag: str = "1.0.0") -> CogBundle:
    """A bundle pushed to the backing registry directly, behind the Hub's back."""

    bundle = CogBundle(extra=b"pushed to the registry directly")
    hub.upstream.publish_raw(repo, MEDIA_TYPE_OCI_MANIFEST, bundle.manifest, tag)
    hub.upstream.blobs.update(bundle.blobs)
    return bundle


async def test_a_repository_the_registry_already_holds_cannot_be_claimed_before_it_is_indexed(sweeping_hub: Hub):
    """Pushed to the registry directly, and no sweep has seen it yet: the catalog knows nothing about it."""

    hub, store = sweeping_hub, store_of(sweeping_hub)
    theirs = out_of_band(hub, REPO)
    assert not hub.catalog.repository_known(REPO)
    alice = await pusher(hub, ALICE)
    # Nothing of the publisher's reaches the repository: not an upload, not a blob check, not a manifest --
    # and the manifest is refused before its bundle is read back from someone else's repository.
    status, code, message = errors(await alice.start())
    assert (status, code) == (403, "DENIED") and "not published through this Hub" in message
    layer = next(iter(theirs.blobs.values()))
    held = await hub.request("HEAD", f"/v2/{REPO}/blobs/{sha256(layer)}", headers=alice.headers)
    assert held.status_code == 404, "a blob check says nothing about a repository the caller may not publish to"
    refused = await alice.manifest(COG, "1.0.0")
    status, code, message = errors(refused)
    assert (status, code) == (403, "DENIED") and "not published through this Hub" in message
    assert hub.upstream.writes() == []
    assert not [r for r in hub.upstream.requests if r.url.path.startswith(f"/v2/{REPO}/blobs/")]
    # Its tag still points at what was there, nothing was forwarded, and the name is nobody's.
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == theirs.manifest and manifest_puts(hub) == []
    assert store._repositories == {} and hub.catalog._attempts == {}
    # One request to the registry decided each, and it asked for one tag.
    asked = [request for request in hub.upstream.requests if request.url.path == f"/v2/{REPO}/tags/list"]
    assert [str(request.url.query, "ascii") for request in asked[-1:]] == ["n=1"]

    # A tag listing that does not say what the repository holds is not "nothing": 200 with an empty object.
    hub.upstream.fail[f"/v2/{REPO}/tags/list"] = httpx.Response(200, json={})
    assert errors(await alice.manifest(COG, "1.0.0"))[:2] == (503, "UNAVAILABLE")
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == theirs.manifest and manifest_puts(hub) == []
    assert store._repositories == {} and hub.catalog._attempts == {}
    del hub.upstream.fail[f"/v2/{REPO}/tags/list"]

    # When the registry cannot say, the answer is not "new": nothing is reserved and nothing forwarded.
    hub.upstream.fail[f"/v2/{OTHER}/tags/list"] = httpx.Response(500, text="boom")
    elsewhere = await pusher(hub, ALICE, OTHER)
    assert errors(await elsewhere.start(OTHER))[:2] == (503, "UNAVAILABLE")
    assert errors(await elsewhere.manifest(COG, "1.0.0", OTHER))[:2] == (503, "UNAVAILABLE")
    assert store._repositories == {} and manifest_puts(hub) == [] and hub.upstream.writes() == []
    # A repository the registry does not have, or has with nothing in it, is new.
    del hub.upstream.fail[f"/v2/{OTHER}/tags/list"]
    await upload(elsewhere, COG, OTHER)
    assert (await elsewhere.manifest(COG, "1.0.0", OTHER)).status_code == 201
    # Once it is the organization's, publishing to it asks the registry nothing of the kind.
    asked = len([r for r in hub.upstream.requests if r.url.path.endswith("/tags/list")])
    assert (await elsewhere.manifest(COG, "1.0.1", OTHER)).status_code == 201
    assert len([r for r in hub.upstream.requests if r.url.path.endswith("/tags/list")]) == asked

    # No sweep is sent there either (nobody published it through the Hub), so the catalog never learns of
    # it; the registry is what keeps answering, each time.
    assert (await hub.app.state.cog_indexer.sweep()).errors == []
    assert store.get_repository(REPO) is None and not hub.catalog.repository_known(REPO)
    assert (await alice.manifest(COG, "1.0.0")).status_code == 403
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == theirs.manifest


async def test_content_found_in_a_pending_repository_settles_it_only_if_its_organization_published_it(
    sweeping_hub: Hub, caplog
):
    """A's manifest gets a 500 and never lands; somebody pushes unrelated content there directly; a sweep runs."""

    hub, store = sweeping_hub, store_of(sweeping_hub)
    now = [datetime.now(UTC)]
    store.clock = lambda: now[0]
    alice = await pusher(hub, ALICE)
    await upload(alice, COG)
    hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    assert (await alice.manifest(COG, "1.0.0")).status_code == 503
    del hub.upstream.fail[f"/v2/{REPO}/manifests/1.0.0"]
    theirs = out_of_band(hub, REPO)

    now[0] += FOREVER
    with caplog.at_level(logging.WARNING, logger="frames_server.cogs.publishing"):
        assert (await hub.app.state.cog_indexer.sweep()).errors == []
    # What was found is listed as out-of-band content, with no publisher; and it is not proof of ownership.
    row = hub.catalog.get(theirs.digest)
    assert (row.status, row.published_by) == (STATUS_INDEXED, None)
    record = store.get_repository(REPO)
    assert (record.owner_org_id, record.committed) == ("org-a", False), "still pending: not settled by that"
    assert "cog_publish_pending_repository_holds_other_content" in [record.message for record in caplog.records]
    # So the organization cannot write over it: its retry is refused, and nothing reaches the registry.
    retry = await alice.manifest(COG, "1.0.0")
    assert errors(retry)[:2] == (403, "DENIED") and "not published through this Hub" in errors(retry)[2]
    assert hub.upstream.manifests[(REPO, "1.0.0")][1] == theirs.manifest and manifest_puts(hub) == [
        f"PUT /v2/{REPO}/manifests/1.0.0"  # the one that got the 500
    ]
    assert (await hub.app.state.cog_indexer.sweep()).errors == []
    assert store.get_repository(REPO).committed is False


async def test_a_platform_operator_may_publish_to_a_repository_the_registry_already_holds(membership_hub):
    hub = membership_hub
    theirs = out_of_band(hub, REPO, "0.1.0")
    owner = await pusher(hub, member_token(OWNER))
    assert (await owner.start()).status_code == 403
    assert (await owner.manifest(COG, "1.0.0")).status_code == 403
    operator = await pusher(hub, member_token(OPERATOR))
    asked = len(hub.upstream.requests)
    await upload(operator, COG)
    assert (await operator.manifest(COG, "1.0.0")).status_code == 201
    assert hub.upstream.manifests[(REPO, "0.1.0")][1] == theirs.manifest
    assert not any(request.url.path.endswith("/tags/list") for request in hub.upstream.requests[asked:])


async def test_an_organization_cannot_hold_more_pending_names_than_the_limit(make_hub):
    """The registry cannot take manifests; a permitted publisher keeps publishing under new names."""

    hub = await make_hub(publish={**EVERYONE, "max_pending_repositories": 2})
    store = store_of(hub)
    names = ["cogs/one", "cogs/two", "cogs/three"]
    alice = await pusher(hub, ALICE, *names)
    hub.upstream.refuse_writes = None
    for name in names:
        await upload(alice, COG, name)
        hub.upstream.fail[f"/v2/{name}/manifests/1.0.0"] = httpx.Response(500, text="boom")
    for name in names[:2]:
        assert (await alice.manifest(COG, "1.0.0", name)).status_code == 503
    forwarded = len(manifest_puts(hub))
    refused = await alice.manifest(COG, "1.0.0", names[2])
    status, code, message = errors(refused)
    assert (status, code) == (429, "TOOMANYREQUESTS") and "already has 2 repositories" in message
    assert len(manifest_puts(hub)) == forwarded and store.get_repository(names[2]) is None
    assert sorted(store._repositories) == names[:2]
    # Another organization has its own allowance; and a retry of a pending name is never what is refused.
    bob = await pusher(hub, BOB, "cogs/bobs")
    await upload(bob, COG, "cogs/bobs")
    assert (await bob.manifest(COG, "1.0.0", "cogs/bobs")).status_code == 201
    assert (await alice.manifest(COG, "1.0.0", names[0])).status_code == 503
    hub.upstream.fail.clear()
    assert (await alice.manifest(COG, "1.0.0", names[0])).status_code == 201
    # Settled, it no longer counts: there is room for the third.
    assert (await alice.manifest(COG, "1.0.0", names[2])).status_code == 201


class _HarborLike:
    """A source whose listing API says "no such repository" its own way, as Harbor's does."""

    id, host = "backing", "registry.example"

    def __init__(self, listed: list[str]) -> None:
        self.listed = listed

    async def list_repositories(self) -> list[str]:
        return list(self.listed)

    async def list_artifacts(self, repository: str):
        raise RegistryRepositoryNotFound(f"source 'backing': {repository} does not exist on the registry (HTTP 404)")


async def test_a_published_repository_the_registry_does_not_have_enumerates_as_empty_on_either_adapter():
    """Sent there only because of a publish that never landed: nothing there, which is not a failed sweep."""

    from collab_hub_api.cogs.catalog import InMemoryCogCatalogStore
    from collab_hub_api.cogs.indexer import CogIndexer

    found: list[tuple[str, list[str]]] = []
    indexer = CogIndexer(
        InMemoryCogCatalogStore(),
        [_HarborLike(listed=[])],
        published_repositories=lambda source_id: ["cogs/never-landed"],
        repositories_found=lambda source_id, present: found.append((source_id, dict(present))),
    )
    try:
        summary = await indexer.sweep()
    finally:
        indexer.close()
    assert (summary.errors, summary.sources_failed) == ([], 0)
    assert found == [("backing", {"cogs/never-landed": []})], "and nothing was found there to settle it by"

    # A repository the source itself lists is another matter: that it then does not exist is an error, as before.
    indexer = CogIndexer(
        InMemoryCogCatalogStore(), [_HarborLike(listed=["cogs/listed"])], published_repositories=lambda source_id: []
    )
    try:
        summary = await indexer.sweep()
    finally:
        indexer.close()
    assert summary.sources_failed == 1 and "list_artifacts cogs/listed" in summary.errors[0]


# -- what the Hub's HTTP client logs about a write ------------------------------------------------


async def test_writes_to_the_registry_are_logged_by_operation_and_source_never_by_url(hub: Hub, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    client = await pusher(hub)
    assert (await client.bundle(COG, "1.0.0", chunks=2)).status_code == 201
    location = (await client.start()).headers["location"]
    assert (await hub.request("DELETE", location, headers=client.headers)).status_code == 204

    lines = [record.getMessage() for record in caplog.records if record.name == "httpx"]
    upstream = [line for line in lines if "http://test" not in line]  # the rest are this test's own client
    writes = [line for line in upstream if "[registry write: " in line]
    operations = {line.split("[registry write: ")[1].split(",")[0] for line in writes}
    assert operations == {"upload start", "upload chunk", "upload finish", "upload cancel", "manifest put"}
    assert all("source backing]" in line for line in writes)
    assert 'HTTP Request: PATCH [registry write: upload chunk, source backing] "HTTP/1.1 202 Accepted"' in writes
    for line in upstream:
        # No session URL in any form: not its path (which may be the capability), not its state, not its host.
        assert "/blobs/uploads" not in line and "upstream-" not in line and UPLOAD_STATE not in line, line
        if line.split()[2] in ("POST", "PATCH", "PUT", "DELETE"):
            assert BACKING_HOST not in line and "[registry write: " in line, line
    # Reads are logged as they always were: the path says which blob.
    read = f"HTTP Request: GET https://{BACKING_HOST}/v2/{REPO}/blobs/sha256:"
    assert any(line.startswith(read) for line in upstream)


# -- with no publish source, nothing about this surface exists -------------------------------------


async def test_the_matrix_on_a_hub_that_accepts_no_publishes(make_hub):
    hub = await make_hub()
    token = await hub.pull_token(REPO)
    for method, path, body in PUSHES:
        if method == "GET":
            continue
        for headers in ({}, token):
            response = await hub.request(method, path.format(repo=REPO, upload="up-1"), headers=headers, content=body)
            assert errors(response) == (405, "UNSUPPORTED", "this registry is read-only"), (method, path)
    assert hub.upstream.requests == []


# -- review follow-ups (#182): untagged manifests, cut-off reservations, registry pushback ----------------


async def test_a_manifest_put_by_digest_stays_listed_on_a_source_that_finds_manifests_by_tag(sweeping_hub: Hub):
    """The static adapter cannot list a manifest with no tag; the sweep asks the registry about it instead."""

    hub = sweeping_hub
    client = await pusher(hub)
    await upload(client, COG)
    assert (await client.manifest(COG, COG.digest)).status_code == 201

    def row():
        return hub.catalog.get(COG.digest, source_id="backing", repository=REPO)

    assert row().tags == () and row().removed_at is None
    # Written before the sweep's clock reading, so only the registry's answer keeps it.
    assert (await hub.app.state.cog_indexer.sweep()).removed == 0
    assert row().removed_at is None
    asked = [r for r in hub.upstream.requests if r.method == "HEAD" and r.url.path.endswith(f"/manifests/{COG.digest}")]
    assert len(asked) == 1, "one HEAD for the one untagged row"
    # Gone from the registry, it is gone from the catalog at the next sweep.
    gone_from(hub.upstream, COG)
    assert (await hub.app.state.cog_indexer.sweep()).removed == 1
    assert row().removed_at is not None


async def test_a_reservation_that_lands_after_the_deadline_is_taken_back_and_holds_nothing(hub: Hub):
    """A thread is not interrupted by the deadline: the request waits for the reservation, then undoes it."""

    client, store = await pusher(hub), store_of(hub)
    await upload(client, COG)
    reserve, serving = store.reserve_repository, hub.serving

    def late(*args, **kwargs):
        time.sleep(0.8)  # past the request's budget, and only then recorded
        return reserve(*args, **kwargs)

    store.reserve_repository = late
    hub.app.state.cog_registry_serving = replace(serving, max_metadata_seconds=0.4)
    assert errors(await client.manifest(COG, "1.0.0"))[:2] == (503, "UNAVAILABLE")
    await asyncio.sleep(1.0)  # any reservation still running has landed by now
    assert store.get_repository(REPO) is None, "nothing was forwarded, and the name is nobody's"
    assert manifest_puts(hub) == [] and hub.catalog._attempts == {}
    store.reserve_repository, hub.app.state.cog_registry_serving = reserve, serving
    assert (await client.manifest(COG, "1.0.0")).status_code == 201


async def test_a_registry_that_says_try_later_or_too_large_is_not_reported_as_the_clients_mistake(hub: Hub):
    """429 and 408 mean "try again"; 413 is the registry's own size limit. None is a bad digest or a bad chunk."""

    client, store = await pusher(hub), store_of(hub)
    location = (await client.start()).headers["location"]
    upload_id = location.rsplit("/", 1)[-1]
    assert (await hub.request("PATCH", location, headers=client.headers, content=b"01234")).status_code == 202
    whole = sha256(b"0123456789")

    # Rate limited: the registry took nothing, so the session is given back as it was, for the same request again.
    hub.upstream.refuse_writes = httpx.Response(429, text="slow down")
    for method, request in (("PATCH", {}), ("PUT", {"params": {"digest": whole}})):
        limited = await hub.request(method, location, headers=client.headers, content=b"56789", **request)
        assert errors(limited)[:2] == (503, "UNAVAILABLE"), method
        assert store._uploads[upload_id].leased_until is None, method
    hub.upstream.refuse_writes = None
    assert (await hub.get(location, headers=client.headers)).headers["range"] == "0-4", "where it was"
    closed = await hub.request("PUT", location, headers=client.headers, params={"digest": whole}, content=b"56789")
    assert closed.status_code == 201 and hub.upstream.blobs[whole] == b"0123456789"

    async def refused_with(status: int, method: str) -> tuple[tuple[int, str], str]:
        location = (await client.start()).headers["location"]
        hub.upstream.refuse_writes = httpx.Response(status, text="no")
        try:
            response = await hub.request(
                method, location, headers=client.headers, params={"digest": sha256(b"x")}, content=b"x"
            )
        finally:
            hub.upstream.refuse_writes = None
        return errors(response)[:2], location

    for method in ("PATCH", "PUT"):
        # Timed out at the registry: what it took is not known, so the upload ends -- still not the client's fault.
        assert (await refused_with(408, method))[0] == (503, "UNAVAILABLE"), method
        # Over the registry's own limit: no retry will fit, and it is said as a size, not as a bad chunk or digest.
        status, location = await refused_with(413, method)
        assert status == (413, "SIZE_INVALID"), method
        assert (await hub.get(location, headers=client.headers)).status_code == 404, "ended"
    # Closing: a 400 is the digest check failing; any other refusal is not reported as one.
    assert (await refused_with(400, "PUT"))[0] == (400, "DIGEST_INVALID")
    assert (await refused_with(409, "PUT"))[0] == (400, "BLOB_UPLOAD_INVALID")

    # A manifest: rate limited is a definite no, so the attempt and the name are let go of.
    await upload(client, COG)
    hub.upstream.refuse_writes = httpx.Response(429, text="slow down")
    assert errors(await client.manifest(COG, "1.0.0"))[:2] == (503, "UNAVAILABLE")
    assert store.get_repository(REPO) is None and hub.catalog._attempts == {}
    hub.upstream.refuse_writes = httpx.Response(413, text="too big")
    status, _code, message = errors(await client.manifest(COG, "1.0.0"))
    assert (status, _code) == (400, "MANIFEST_INVALID") and "too large" in message
    assert store.get_repository(REPO) is None
    # Timed out: the registry may have it, so the name stays this organization's, pending.
    hub.upstream.refuse_writes = httpx.Response(408, text="slow")
    assert errors(await client.manifest(COG, "1.0.0"))[:2] == (503, "UNAVAILABLE")
    assert store.get_repository(REPO).committed is False
    hub.upstream.refuse_writes = None
    assert (await client.manifest(COG, "1.0.0")).status_code == 201
