"""A Hub that serves pulls and accepts no publishes answers exactly as it did before publishing existed.

Publishing (issue #180) is switched on by marking a registry source
``publish: true``. Without one, none of it may show: not in what a token
request's scopes mean, not in the order of authentication and refusal, not
in the words of a refusal. The expectations here are the read-only
surface's own (issue #179), written out literally, so a change to the push
half that leaks into a Hub without it fails here.
"""

# ruff: noqa: F811 - the fixtures imported from test_cog_serving are used as parameters

from __future__ import annotations

import pytest
from test_cog_serving import (
    ALICE,
    ALPHA,
    HUB_HOST,
    HUB_URL,
    REPO,
    Hub,
    basic,
    hub,  # noqa: F401 - fixture
    make_hub,  # noqa: F401 - fixture
)

OTHER = "cogs/another-cog"
READ_ONLY = {"errors": [{"code": "UNSUPPORTED", "message": "this registry is read-only", "detail": {}}]}
UNAUTHORIZED = {"errors": [{"code": "UNAUTHORIZED", "message": "authentication required", "detail": {}}]}


def challenge(repository: str, *, insufficient: bool = False) -> str:
    error = ',error="insufficient_scope"' if insufficient else ""
    return f'Bearer realm="{HUB_URL}/v2/token",service="{HUB_HOST}",scope="repository:{repository}:pull"{error}'


async def token_for(hub: Hub, *scopes: str) -> dict[str, str]:
    credential = await hub.exchange(ALICE)
    response = await hub.get(
        "/v2/token",
        params={"service": HUB_HOST, "scope": list(scopes)},
        headers=basic(credential["username"], credential["secret"]),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def test_asking_to_push_names_no_repository(hub: Hub):
    """``push`` is one more action this surface does not grant: a scope without ``pull`` is dropped."""

    hub.seed(REPO, ALPHA, "v1")
    hub.seed(OTHER, ALPHA, "v1")
    token = await token_for(hub, f"repository:{REPO}:push", f"repository:{OTHER}:pull,push")
    # The repository asked for with push alone is not on the token: not to push to, and not to pull from.
    refused = await hub.get(f"/v2/{REPO}/tags/list", headers=token)
    assert refused.status_code == 401 and refused.json() == UNAUTHORIZED
    assert refused.headers["www-authenticate"] == challenge(REPO, insufficient=True)
    assert (await hub.get(f"/v2/{REPO}/manifests/v1", headers=token)).status_code == 401
    # With pull among the actions it is, for pulling.
    listed = await hub.get(f"/v2/{OTHER}/tags/list", headers=token)
    assert listed.status_code == 200 and listed.json() == {"name": OTHER, "tags": ["v1"]}


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_an_upload_path_is_a_blob_path_like_any_other(hub: Hub, method):
    """Authentication first, with the pull challenge; then the answer any unknown blob gets. Never a 405."""

    hub.seed(REPO, ALPHA, "v1")
    url = f"/v2/{REPO}/blobs/uploads/up-1"
    anonymous = await hub.request(method, url)
    assert anonymous.status_code == 401 and anonymous.headers["www-authenticate"] == challenge(REPO)
    wrong_scope = await hub.request(method, url, headers=await hub.pull_token(OTHER))
    assert wrong_scope.status_code == 401
    assert wrong_scope.headers["www-authenticate"] == challenge(REPO, insufficient=True)
    reader = await hub.request(method, url, headers=await hub.pull_token(REPO))
    assert reader.status_code == 404
    if method == "GET":
        assert anonymous.json() == UNAUTHORIZED
        assert reader.json()["errors"][0]["code"] == "BLOB_UNKNOWN"
    assert hub.upstream.requests == [], "nothing was asked of the backing registry"


async def test_publish_is_not_a_scope_the_credential_exchange_knows(hub: Hub):
    """Refused by validation, like any scope that does not exist: the same status, code and details."""

    refused = await hub.request("POST", "/v1/cogs/registry-credentials", headers=ALICE, json={"scope": "publish"})
    assert refused.status_code == 422
    assert refused.json() == {
        "error": {
            "code": "validation_error",
            "message": "Request validation failed",
            "details": [
                {
                    "type": "literal_error",
                    "loc": ["body", "scope"],
                    "msg": "Input should be 'pull'",
                    "input": "publish",
                    "ctx": {"expected": "'pull'"},
                }
            ],
        }
    }
    assert (await hub.exchange(ALICE))["scope"] == "pull"


def literal_error(given) -> dict:
    return {
        "type": "literal_error",
        "loc": ["body", "scope"],
        "msg": "Input should be 'pull'",
        "input": given,
        "ctx": {"expected": "'pull'"},
    }


@pytest.mark.parametrize(
    "body, details",
    [
        # The only scope it advertises is the only one it has.
        ({"scope": "bogus"}, [literal_error("bogus")]),
        # Every problem is listed, in the pull-only contract's order: the scope is one of them.
        (
            {"scope": "publish", "extra": True},
            [
                literal_error("publish"),
                {"type": "extra_forbidden", "loc": ["body", "extra"], "msg": "Extra inputs are not permitted",
                 "input": True},
            ],
        ),
        (
            {"scope": "pull", "extra": 1},
            [{"type": "extra_forbidden", "loc": ["body", "extra"], "msg": "Extra inputs are not permitted",
              "input": 1}],
        ),
        ({"scope": None}, [literal_error(None)]),
    ],
)
async def test_every_validation_answer_of_the_exchange_is_the_pull_only_contracts(hub: Hub, body, details):
    refused = await hub.request("POST", "/v1/cogs/registry-credentials", headers=ALICE, json=body)
    assert refused.status_code == 422
    assert refused.json() == {
        "error": {"code": "validation_error", "message": "Request validation failed", "details": details}
    }


async def test_a_body_that_is_not_an_object_is_refused_as_it_always_was(hub: Hub):
    for content in (b"[1]", b'"pull"', b"{not json"):
        refused = await hub.request(
            "POST",
            "/v1/cogs/registry-credentials",
            headers={**ALICE, "Content-Type": "application/json"},
            content=content,
        )
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_error", content
        assert "publish" not in refused.text


async def test_every_write_is_refused_as_read_only_before_anything_else(hub: Hub):
    hub.seed(REPO, ALPHA, "v1")
    reader = await hub.pull_token(REPO)
    asked_to_push = await token_for(hub, f"repository:{REPO}:pull,push")
    digest = ALPHA.digest
    for method, path in (
        ("POST", f"/v2/{REPO}/blobs/uploads/"),
        ("POST", f"/v2/{REPO}/blobs/uploads/?digest={digest}"),
        ("PATCH", f"/v2/{REPO}/blobs/uploads/up-1"),
        ("PUT", f"/v2/{REPO}/blobs/uploads/up-1?digest={digest}"),
        ("DELETE", f"/v2/{REPO}/blobs/uploads/up-1"),
        ("PUT", f"/v2/{REPO}/manifests/v2"),
        ("PUT", f"/v2/{REPO}/manifests/{digest}"),
        ("DELETE", f"/v2/{REPO}/manifests/v1"),
        ("DELETE", f"/v2/{REPO}/blobs/{digest}"),
        ("POST", "/v2/"),
        ("PUT", "/v2"),
        ("PATCH", "/v2/not/a/registry/path"),
    ):
        for headers in ({}, reader, asked_to_push, ALICE):
            response = await hub.request(method, path, headers=headers, content=b"x")
            assert (response.status_code, response.json()) == (405, READ_ONLY), (method, path)
            assert "www-authenticate" not in response.headers, (method, path)
    assert hub.upstream.requests == []
