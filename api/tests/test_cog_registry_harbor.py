"""The Harbor adapter: REST pagination and encoding, credential handling, and webhook translation."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cog_registry_fakes import API_URL, DIGEST_A, DIGEST_B, HOST, URL, FakeOCIFactory, harbor_artifact_json

from collab_hub_api.cogs.adapters import harbor
from collab_hub_api.cogs.adapters.harbor import MAX_PAGES, MAX_WEBHOOK_BODY_BYTES, PAGE_SIZE, HarborRegistrySource
from collab_hub_api.cogs.registry import (
    ArtifactRef,
    CogRegistrySourceConfig,
    RegistryEvent,
    RegistrySourceAuthError,
    RegistrySourceError,
    RegistrySourceProtocolError,
    WebhookRequest,
    build_registry_sources,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cogs" / "harbor-events"
EVENT_DIGEST = "sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
EVENT_REPO = "cogs/cog-alpha-1a2b3c4d"
EVENT_HOST = "harbor.example.com"
CREDENTIALS = {"username": "robot$cogs+indexer", "password": "robot-secret-value"}


def make_source(handler, **overrides) -> HarborRegistrySource:
    config = CogRegistrySourceConfig(
        **{
            "id": "harbor-main",
            "kind": "harbor",
            "url": URL,
            "api_url": API_URL,
            "projects": ["cogs"],
            "credentials": CREDENTIALS,
            **overrides,
        }
    )
    [source] = build_registry_sources(
        [config], oci_client_factory=FakeOCIFactory({}), http_transport=httpx.MockTransport(handler)
    )
    assert isinstance(source, HarborRegistrySource)
    return source


def repo_page(names: list[str], *, link: str | None = None) -> httpx.Response:
    headers = {"Link": link} if link is not None else {}
    return httpx.Response(200, json=[{"name": name} for name in names], headers=headers)


def fixture(name: str) -> WebhookRequest:
    return WebhookRequest(headers={"content-type": "application/json"}, body=(FIXTURES / name).read_bytes())


def payload(document: object) -> WebhookRequest:
    return WebhookRequest(headers={}, body=json.dumps(document).encode())


# --- REST: pagination -----------------------------------------------------


async def test_pagination_follows_link_next_and_stops_when_absent() -> None:
    names = [f"cogs/repo-{i:04d}" for i in range(2 * PAGE_SIZE + 5)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        assert request.url.params["page_size"] == str(PAGE_SIZE)
        chunk = names[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        # Harbor's Link is relative and may carry prev as well as next.
        link = f'</api/v2.0/projects/cogs/repositories?page={page + 1}&page_size={PAGE_SIZE}>; rel="next"'
        if page > 1:
            link = f'</api/v2.0/projects/cogs/repositories?page={page - 1}&page_size={PAGE_SIZE}>; rel="prev" , ' + link
        if page == 3:
            link = f'</api/v2.0/projects/cogs/repositories?page=2&page_size={PAGE_SIZE}>; rel="prev"'
        return repo_page(chunk, link=link)

    source = make_source(handler)
    assert await source.list_repositories() == sorted(names)
    assert [int(r.url.params["page"]) for r in seen] == [1, 2, 3]
    assert all(r.url.path == "/api/v2.0/projects/cogs/repositories" for r in seen)


async def test_pagination_without_link_stops_at_short_page() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        seen.append(page)
        if page == 1:
            return repo_page([f"cogs/full-{i:03d}" for i in range(PAGE_SIZE)])
        return repo_page(["cogs/tail-a", "cogs/tail-b"])

    assert len(await make_source(handler).list_repositories()) == PAGE_SIZE + 2
    assert seen == [1, 2]


async def test_pagination_without_link_stops_at_empty_page() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        seen.append(page)
        return repo_page([f"cogs/full-{i:03d}" for i in range(PAGE_SIZE)] if page == 1 else [])

    assert len(await make_source(handler).list_repositories()) == PAGE_SIZE
    assert seen == [1, 2]


async def test_pagination_never_follows_the_link_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if int(request.url.params["page"]) == 1:
            return repo_page(["cogs/a"], link='<https://elsewhere.example/steal?page=9>; rel="next"')
        return repo_page(["cogs/b"], link='<https://elsewhere.example/x?page=1>; rel="prev"')

    assert await make_source(handler).list_repositories() == ["cogs/a", "cogs/b"]
    assert [str(r.url) for r in seen] == [
        f"{API_URL}/api/v2.0/projects/cogs/repositories?page=1&page_size={PAGE_SIZE}",
        f"{API_URL}/api/v2.0/projects/cogs/repositories?page=2&page_size={PAGE_SIZE}",
    ]


async def test_pagination_refuses_a_listing_that_never_ends() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return repo_page(["cogs/loop"], link='</api/v2.0/projects/cogs/repositories?page=2>; rel="next"')

    with pytest.raises(RegistrySourceProtocolError, match=f"did not end within {MAX_PAGES} pages"):
        await make_source(handler).list_repositories()


async def test_list_repositories_spans_projects_and_skips_odd_names(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/projects/cogs/" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {"name": "cogs/beta"},
                    {"name": "cogs/alpha"},
                    {"name": "cogs/alpha"},
                    {"name": "library/not-in-project"},
                    {"name": "cogs/Bad Name"},
                    {"id": 7},
                    "cogs/not-an-object",
                ],
            )
        return httpx.Response(200, json=[{"name": "models/qwen"}])

    with caplog.at_level(logging.WARNING):
        source = make_source(handler, projects=["models", "cogs"])
        assert await source.list_repositories() == ["cogs/alpha", "cogs/beta", "models/qwen"]
    assert sum("skipping" in record.message for record in caplog.records) == 4


# --- REST: artifacts ------------------------------------------------------


async def test_list_artifacts_encodes_nested_repository_names_and_parses_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                harbor_artifact_json({"digest": DIGEST_B, "tags": ["v0"], "pushed_at": None, "media_type": "m"}),
                {
                    "digest": DIGEST_A,
                    "tags": [{"name": "latest"}, {"name": "v1"}, {"no": "name"}, "v2", {"name": 3}],
                    "push_time": "2026-09-04T17:11:31.297Z",
                    "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
                },
                # The same digest listed again: tags accumulate, the first timestamp wins.
                {"digest": DIGEST_A, "tags": [{"name": "v0.9"}], "push_time": "2020-01-01T00:00:00Z"},
                {"digest": "not-a-digest", "tags": []},
                {"tags": [{"name": "orphan"}]},
                "not-an-object",
            ],
        )

    refs = await make_source(handler).list_artifacts("cogs/nested/cog-a")
    [request] = seen
    assert request.url.raw_path.startswith(b"/api/v2.0/projects/cogs/repositories/nested%252Fcog-a/artifacts?")
    assert request.url.params["with_tag"] == "true"
    assert refs == [
        ArtifactRef(
            digest=DIGEST_A,
            tags=("latest", "v0.9", "v1"),  # tags accumulate across repeated entries; first timestamp wins
            pushed_at=datetime(2026, 9, 4, 17, 11, 31, 297000, tzinfo=UTC),
            media_type="application/vnd.oci.image.manifest.v1+json",
        ),
        ArtifactRef(digest=DIGEST_B, tags=("v0",), pushed_at=None, media_type="m"),
    ]


async def test_list_artifacts_single_component_name_is_encoded_once() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    assert await make_source(handler).list_artifacts("cogs/cog-a.b_c") == []
    assert seen[0].url.raw_path.startswith(b"/api/v2.0/projects/cogs/repositories/cog-a.b_c/artifacts?")


async def test_list_artifacts_refuses_repositories_outside_configured_projects() -> None:
    source = make_source(lambda request: httpx.Response(500))
    for repo in ("library/alpine", "cogs", "cogs/", "/cogs/x", "cogs/Bad"):
        with pytest.raises(RegistrySourceError, match="not under a configured project"):
            await source.list_artifacts(repo)


# --- REST: credentials and status handling ---------------------------------


async def test_rest_requests_carry_basic_auth_and_json_accept() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    await make_source(handler).list_repositories()
    expected = base64.b64encode(f"{CREDENTIALS['username']}:{CREDENTIALS['password']}".encode()).decode()
    assert seen[0].headers["Authorization"] == f"Basic {expected}"
    assert seen[0].headers["Accept"] == "application/json"


async def test_rest_requests_without_credentials_are_anonymous() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    source = make_source(handler, credentials={})
    await source.list_repositories()
    assert "authorization" not in seen[0].headers
    assert source.oci().credentials is None


@pytest.mark.parametrize("status", [401, 403])
async def test_rest_refused_credential_is_a_clear_error_without_the_secret(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"code": "UNAUTHORIZED", "message": "unauthorized"}]})

    with pytest.raises(RegistrySourceAuthError) as excinfo:
        await make_source(handler).list_repositories()
    message = str(excinfo.value)
    assert f"refused the configured credential for /projects/cogs/repositories (HTTP {status})" in message
    assert CREDENTIALS["password"] not in message
    assert "unauthorized" not in message  # Harbor's body is not echoed either


async def test_rest_404_and_5xx_and_bad_bodies() -> None:
    with pytest.raises(RegistrySourceError, match="does not exist on the registry") as missing_project:
        await make_source(lambda r: httpx.Response(404, json={"errors": []})).list_repositories()
    # A repository that does not exist is the same error to everyone, and one a sweep can tell apart:
    # sent there by a publish through the Hub that never landed, it reads this as "nothing there".
    from collab_hub_api.cogs.registry import RegistryRepositoryNotFound

    assert not isinstance(missing_project.value, RegistryRepositoryNotFound)
    with pytest.raises(RegistrySourceError, match="does not exist on the registry") as missing_repository:
        await make_source(lambda r: httpx.Response(404, json={"errors": []})).list_artifacts("cogs/never-landed")
    assert isinstance(missing_repository.value, RegistryRepositoryNotFound)
    with pytest.raises(RegistrySourceProtocolError, match="answered HTTP 503"):
        await make_source(lambda r: httpx.Response(503)).list_repositories()
    with pytest.raises(RegistrySourceProtocolError, match="is not JSON"):
        await make_source(lambda r: httpx.Response(200, content=b"<html>")).list_repositories()
    with pytest.raises(RegistrySourceProtocolError, match="is not a JSON list"):
        await make_source(lambda r: httpx.Response(200, json={"name": "x"})).list_repositories()


async def test_rest_transport_failure_is_reported_as_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RegistrySourceError, match="request to the registry API failed"):
        await make_source(handler).list_repositories()


async def test_api_base_accepts_origin_or_full_prefix_and_defaults_to_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.copy_with(query=None)))
        return httpx.Response(200, json=[])

    await make_source(handler, api_url=f"{API_URL}/api/v2.0").list_repositories()
    await make_source(handler, api_url="").list_repositories()
    source = make_source(handler, api_url=f"{API_URL}/api/v2.0/")
    await source.list_repositories()
    assert seen == [
        f"{API_URL}/api/v2.0/projects/cogs/repositories",
        f"{URL}/api/v2.0/projects/cogs/repositories",
        f"{API_URL}/api/v2.0/projects/cogs/repositories",
    ]
    assert source.host == HOST
    assert source.oci().base_url == API_URL


# --- webhooks ---------------------------------------------------------------


def webhook_source(**overrides) -> HarborRegistrySource:
    settings = {"url": f"https://{EVENT_HOST}", "api_url": "", **overrides}
    return make_source(lambda request: httpx.Response(500), **settings)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "default-push.json",
            [RegistryEvent(kind="push", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=("latest", "sha-1a2b3c4d5e6f"))],
        ),
        ("default-delete.json", [RegistryEvent(kind="delete", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=("latest",))]),
        ("default-push-untagged.json", [RegistryEvent(kind="push", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=())]),
        ("cloudevents-push.json", [RegistryEvent(kind="push", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=("latest",))]),
        (
            "cloudevents-delete.json",
            [RegistryEvent(kind="delete", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=("latest",))],
        ),
        ("default-scanning-completed.json", []),
        ("cloudevents-scan-completed.json", []),
    ],
)
def test_parse_event_fixtures(name: str, expected: list[RegistryEvent]) -> None:
    assert webhook_source().parse_event(fixture(name)) == expected


@pytest.mark.parametrize(
    "name",
    ["foreign-payload.json", "default-push-other-project.json", "default-push-other-host.json"],
)
def test_parse_event_not_mine(name: str, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        assert webhook_source().parse_event(fixture(name)) is None
    if name == "default-push-other-project.json":
        assert any("dropping a webhook for project 'library'" in r.message for r in caplog.records)
    if name == "default-push-other-host.json":
        assert any(r.levelno == logging.WARNING and "names another host" in r.message for r in caplog.records)


def test_parse_event_ignored_type_for_another_project_is_not_claimed() -> None:
    # A scan event for a project this source does not own must stay claimable
    # by the source that does, so it is None here rather than [].
    body = json.loads((FIXTURES / "default-scanning-completed.json").read_bytes())
    body["event_data"]["repository"]["namespace"] = "library"
    assert webhook_source().parse_event(payload(body)) is None
    assert webhook_source(id="library", projects=["library"]).parse_event(payload(body)) == []


def test_parse_event_deeply_nested_json_is_not_mine() -> None:
    depth = 200_000
    body = b"[" * depth + b"]" * depth
    assert len(body) < MAX_WEBHOOK_BODY_BYTES
    assert webhook_source().parse_event(WebhookRequest(headers={}, body=body)) is None


def test_parse_event_other_project_is_claimable_by_a_second_source() -> None:
    assert webhook_source(id="library", projects=["library"]).parse_event(
        fixture("default-push-other-project.json")
    ) == [RegistryEvent(kind="push", repo="library/alpine", digest=EVENT_DIGEST, tags=("3.20",))]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"{",
        b"[]",
        b'"PUSH_ARTIFACT"',
        b"null",
        json.dumps({"type": 7}).encode(),
        json.dumps({"type": "PUSH_ARTIFACT_V9", "event_data": {}}).encode(),
        json.dumps({"specversion": "1.0", "type": "com.example.pushed", "data": {}}).encode(),
        json.dumps({"event_data": {"repository": {"namespace": "cogs"}}}).encode(),
    ],
)
def test_parse_event_returns_none_for_non_harbor_bodies(body: bytes) -> None:
    assert webhook_source().parse_event(WebhookRequest(headers={}, body=body)) is None


def test_parse_event_oversized_body_is_not_parsed(caplog) -> None:
    body = (FIXTURES / "default-push.json").read_bytes()
    padded = body[:-1] + b' , "padding": "' + b"x" * MAX_WEBHOOK_BODY_BYTES + b'"}'
    assert len(padded) > MAX_WEBHOOK_BODY_BYTES
    with caplog.at_level(logging.DEBUG):
        assert webhook_source().parse_event(WebhookRequest(headers={}, body=padded)) is None
    assert any("oversized" in r.message for r in caplog.records)


def repository_block(**overrides) -> dict:
    block = {"name": "cog-alpha-1a2b3c4d", "namespace": "cogs", "repo_full_name": EVENT_REPO, "repo_type": "private"}
    return {**block, **overrides}


def test_parse_event_recognized_but_unusable_shapes_yield_empty() -> None:
    source = webhook_source()
    assert source.parse_event(payload({"type": "PUSH_ARTIFACT", "occur_at": 1})) == []
    assert source.parse_event(payload({"type": "PUSH_ARTIFACT", "event_data": []})) == []
    assert source.parse_event(payload({"type": "PUSH_ARTIFACT", "event_data": {"resources": []}})) == []
    assert source.parse_event(payload({"specversion": "1.0", "type": "harbor.artifact.pushed"})) == []
    assert source.parse_event(payload({"specversion": "1.0", "type": "harbor.artifact.pushed", "data": "x"})) == []
    # Recognized Harbor event, repository named but no resources: nothing to act on.
    only_repository = {"type": "PUSH_ARTIFACT", "event_data": {"repository": repository_block()}}
    assert source.parse_event(payload(only_repository)) == []
    assert (
        source.parse_event(
            payload({"type": "PUSH_ARTIFACT", "event_data": {"repository": repository_block(), "resources": [1, "x"]}})
        )
        == []
    )


def test_parse_event_malformed_repository_name_yields_empty(caplog) -> None:
    body = payload(
        {
            "type": "PUSH_ARTIFACT",
            "event_data": {
                "repository": repository_block(repo_full_name="cogs/Bad Name"),
                "resources": [{"digest": EVENT_DIGEST, "tag": "latest"}],
            },
        }
    )
    with caplog.at_level(logging.WARNING):
        assert webhook_source().parse_event(body) == []
    assert any("not an OCI path" in r.message for r in caplog.records)


def test_parse_event_builds_repo_from_namespace_and_name_when_full_name_missing() -> None:
    body = payload(
        {
            "type": "DELETE_ARTIFACT",
            "event_data": {
                "repository": {"name": "cog-alpha-1a2b3c4d", "namespace": "cogs"},
                "resources": [{"digest": EVENT_DIGEST}],
            },
        }
    )
    assert webhook_source().parse_event(body) == [
        RegistryEvent(kind="delete", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=())
    ]


def test_parse_event_groups_resources_by_digest_and_orders_deterministically() -> None:
    body = payload(
        {
            "type": "PUSH_ARTIFACT",
            "event_data": {
                "repository": repository_block(),
                "resources": [
                    {"digest": DIGEST_B, "tag": "v0", "resource_url": f"{EVENT_HOST}/{EVENT_REPO}:v0"},
                    {"digest": DIGEST_A, "tag": "v1"},
                    {"digest": DIGEST_A, "tag": "latest"},
                    {"digest": DIGEST_A, "tag": DIGEST_A},  # untagged push reports the digest as the tag
                    {"digest": DIGEST_A, "tag": ""},
                    {"digest": DIGEST_A, "tag": 42},
                    {"digest": "garbage", "tag": "orphan"},  # unusable digest: kept as digest=None
                    {"tag": "another-orphan"},
                ],
            },
        }
    )
    assert webhook_source().parse_event(body) == [
        RegistryEvent(kind="push", repo=EVENT_REPO, digest=None, tags=("another-orphan", "orphan")),
        RegistryEvent(kind="push", repo=EVENT_REPO, digest=DIGEST_A, tags=("latest", "v1")),
        RegistryEvent(kind="push", repo=EVENT_REPO, digest=DIGEST_B, tags=("v0",)),
    ]


def test_parse_event_ignores_headers_and_accepts_host_with_port() -> None:
    source = webhook_source(url=f"https://{EVENT_HOST}:8443")
    body = payload(
        {
            "type": "PUSH_ARTIFACT",
            "event_data": {
                "repository": repository_block(),
                "resources": [
                    {"digest": EVENT_DIGEST, "tag": "v1", "resource_url": f"{EVENT_HOST}:8443/{EVENT_REPO}:v1"}
                ],
            },
        }
    )
    assert source.host == f"{EVENT_HOST}:8443"
    assert source.parse_event(WebhookRequest(headers={"authorization": "irrelevant-here"}, body=body.body)) == [
        RegistryEvent(kind="push", repo=EVENT_REPO, digest=EVENT_DIGEST, tags=("v1",))
    ]


def test_module_is_the_only_home_of_the_vendor_name() -> None:
    """The acceptance grep from the issue: vendor logic lives here and nowhere else in cogs/."""

    package = Path(harbor.__file__).parents[1]
    offenders = []
    for path in package.rglob("*.py"):
        if path.name == "harbor.py":
            continue
        text = path.read_text().lower()
        if path.name == "registry.py":
            # The kind literal and the builder dispatch are allowed to name the adapter.
            allowed = ('"harbor"', "'harbor'", "adapters.harbor import harborregistrysource", "harborregistrysource(")
            for phrase in allowed:
                text = text.replace(phrase, "")
        if path.name == "oci.py":
            continue  # the generic client's docstring names registries it does *not* special-case; owned by #82
        if "harbor" in text:
            offenders.append(str(path.relative_to(package)))
    assert offenders == []
