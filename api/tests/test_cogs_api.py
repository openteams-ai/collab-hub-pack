"""The Cog catalog read API (issue #85), against a seeded in-memory catalog.

Cards are the bundle reader's real output for the sanitized fixtures under
``fixtures/cogs/`` (so the response model is proven against what the indexer
actually stores), plus small synthetic cards where a test needs one knob.
The listing semantics are also pinned store-side, against both backends, in
``test_cog_catalog.py``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from test_cog_bundle import read_fixture
from test_frames_auth_jwks import KEY_1_JWK, KEY_1_PEM, _JWKSEndpoint

from collab_hub_api.cogs.adapters.static import parse_index_document
from collab_hub_api.cogs.bundle import CogCard, read_cog_bundle
from collab_hub_api.cogs.catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
    InMemoryCogCatalogStore,
    UnavailableCogCatalogStore,
    card_search_fields,
)
from collab_hub_api.cogs.models import ANONYMOUS_CARD_OMITTED_KEYS, CARD_SCHEMA, LIST_CARD_OMITTED_KEYS, list_card
from collab_hub_api.config import Config, recommended_path_rules
from collab_hub_api.core import make_app
from collab_hub_api.frames import auth
from collab_hub_api.frames.auth import NoOrganizationError
from collab_hub_api.routers import cogs as cogs_router

HOST = "registry.example"
SOURCE = "main"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

TRANSCRIBER = "example/cog-audio-transcriber"
NOTES = "example/cog-meeting-notes"
MODEL = "example/cog-small-model"


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def fixture_card(fixture: str, **overrides) -> dict:
    document = read_fixture(fixture).to_dict()
    document.update(overrides)
    return document


def row(
    seed: str,
    document: dict | None,
    *,
    repository: str,
    source_id: str = SOURCE,
    host: str = HOST,
    pushed_at: datetime | None = T0,
    tags: tuple[str, ...] = ("latest",),
    status: str = STATUS_INDEXED,
) -> CogArtifact:
    return CogArtifact(
        source_id=source_id,
        host=host,
        repository=repository,
        digest=digest(seed),
        status=status,
        tags=tags,
        pushed_at=pushed_at,
        card=document,
        **(card_search_fields(document) if document else {}),
    )


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":")).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


AUTH = {"IdToken-test": _jwt({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a"})}


def values(tmp_path, *, security: dict | None = None, catalog_backend: str = "memory") -> dict:
    result: dict = {
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"mcp_session_manager_enabled": False},
        "tasks": {"backend": "memory"},
        "cogs": {"catalog": {"backend": catalog_backend}},
    }
    if security is not None:
        result["security"] = security
    return result


async def _client(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(Config.parse(values(tmp_path, **kwargs)))
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=AUTH)


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    app, client = await _client(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app), client:
        store = app.state.cog_catalog_store
        assert isinstance(store, InMemoryCogCatalogStore)
        yield client, store


def seed_catalog(store: InMemoryCogCatalogStore) -> None:
    """Three Cogs; the transcriber has two versions, a mirror copy, and a removed third."""

    store.upsert(row("1", fixture_card("pixi-complete"), repository="cogs/cog-audio-transcriber-1a2b"))
    newer = fixture_card("pixi-complete", version="0.2.0", description="Now with speaker turns.")
    store.upsert(
        row("2", newer, repository="cogs/cog-audio-transcriber-1a2b", pushed_at=T0 + timedelta(days=1), tags=("0.2.0",))
    )
    # The same 0.2.0 artifact, mirrored: same digest, another source and host.
    store.upsert(
        row(
            "2",
            newer,
            repository="mirror/cog-audio-transcriber",
            source_id="mirror",
            host="mirror.example",
            pushed_at=T0 + timedelta(hours=1),
        )
    )
    withdrawn = fixture_card("pixi-complete", version="0.3.0-rc1")
    store.upsert(
        row("3", withdrawn, repository="cogs/cog-audio-transcriber-1a2b", pushed_at=T0 + timedelta(days=2), tags=())
    )
    store.mark_removed_one(SOURCE, "cogs/cog-audio-transcriber-1a2b", digest("3"))
    store.upsert(row("4", fixture_card("pixi-context"), repository="cogs/cog-meeting-notes-9f8e"))
    store.upsert(row("5", fixture_card("yaml-model"), repository="cogs/cog-small-model-7c6d"))
    # Rows the API must never list.
    store.upsert(row("6", None, repository="images/nginx", status=STATUS_NON_COG))
    store.upsert(row("7", None, repository="cogs/broken", status=STATUS_FAILED))
    store.upsert(row("8", fixture_card("draft"), repository="cogs/draft"))  # no id: not listable


# --- list ---------------------------------------------------------------------


async def test_empty_catalog_is_an_empty_page(api):
    client, _ = api
    response = await client.get("/v1/cogs")
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0, "next_offset": None}


async def test_list_is_one_entry_per_cog_collapsed_to_its_newest_present_version(api):
    client, store = api
    seed_catalog(store)

    items = (await client.get("/v1/cogs")).json()["items"]

    assert [item["cog_id"] for item in items] == [TRANSCRIBER, NOTES, MODEL]
    transcriber = items[0]
    # 0.3.0-rc1 is newer but removed; 0.2.0 at its newest-pushed location wins.
    assert transcriber["version"] == "0.2.0"
    assert transcriber["digest"] == digest("2")
    assert transcriber["source_id"] == SOURCE
    assert transcriber["reference"] == f"{HOST}/cogs/cog-audio-transcriber-1a2b@{digest('2')}"
    assert transcriber["removed_at"] is None
    assert transcriber["card"]["description"] == "Now with speaker turns."
    assert transcriber["card"]["io"] == {
        "accepts": ["media_transcription_request"],
        "produces": ["timestamped_transcript_bundle"],
    }


async def test_list_items_carry_a_trimmed_card_and_the_version_routes_the_full_one(api):
    client, store = api
    seed_catalog(store)
    stored = fixture_card("pixi-complete", version="0.2.0", description="Now with speaker turns.")
    assert all(stored.get(key) for key in LIST_CARD_OMITTED_KEYS), "the fixture card holds every heavy key"

    items = (await client.get("/v1/cogs")).json()["items"]
    assert items and all(not set(LIST_CARD_OMITTED_KEYS) & set(item["card"]) for item in items)
    transcriber = items[0]
    assert transcriber["card"] == json.loads(json.dumps(list_card(stored))), "only those keys are dropped"
    assert set(stored) - set(transcriber["card"]) == set(LIST_CARD_OMITTED_KEYS)

    detail = (await client.get(f"/v1/cogs/{TRANSCRIBER}")).json()
    version = (await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{digest('2')}")).json()
    for full in (detail["card"], version["card"]):
        assert full == json.loads(json.dumps(stored))
    body = await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{digest('2')}/cog.md")
    assert body.text == stored["body"]


def test_list_card_leaves_the_stored_card_alone():
    stored = {"id": "x", "body": "b", "profile_raw": "p", "frontmatter_raw": "f", "future_key": 1}
    assert list_card(stored) == {"id": "x", "future_key": 1}
    assert set(stored) == {"id", "body", "profile_raw", "frontmatter_raw", "future_key"}
    assert list_card(None) == {}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("kind=model", [MODEL]),
        ("kind=complete", [TRANSCRIBER]),
        ("publisher=Example%20Organization", [TRANSCRIBER, NOTES]),
        ("provides=model-artifact/gguf", [MODEL]),
        ("requires=media/ffmpeg", [TRANSCRIBER]),
        ("requires=model-endpoint/openai-compatible", [NOTES]),
        ("accepts=task_request", [NOTES]),
        ("produces=timestamped_transcript_bundle", [TRANSCRIBER]),
        ("q=SMALL-MODEL", [MODEL]),
        ("q=speaker%20turns", [TRANSCRIBER]),
        ("q=grounded", [NOTES]),
        ("source_id=mirror", [TRANSCRIBER]),
        ("kind=context&requires=model-endpoint/openai-compatible", [NOTES]),
        ("kind=model&requires=media/ffmpeg", []),
        ("requires=nothing/declares-this", []),
    ],
)
async def test_list_filters(api, query, expected):
    client, store = api
    seed_catalog(store)
    response = await client.get(f"/v1/cogs?{query}")
    assert response.status_code == 200, response.text
    assert [item["cog_id"] for item in response.json()["items"]] == expected


async def test_a_filter_tests_the_current_version_not_an_older_one(api):
    client, store = api
    store.upsert(row("1", fixture_card("pixi-complete", kind="model"), repository="cogs/t"))
    store.upsert(
        row("2", fixture_card("pixi-complete", version="0.2.0"), repository="cogs/t", pushed_at=T0 + timedelta(1))
    )

    assert (await client.get("/v1/cogs?kind=model")).json()["items"] == []
    listed = (await client.get("/v1/cogs?kind=complete")).json()["items"]
    detail = (await client.get(f"/v1/cogs/{TRANSCRIBER}")).json()
    assert [item["digest"] for item in listed] == [detail["digest"]] == [digest("2")]


async def test_source_id_scopes_which_versions_are_considered(api):
    client, store = api
    seed_catalog(store)
    (item,) = (await client.get("/v1/cogs?source_id=mirror")).json()["items"]
    assert (item["source_id"], item["version"]) == ("mirror", "0.2.0")
    assert item["reference"] == f"mirror.example/mirror/cog-audio-transcriber@{digest('2')}"


async def test_list_pages_with_limit_and_offset(api):
    client, store = api
    for index in range(5):
        document = fixture_card("pixi-complete", id=f"example/cog-{index}", name=f"cog-{index}")
        store.upsert(row(f"{index}", document, repository=f"cogs/cog-{index}"))

    first = (await client.get("/v1/cogs?limit=2")).json()
    assert [item["cog_id"] for item in first["items"]] == ["example/cog-0", "example/cog-1"]
    assert (first["limit"], first["offset"], first["next_offset"]) == (2, 0, 2)
    second = (await client.get("/v1/cogs?limit=2&offset=2")).json()
    assert [item["cog_id"] for item in second["items"]] == ["example/cog-2", "example/cog-3"]
    assert second["next_offset"] == 4
    last = (await client.get("/v1/cogs?limit=2&offset=4")).json()
    assert [item["cog_id"] for item in last["items"]] == ["example/cog-4"]
    assert last["next_offset"] is None, "no empty trailing page"
    exact = (await client.get("/v1/cogs?limit=5")).json()
    assert len(exact["items"]) == 5 and exact["next_offset"] is None
    assert (await client.get("/v1/cogs?offset=9")).json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "kind=", "q=" + "x" * 513])
async def test_list_refuses_out_of_range_parameters(api, query):
    client, _ = api
    response = await client.get(f"/v1/cogs?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- one Cog ------------------------------------------------------------------


async def test_cog_detail_is_the_current_card_plus_every_version(api):
    client, store = api
    seed_catalog(store)

    response = await client.get(f"/v1/cogs/{TRANSCRIBER}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["cog_id"] == TRANSCRIBER
    assert (detail["digest"], detail["version"]) == (digest("2"), "0.2.0")
    assert detail["card"]["id"] == TRANSCRIBER
    versions = [(v["version"], v["source_id"], v["removed_at"] is not None) for v in detail["versions"]]
    assert versions == [
        ("0.3.0-rc1", SOURCE, True),
        ("0.2.0", SOURCE, False),
        ("0.2.0", "mirror", False),
        ("0.1.0", SOURCE, False),
    ]
    assert set(detail["versions"][1]) == {
        "digest",
        "version",
        "source_id",
        "repository",
        "reference",
        "tags",
        "pushed_at",
        "indexed_at",
        "removed_at",
    }
    assert detail["versions"][1]["tags"] == ["0.2.0"]


async def test_a_cog_with_only_removed_versions_is_not_found_but_its_digest_is(api):
    client, store = api
    store.upsert(row("1", fixture_card("yaml-model"), repository="cogs/m"))
    store.mark_removed_one(SOURCE, "cogs/m", digest("1"))

    assert (await client.get("/v1/cogs")).json()["items"] == []
    response = await client.get(f"/v1/cogs/{MODEL}")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "cog_not_found", "message": "Cog not found"}}

    pinned = await client.get(f"/v1/cogs/{MODEL}/versions/{digest('1')}")
    assert pinned.status_code == 200
    assert pinned.json()["removed_at"] is not None
    assert pinned.json()["card"]["id"] == MODEL


async def test_unknown_cog_is_404(api):
    client, store = api
    seed_catalog(store)
    for path in ("/v1/cogs/example/nope", "/v1/cogs/", "/v1/cogs/example", f"/v1/cogs/{TRANSCRIBER}/extra"):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "cog_not_found"


async def test_a_percent_encoded_slash_in_the_cog_id_reaches_the_same_cog(api):
    client, store = api
    seed_catalog(store)
    response = await client.get("/v1/cogs/example%2Fcog-meeting-notes")
    assert response.status_code == 200
    assert response.json()["cog_id"] == NOTES


# --- one version --------------------------------------------------------------


async def test_version_is_the_exact_card_for_that_digest(api):
    client, store = api
    seed_catalog(store)

    old = (await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{digest('1')}")).json()
    new = (await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{digest('2')}")).json()

    assert (old["version"], old["card"]["version"]) == ("0.1.0", "0.1.0")
    assert (new["version"], new["card"]["description"]) == ("0.2.0", "Now with speaker turns.")
    assert old["card"] == json.loads(json.dumps(fixture_card("pixi-complete"))), "served verbatim"


async def test_removed_version_is_reachable_by_digest(api):
    client, store = api
    seed_catalog(store)
    response = await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{digest('3')}")
    assert response.status_code == 200
    assert response.json()["version"] == "0.3.0-rc1" and response.json()["removed_at"] is not None


async def test_version_404s(api):
    client, store = api
    seed_catalog(store)
    for path in (
        f"/v1/cogs/{TRANSCRIBER}/versions/{digest('f')}",  # unknown digest
        f"/v1/cogs/{NOTES}/versions/{digest('1')}",  # a digest of another Cog
        f"/v1/cogs/{TRANSCRIBER}/versions/{digest('7')}",  # a failed row carries no card
        f"/v1/cogs/{TRANSCRIBER}/versions/{digest('f')}/cog.md",
        f"/v1/cogs/{TRANSCRIBER}/versions/{digest('f')}/reference",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"error": {"code": "cog_version_not_found", "message": "Cog version not found"}}


@pytest.mark.parametrize(
    "value",
    ["abc", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha512:" + "a" * 64, "sha256:" + "g" * 64],
)
@pytest.mark.parametrize("suffix", ["", "/cog.md", "/reference"])
async def test_a_malformed_digest_is_a_validation_error(api, value, suffix):
    client, _ = api
    response = await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{value}{suffix}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_cog_md_is_the_indexed_markdown_body(api):
    client, store = api
    seed_catalog(store)

    response = await client.get(f"/v1/cogs/{NOTES}/versions/{digest('4')}/cog.md")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.text == read_fixture("pixi-context").body
    assert response.text.lstrip().startswith("#"), "the body after the frontmatter"


async def test_cog_md_is_empty_when_the_card_has_no_body(api):
    client, store = api
    document = fixture_card("yaml-model")
    del document["body"]
    store.upsert(row("1", document, repository="cogs/m"))
    response = await client.get(f"/v1/cogs/{MODEL}/versions/{digest('1')}/cog.md")
    assert (response.status_code, response.text) == (200, "")


async def test_reference_prefers_the_newest_pushed_present_location(api):
    client, store = api
    seed_catalog(store)

    response = await client.get(f"/v1/cogs/{TRANSCRIBER}/versions/{digest('2')}/reference")

    assert response.status_code == 200
    body = response.json()
    assert body["reference"] == f"{HOST}/cogs/cog-audio-transcriber-1a2b@{digest('2')}"
    assert (body["source_id"], body["repository"], body["digest"], body["present"]) == (
        SOURCE,
        "cogs/cog-audio-transcriber-1a2b",
        digest("2"),
        True,
    )
    assert [location["reference"] for location in body["locations"]] == [
        f"mirror.example/mirror/cog-audio-transcriber@{digest('2')}"
    ]


async def test_reference_prefers_a_present_location_over_a_newer_removed_one(api):
    client, store = api
    document = fixture_card("yaml-model")
    store.upsert(row("1", document, repository="cogs/m", pushed_at=T0 + timedelta(days=3)))
    store.upsert(row("1", document, repository="mirror/m", source_id="mirror", pushed_at=T0))
    store.mark_removed_one(SOURCE, "cogs/m", digest("1"))

    body = (await client.get(f"/v1/cogs/{MODEL}/versions/{digest('1')}/reference")).json()

    assert (body["source_id"], body["present"]) == ("mirror", True)
    assert [(loc["source_id"], loc["removed_at"] is not None) for loc in body["locations"]] == [(SOURCE, True)]

    store.mark_removed_one("mirror", "mirror/m", digest("1"))
    gone = (await client.get(f"/v1/cogs/{MODEL}/versions/{digest('1')}/reference")).json()
    assert gone["present"] is False, "still answered: installs pin digests"


# --- catalog.v1.json ----------------------------------------------------------


async def test_catalog_v1_empty(api):
    client, _ = api
    response = await client.get("/v1/cogs/catalog.v1.json")
    assert response.status_code == 200
    assert response.json() == {"schemaVersion": 1, "repositories": []}


async def test_catalog_v1_lists_present_cog_repositories_deduplicated_and_sorted(api):
    client, store = api
    seed_catalog(store)
    # The same path in a second source collapses into one entry.
    store.upsert(row("9", fixture_card("yaml-model"), repository="cogs/cog-small-model-7c6d", source_id="mirror"))
    # A single-segment path cannot be expressed as namespace/name.
    store.upsert(row("a", fixture_card("yaml-model", id="example/flat"), repository="flat"))
    # A description that is not a string becomes the empty string.
    store.upsert(row("b", fixture_card("pixi-context", id="example/odd", description=None), repository="more/odd/deep"))

    body = (await client.get("/v1/cogs/catalog.v1.json")).json()

    assert body["schemaVersion"] == 1
    assert body["repositories"] == [
        {"namespace": "cogs", "name": "cog-audio-transcriber-1a2b", "description": "Now with speaker turns."},
        {
            "namespace": "cogs",
            "name": "cog-meeting-notes-9f8e",
            "description": read_fixture("pixi-context").description,
        },
        {"namespace": "cogs", "name": "cog-small-model-7c6d", "description": read_fixture("yaml-model").description},
        {"namespace": "mirror", "name": "cog-audio-transcriber", "description": "Now with speaker turns."},
        {"namespace": "more", "name": "odd/deep", "description": ""},
    ]


async def test_catalog_v1_round_trips_through_the_hubs_own_static_index_reader(api):
    client, store = api
    seed_catalog(store)
    response = await client.get("/v1/cogs/catalog.v1.json")
    assert parse_index_document(response.content) == [
        "cogs/cog-audio-transcriber-1a2b",
        "cogs/cog-meeting-notes-9f8e",
        "cogs/cog-small-model-7c6d",
        "mirror/cog-audio-transcriber",
    ]


# --- storage and auth ---------------------------------------------------------

ROUTES = [
    "/v1/cogs",
    "/v1/cogs/catalog.v1.json",
    f"/v1/cogs/{TRANSCRIBER}",
    f"/v1/cogs/{TRANSCRIBER}/versions/{digest('1')}",
    f"/v1/cogs/{TRANSCRIBER}/versions/{digest('1')}/cog.md",
    f"/v1/cogs/{TRANSCRIBER}/versions/{digest('1')}/reference",
]


@pytest.mark.parametrize("path", ROUTES)
async def test_no_catalog_storage_is_a_503_not_an_empty_catalog(tmp_path, monkeypatch, path):
    app, client = await _client(tmp_path, monkeypatch, catalog_backend="")
    async with app.router.lifespan_context(app), client:
        assert isinstance(app.state.cog_catalog_store, UnavailableCogCatalogStore)
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "cog_catalog_unavailable", "message": "Cog catalog storage is not configured"}
    }


@pytest.mark.parametrize("path", ROUTES)
async def test_unauthenticated_requests_are_refused(tmp_path, monkeypatch, path):
    app, client = await _client(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app), client:
        # No path map at all (the default): the route's own auth dependency refuses.
        client.cookies.clear()
        response = await client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("path", ROUTES)
async def test_unauthenticated_requests_are_refused_under_the_hardened_path_map(tmp_path, monkeypatch, path):
    hardened = {"paths": [rule.model_dump() for rule in recommended_path_rules()], "default_access": "authenticated"}
    app, client = await _client(tmp_path, monkeypatch, security=hardened)
    async with app.router.lifespan_context(app), client:
        client.cookies.clear()
        response = await client.get(path)
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "unauthorized", "message": "Authentication required"}}
        client.cookies.update(AUTH)
        assert (await client.get("/v1/cogs")).status_code == 200


# --- OpenAPI and the card schema ----------------------------------------------


async def test_openapi_documents_every_route_and_the_card_schema(api):
    client, _ = api
    spec = (await client.get("/openapi.json")).json()

    paths = {path: spec["paths"][path] for path in spec["paths"] if path.startswith("/v1/cogs")}
    assert set(paths) == {
        "/v1/cogs",
        "/v1/cogs/catalog.v1.json",
        "/v1/cogs/{cog_id}",
        "/v1/cogs/{cog_id}/versions/{digest}",
        "/v1/cogs/{cog_id}/versions/{digest}/cog.md",
        "/v1/cogs/{cog_id}/versions/{digest}/reference",
        # The exchange for the Hub's own registry surface (issue #179).
        "/v1/cogs/registry-credentials",
        "/v1/cogs/registry-credentials/{credential_id}",
    }
    for path, item in paths.items():
        for method, operation in item.items():
            assert operation["summary"] and operation["description"], (method, path)
            assert "503" in operation["responses"], (method, path)
    assert set(paths["/v1/cogs/registry-credentials"]) == {"post", "delete"}
    assert set(paths["/v1/cogs/registry-credentials/{credential_id}"]) == {"delete"}
    issued = spec["components"]["schemas"]["RegistryCredentialResponse"]
    assert set(issued["required"]) == {"id", "registry", "username", "secret", "scope", "expires_at"}
    # The registry surface itself is not part of the Hub API document.
    assert not [path for path in spec["paths"] if path.startswith("/v2")]
    assert "text/markdown" in paths["/v1/cogs/{cog_id}/versions/{digest}/cog.md"]["get"]["responses"]["200"]["content"]
    list_params = {param["name"] for param in paths["/v1/cogs"]["get"]["parameters"]}
    assert list_params == {
        "kind",
        "publisher",
        "provides",
        "requires",
        "accepts",
        "produces",
        "q",
        "source_id",
        "limit",
        "offset",
    }
    card_schema = spec["components"]["schemas"]["CogEntry"]["properties"]["card"]
    assert card_schema["title"] == "CogCard" and card_schema["additionalProperties"] is True
    assert {"id", "name", "description", "version", "kind", "publisher", "provides", "requires", "io"} <= set(
        card_schema["properties"]
    )
    assert all("type" not in prop for prop in card_schema["properties"].values()), "documented, not enforced"

    # List items are a different schema, so a client cannot take one for the other.
    schemas = spec["components"]["schemas"]
    assert schemas["CogListPage"]["properties"]["items"]["items"] == {"$ref": "#/components/schemas/CogListEntry"}
    list_card_schema = schemas["CogListEntry"]["properties"]["card"]
    assert list_card_schema["title"] == "CogListCard" and list_card_schema["additionalProperties"] is True
    assert set(list_card_schema["properties"]) == set(card_schema["properties"]) - set(LIST_CARD_OMITTED_KEYS)
    list_description = paths["/v1/cogs"]["get"]["description"]
    for key in LIST_CARD_OMITTED_KEYS:
        assert f"`{key}`" in list_card_schema["description"], key
        assert f"`{key}`" in list_description, key
        assert key in card_schema["properties"], key

    # What an anonymous caller does not get is not promised to it.
    for name in ("CogVersion", "CogEntry", "CogListEntry", "CogDetail", "CogLocation", "CogReference"):
        source_id = schemas[name]["properties"]["source_id"]
        assert "anonymous" in source_id["description"], name
        assert "source_id" not in schemas[name].get("required", []), name
        assert "reference" in schemas[name]["required"], name
    for schema in (card_schema, list_card_schema):
        assert "anonymous" in schema["description"]
        for key in ANONYMOUS_CARD_OMITTED_KEYS:
            assert "anonymous" in schema["properties"][key]["description"], key
    source_param = next(param for param in paths["/v1/cogs"]["get"]["parameters"] if param["name"] == "source_id")
    assert "anonymous" in source_param["description"]


def test_the_card_schema_names_exactly_the_keys_the_reader_emits():
    # A reader key added without a schema entry (or the reverse) fails here,
    # so the OpenAPI description cannot drift from what the store holds. Only
    # names: the values are the Cog's own and are deliberately not typed.
    assert list(CARD_SCHEMA["properties"]) == [field.name for field in fields(CogCard)]


def _yaml_model_files(**replacements: bytes) -> dict[str, bytes]:
    root = Path(__file__).parent / "fixtures" / "cogs" / "yaml-model"
    files = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    for name, (old, new) in replacements.items():
        assert old in files[name]
        files[name] = files[name].replace(old, new)
    return files


async def test_a_card_with_values_of_unexpected_types_is_served_verbatim_not_a_500(api):
    client, store = api
    # Reader -> store -> API: a profile declaring a numeric id. The reader
    # keeps 42, the store's search key is "42", and the card still says 42.
    numeric = read_cog_bundle(_yaml_model_files(**{"cog.yaml": (b"id: example/cog-small-model", b"id: 42")})).to_dict()
    assert numeric["id"] == 42
    store.upsert(row("1", numeric, repository="cogs/numeric"))
    # And a stored card whose reader-shaped keys have the wrong shapes.
    odd = fixture_card("pixi-context", ops="not-a-mapping", errors="one string", card="one", requires={"x": 1})
    store.upsert(row("2", odd, repository="cogs/odd"))
    store.upsert(row("3", fixture_card("pixi-complete"), repository="cogs/fine"))

    page = await client.get("/v1/cogs")
    assert page.status_code == 200, page.text
    cards = {item["cog_id"]: item["card"] for item in page.json()["items"]}
    assert set(cards) == {"42", NOTES, TRANSCRIBER}, "one odd card never takes the page down"
    assert cards["42"] == json.loads(json.dumps(list_card(numeric)))
    assert cards[NOTES] == json.loads(json.dumps(list_card(odd)))

    for path in ("/v1/cogs/42", f"/v1/cogs/42/versions/{digest('1')}", f"/v1/cogs/{NOTES}/versions/{digest('2')}"):
        response = await client.get(path)
        assert response.status_code == 200, path
    assert (await client.get(f"/v1/cogs/{NOTES}")).json()["card"]["ops"] == "not-a-mapping"


@pytest.mark.parametrize("name", ["pixi-complete", "pixi-context", "yaml-model", "draft", "version-conflict", "prog"])
async def test_every_fixture_card_is_served_unchanged(api, name):
    client, store = api
    document = fixture_card(name, id=f"example/{name}")
    store.upsert(row("1", document, repository="cogs/x"))
    served = (await client.get(f"/v1/cogs/example/{name}/versions/{digest('1')}")).json()["card"]
    assert served == json.loads(json.dumps(document))


async def test_a_key_a_newer_reader_adds_is_kept_rather_than_dropped(api):
    client, store = api
    store.upsert(row("1", fixture_card("yaml-model") | {"future_key": {"x": 1}}, repository="cogs/m"))
    assert (await client.get(f"/v1/cogs/{MODEL}")).json()["card"]["future_key"] == {"x": 1}


# --- anonymous discovery through security.paths --------------------------------


def _map(*rules: dict) -> dict:
    return {
        "paths": [rule.model_dump() for rule in recommended_path_rules()] + list(rules),
        "default_access": "authenticated",
    }


PUBLIC_COGS = {"path": "/v1/cogs", "match": "prefix", "access": "public"}


@pytest.mark.parametrize("path", ["/v1/cogs", f"/v1/cogs/{MODEL}", "/v1/cogs/catalog.v1.json"])
async def test_a_public_rule_for_the_catalog_opens_anonymous_discovery(tmp_path, monkeypatch, path):
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        app.state.cog_catalog_store.upsert(row("1", fixture_card("yaml-model"), repository="cogs/m"))
        client.cookies.clear()
        response = await client.get(path)
        assert response.status_code == 200, response.text
        # Opening the catalog opens nothing else.
        assert (await client.get("/v1/frames")).status_code == 401


@pytest.mark.parametrize(
    "security",
    [
        None,  # unconfigured: default_access is "public", yet no rule says so for the catalog
        _map(),  # the hardened default
        _map({"path": "/v1/cogs", "match": "prefix", "access": "authenticated"}),
        {"paths": [{"path": "/", "match": "prefix", "access": "public"}], "default_access": "authenticated"},
        {"paths": [{"path": "/v1", "match": "prefix", "access": "public"}], "default_access": "public"},
    ],
    ids=["unconfigured", "hardened", "authenticated-rule", "broad-root-public", "broad-v1-public"],
)
@pytest.mark.parametrize("path", ["/v1/cogs", f"/v1/cogs/{MODEL}", "/v1/cogs/catalog.v1.json"])
async def test_without_a_public_catalog_rule_anonymous_requests_are_refused(tmp_path, monkeypatch, security, path):
    app, client = await _client(tmp_path, monkeypatch, security=security)
    async with app.router.lifespan_context(app), client:
        client.cookies.clear()
        response = await client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


async def test_an_exact_public_rule_opens_only_that_route(tmp_path, monkeypatch):
    rule = {"path": "/v1/cogs/catalog.v1.json", "match": "exact", "access": "public"}
    app, client = await _client(tmp_path, monkeypatch, security=_map(rule))
    async with app.router.lifespan_context(app), client:
        client.cookies.clear()
        assert (await client.get("/v1/cogs/catalog.v1.json")).status_code == 200
        assert (await client.get("/v1/cogs")).status_code == 401


# --- what an anonymous caller sees ---------------------------------------------

REDACTED_ROUTES = [
    "/v1/cogs",
    f"/v1/cogs/{TRANSCRIBER}",
    f"/v1/cogs/{TRANSCRIBER}/versions/{digest('2')}",
    f"/v1/cogs/{TRANSCRIBER}/versions/{digest('2')}/reference",
]


def _keys(value) -> set[str]:
    """Every mapping key anywhere in a JSON document, cards excluded (they are the Cog's own)."""

    if isinstance(value, list):
        return set().union(*map(_keys, value)) if value else set()
    if isinstance(value, dict):
        return set(value).union(*(_keys(child) for key, child in value.items() if key != "card"))
    return set()


def _count(value, name: str) -> int:
    """How many times ``name`` is a mapping key in a JSON document, cards excluded."""

    if isinstance(value, list):
        return sum(_count(child, name) for child in value)
    if isinstance(value, dict):
        return (name in value) + sum(_count(child, name) for key, child in value.items() if key != "card")
    return 0


def _cards(document: dict) -> list[dict]:
    return [item["card"] for item in document.get("items", [document]) if "card" in item]


def _seed_with_diagnostics(store: InMemoryCogCatalogStore) -> None:
    seed_catalog(store)
    # The current transcriber card, with something for the reader to have said.
    noisy = fixture_card(
        "pixi-complete",
        version="0.2.0",
        errors=["reader error: example"],
        warnings=["profile kind 'x' disagrees"],
    )
    store.upsert(
        row("2", noisy, repository="cogs/cog-audio-transcriber-1a2b", pushed_at=T0 + timedelta(days=1), tags=("0.2.0",))
    )


@pytest.mark.parametrize("path", REDACTED_ROUTES)
async def test_anonymous_answers_carry_no_source_id_and_no_reader_diagnostics(tmp_path, monkeypatch, path):
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        _seed_with_diagnostics(app.state.cog_catalog_store)

        signed_in = await client.get(path)
        assert signed_in.status_code == 200, signed_in.text
        client.cookies.clear()
        anonymous = await client.get(path)
        assert anonymous.status_code == 200, anonymous.text

    full, redacted = signed_in.json(), anonymous.json()
    locations = full.get("items", []) + full.get("versions", []) + full.get("locations", [])
    expected = len(locations) + (0 if "items" in full else 1)
    assert expected >= 1
    assert _count(full, "source_id") == expected, "a signed-in caller sees every source id"
    assert "source_id" not in _keys(redacted)
    for card in _cards(full):
        assert set(ANONYMOUS_CARD_OMITTED_KEYS) <= set(card)
    for card in _cards(redacted):
        assert not set(ANONYMOUS_CARD_OMITTED_KEYS) & set(card)
    # The install reference stays: a client must know where to pull from.
    assert _keys(redacted) == _keys(full) - {"source_id"}
    if "reference" in full:
        assert redacted["reference"] == full["reference"]
    if "locations" in full:
        assert [loc["reference"] for loc in redacted["locations"]] == [loc["reference"] for loc in full["locations"]]


async def test_anonymous_cards_keep_everything_but_the_diagnostics(tmp_path, monkeypatch):
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        _seed_with_diagnostics(app.state.cog_catalog_store)
        full = (await client.get(f"/v1/cogs/{TRANSCRIBER}")).json()
        client.cookies.clear()
        redacted = (await client.get(f"/v1/cogs/{TRANSCRIBER}")).json()
        listed = (await client.get("/v1/cogs")).json()["items"][0]

    assert full["card"]["errors"] == ["reader error: example"]
    expected = {key: value for key, value in full["card"].items() if key not in ANONYMOUS_CARD_OMITTED_KEYS}
    assert redacted["card"] == expected
    assert listed["card"] == list_card(expected), "trimmed and redacted"
    assert redacted["card"]["body"], "the full card still carries the heavy keys"


async def test_an_anonymous_source_id_filter_is_refused(tmp_path, monkeypatch):
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        seed_catalog(app.state.cog_catalog_store)
        assert [item["cog_id"] for item in (await client.get("/v1/cogs?source_id=mirror")).json()["items"]] == [
            TRANSCRIBER
        ]
        client.cookies.clear()
        long_value = "probe-" + "x" * 600  # past the filter's 512-character limit
        for query, value in [
            ("source_id=mirror", "mirror"),
            ("source_id=no-such-source", "no-such-source"),
            ("source_id=", None),
            (f"source_id={long_value}", long_value),
            # Refused before parameter validation, so another bad parameter
            # does not route the value through the generic, echoing 422.
            (f"source_id={long_value}&limit=0", long_value),
            (f"limit=0&source_id={long_value}&q=", long_value),
            (f"source_id=mirror&source_id={long_value}", long_value),
        ]:
            response = await client.get(f"/v1/cogs?{query}")
            assert response.status_code == 422, query
            error = response.json()["error"]
            assert error["code"] == "validation_error"
            assert error["details"] == [
                {
                    "type": "value_error",
                    "loc": ["query", "source_id"],
                    "msg": "source_id is not available to anonymous callers",
                }
            ], query
            if value:
                assert value not in response.text, "the probed value is not echoed"
                assert "probe-" not in response.text
        assert (await client.get("/v1/cogs?kind=complete")).status_code == 200, "other filters stay open"
        assert (await client.get("/v1/cogs?limit=0")).json()["error"]["code"] == "validation_error"
        client.cookies.update(AUTH)
        signed_in = await client.get(f"/v1/cogs?source_id={long_value}")
        assert signed_in.status_code == 422, "a signed-in caller still gets ordinary validation"


async def test_anonymous_catalog_v1_is_unchanged(tmp_path, monkeypatch):
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        _seed_with_diagnostics(app.state.cog_catalog_store)
        signed_in = (await client.get("/v1/cogs/catalog.v1.json")).json()
        client.cookies.clear()
        anonymous = (await client.get("/v1/cogs/catalog.v1.json")).json()
    assert anonymous == signed_in
    assert _keys(anonymous) == {"schemaVersion", "repositories", "namespace", "name", "description"}


@pytest.mark.parametrize(
    "credentials",
    [
        {"cookies": {"IdToken-test": "not-a-token"}},
        {"headers": {"Authorization": "Bearer not-a-token"}},
        {"headers": {"Authorization": "Basic YWxpY2U6c2VjcmV0"}},
        {"headers": {"Authorization": ""}},
        {"headers": {"Authorization": "   "}},
        {"cookies": {"IdToken-test": ""}},
    ],
    ids=["garbage-cookie", "garbage-bearer", "other-scheme", "empty-header", "blank-header", "empty-cookie"],
)
@pytest.mark.parametrize("path", [*REDACTED_ROUTES, "/v1/cogs/catalog.v1.json"])
async def test_under_a_public_rule_rejected_credentials_are_a_401_not_anonymous(
    tmp_path, monkeypatch, credentials, path
):
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        seed_catalog(app.state.cog_catalog_store)
        client.cookies.clear()
        response = await client.get(path, headers=credentials.get("headers"), cookies=credentials.get("cookies"))
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "unauthorized"
        assert (await client.get(path)).status_code == 200, "the same request without credentials is anonymous"


@pytest_asyncio.fixture
async def signed_api(tmp_path, monkeypatch):
    """A public catalog whose callers present bearer tokens verified against a live JWKS endpoint."""

    endpoint = _JWKSEndpoint()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", endpoint.url)
    try:
        app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
        async with app.router.lifespan_context(app), client:
            seed_catalog(app.state.cog_catalog_store)
            client.cookies.clear()
            yield client, endpoint
    finally:
        endpoint.close()


def _bearer(**claims) -> dict:
    payload = {"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a", **claims}
    token = jwt.encode(payload, KEY_1_PEM, algorithm="RS256", headers={"kid": KEY_1_JWK["kid"]})
    return {"Authorization": f"Bearer {token}"}


async def test_a_verified_bearer_under_a_public_rule_gets_the_full_view(signed_api):
    client, _ = signed_api
    response = await client.get(f"/v1/cogs/{TRANSCRIBER}", headers=_bearer())
    assert response.status_code == 200, response.text
    assert _count(response.json(), "source_id") == 1 + len(response.json()["versions"])


async def test_an_expired_bearer_under_a_public_rule_is_a_401(signed_api):
    client, _ = signed_api
    response = await client.get(f"/v1/cogs/{TRANSCRIBER}", headers=_bearer(exp=int(T0.timestamp())))
    assert response.status_code == 401, response.text


async def test_a_jwks_outage_is_a_401_not_an_anonymous_200(signed_api):
    # The real decoder path: the verifier cannot fetch signing keys, so it
    # cannot tell a good token from a bad one. That must not read as a visit
    # without credentials.
    client, jwks = signed_api
    jwks.status = 503
    response = await client.get(f"/v1/cogs/{TRANSCRIBER}", headers=_bearer())
    assert jwks.fetches >= 1, "the decoder really tried the JWKS endpoint"
    assert response.status_code == 401, response.text
    assert "source_id" not in response.text


async def test_a_valid_caller_without_an_organization_gets_the_anonymous_view(tmp_path, monkeypatch):
    def unaffiliated(_request):
        raise NoOrganizationError()

    monkeypatch.setattr(cogs_router, "get_auth_context", unaffiliated)
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        _seed_with_diagnostics(app.state.cog_catalog_store)
        assert client.cookies, "the caller presents credentials"
        detail = await client.get(f"/v1/cogs/{TRANSCRIBER}")
        listed = await client.get("/v1/cogs")
        refused = await client.get("/v1/cogs?source_id=mirror")

    assert detail.status_code == listed.status_code == 200
    assert detail.json()["versions"] and listed.json()["items"]
    for document in (detail.json(), listed.json()):
        assert _count(document, "source_id") == 0
        for card in _cards(document):
            assert not set(ANONYMOUS_CARD_OMITTED_KEYS) & set(card)
    assert refused.status_code == 422


async def test_under_a_public_rule_other_auth_failures_propagate(tmp_path, monkeypatch):
    def unavailable(_request):
        raise HTTPException(status_code=503, detail="membership unknown")

    monkeypatch.setattr(cogs_router, "get_auth_context", unavailable)
    app, client = await _client(tmp_path, monkeypatch, security=_map(PUBLIC_COGS))
    async with app.router.lifespan_context(app), client:
        response = await client.get("/v1/cogs")
    assert response.status_code == 503, response.text
