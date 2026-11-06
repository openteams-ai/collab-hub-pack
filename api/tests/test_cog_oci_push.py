"""The push half of the generic OCI client (issue #180), against an in-process registry.

What the Hub's publisher relies on: the registry's credential is presented
only to the registry's own origin; a write is never redirected; a streamed
body is never sent before the credential is in hand (it could not be sent
again); and nothing a registry says in a response body is kept.
"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from collab_hub_api.cogs.oci import (
    BasicCredentials,
    OCIAuthError,
    OCIClient,
    OCIError,
    OCIInvalidReference,
    OCIProtocolError,
    OCIRejected,
    OCITransportError,
)

REGISTRY = "https://registry.example"
REPO = "cogs/alpha"
CREDS = BasicCredentials("robot", "robot-secret")
TOKEN = "push-token"
SESSION = f"{REGISTRY}/v2/{REPO}/blobs/uploads/abc?_state=opaque"


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Registry:
    """A token-authenticated registry that records what it was sent."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bool, bytes]] = []
        self.answers: dict[str, httpx.Response] = {}
        self.token_requests: list[str] = []
        self.auth = "bearer"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        authorized = request.headers.get("authorization") in (f"Bearer {TOKEN}", CREDS.header())
        if request.url.path == "/token":
            self.token_requests.append(request.url.params.get("scope", ""))
            if request.headers.get("authorization") != CREDS.header():
                return httpx.Response(401)
            return httpx.Response(200, json={"token": TOKEN, "expires_in": 300})
        self.seen.append((request.method, str(request.url), authorized, body))
        if not authorized:
            if self.auth == "basic":
                return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="registry"'})
            scope = f"repository:{REPO}:pull,push"
            challenge = f'Bearer realm="{REGISTRY}/token",service="registry",scope="{scope}"'
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        key = f"{request.method} {request.url.path}"
        if key in self.answers:
            return self.answers[key]
        if request.method == "POST":
            return httpx.Response(202, headers={"Location": f"/v2/{REPO}/blobs/uploads/abc?_state=opaque"})
        if request.method == "PATCH":
            return httpx.Response(202, headers={"Location": SESSION + "2"})
        if request.method == "GET":
            return httpx.Response(204)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "42"})
        return httpx.Response(201)


def client_for(registry: Registry, **kwargs) -> OCIClient:
    return OCIClient(REGISTRY, credentials=CREDS, transport=httpx.MockTransport(registry), **kwargs)


async def stream(*chunks: bytes):
    for chunk in chunks:
        yield chunk


async def test_a_blob_goes_up_in_a_session_with_the_registrys_credential():
    registry = Registry()
    async with client_for(registry) as client:
        location = await client.start_upload(REPO)
        assert location == SESSION, "a relative Location is resolved against the registry"
        moved = await client.upload_chunk(REPO, location, stream(b"hello ", b"world"), offset=0, length=11)
        assert moved == SESSION + "2"
        await client.finish_upload(REPO, moved, sha256(b"hello world"))
        await client.finish_upload(REPO, location, sha256(b"x"), stream(b"x"), length=1)
        await client.finish_upload(REPO, location, sha256(b"y"), stream(b"y"), length=None)
    sent = [(method, url, body) for method, url, authorized, body in registry.seen if authorized]
    assert sent[0] == ("POST", f"{REGISTRY}/v2/{REPO}/blobs/uploads/", b"")
    assert sent[1] == ("PATCH", SESSION, b"hello world")
    # The digest is added to the session's own URL, keeping whatever state the registry put there.
    assert sent[2] == ("PUT", f"{SESSION}2&digest={sha256(b'hello world').replace(':', '%3A')}", b"")
    assert sent[3][2] == b"x" and sent[4][2] == b"y"
    assert registry.token_requests == [f"repository:{REPO}:pull,push"], "one token, asked for with the push scope"
    patch = next(request for request in registry.seen if request[0] == "PATCH")
    assert patch[2] is True


async def test_a_streamed_body_is_never_sent_before_the_credential_is_in_hand():
    """No token cached: the challenge is taken on a bodiless status request, then the body is sent once."""

    registry = Registry()
    async with client_for(registry) as client:
        await client.upload_chunk(REPO, SESSION, stream(b"payload"), offset=0, length=7)
    bodies = [(method, authorized, body) for method, _url, authorized, body in registry.seen]
    assert bodies == [("GET", False, b""), ("GET", True, b""), ("PATCH", True, b"payload")]
    # And a registry that answers 401 to the streamed request anyway is an auth failure, not a silent resend.
    registry = Registry()
    registry.answers[f"PATCH /v2/{REPO}/blobs/uploads/abc"] = httpx.Response(401, text="expired")
    async with client_for(registry) as client:
        with pytest.raises(OCIAuthError, match="refused the Hub's credential"):
            await client.upload_chunk(REPO, SESSION, stream(b"payload"), offset=0, length=7)
    assert [body for method, _u, _a, body in registry.seen if method == "PATCH"] == [b"payload"]


async def test_basic_auth_registries_are_pushed_to_as_well():
    registry = Registry()
    registry.auth = "basic"
    async with client_for(registry) as client:
        location = await client.start_upload(REPO)
        await client.finish_upload(REPO, location, sha256(b"z"), stream(b"z"), length=1)
        await client.put_manifest(REPO, "v1", b"{}", "application/vnd.oci.image.manifest.v1+json")
    assert registry.token_requests == []
    assert base64.b64decode(CREDS.header().split()[1]) == b"robot:robot-secret"


async def test_a_manifest_is_put_under_a_tag_or_a_digest_and_replayed_after_the_challenge():
    registry = Registry()
    body = b'{"schemaVersion": 2}'
    async with client_for(registry) as client:
        await client.put_manifest(REPO, "v1", body, "application/vnd.oci.image.manifest.v1+json")
        await client.put_manifest(REPO, sha256(body), body, "application/vnd.oci.image.manifest.v1+json")
        for bad in ("not a ref", "sha256:short"):
            with pytest.raises(OCIInvalidReference):
                await client.put_manifest(REPO, bad, body, "x")
        with pytest.raises(OCIInvalidReference):
            await client.put_manifest("Not/Valid", "v1", body, "x")
    puts = [(url, authorized, sent) for method, url, authorized, sent in registry.seen if method == "PUT"]
    assert puts[0] == (f"{REGISTRY}/v2/{REPO}/manifests/v1", False, body), "the first attempt met the challenge"
    assert puts[1] == (f"{REGISTRY}/v2/{REPO}/manifests/v1", True, body), "and the bytes were sent again"
    assert len(puts) == 3


@pytest.mark.parametrize(
    ("status", "error", "attribute"),
    [
        (400, OCIRejected, 400),
        (404, OCIRejected, 404),
        (416, OCIRejected, 416),
        (403, OCIAuthError, None),
        (500, OCIProtocolError, None),
        (307, OCIProtocolError, None),
    ],
)
async def test_what_a_registry_refuses_is_reported_by_status_and_never_by_its_words(status, error, attribute):
    registry = Registry()
    headers = {"Location": "https://elsewhere.example/upload"} if status == 307 else {}
    answer = httpx.Response(status, text="internal detail from registry.example with a secret", headers=headers)
    for key in ("PATCH", "PUT", "POST"):
        registry.answers[f"{key} /v2/{REPO}/blobs/uploads/abc"] = answer
        registry.answers[f"{key} /v2/{REPO}/blobs/uploads/"] = answer
        registry.answers[f"{key} /v2/{REPO}/manifests/v1"] = answer
    async with client_for(registry) as client:
        calls = (
            lambda: client.start_upload(REPO),
            lambda: client.upload_chunk(REPO, SESSION, stream(b"x"), offset=0, length=1),
            lambda: client.finish_upload(REPO, SESSION, sha256(b"x")),
            lambda: client.put_manifest(REPO, "v1", b"{}", "application/json"),
        )
        for call in calls:
            with pytest.raises(error) as caught:
                await call()
            assert "secret" not in str(caught.value) and "registry.example" not in str(caught.value)
            if attribute is not None:
                assert caught.value.status == attribute
    assert not any("elsewhere.example" in url for _m, url, _a, _b in registry.seen), "a write is never redirected"


async def test_upload_locations_must_stay_on_the_registrys_origin():
    registry = Registry()
    registry.answers[f"POST /v2/{REPO}/blobs/uploads/"] = httpx.Response(
        202, headers={"Location": "https://evil.example/v2/cogs/alpha/blobs/uploads/abc"}
    )
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="off its own origin"):
            await client.start_upload(REPO)
        registry.answers[f"POST /v2/{REPO}/blobs/uploads/"] = httpx.Response(202)
        with pytest.raises(OCIProtocolError, match="named no upload location"):
            await client.start_upload(REPO)
        # A stored session URL that is not the registry's is never written to (or handed the credential).
        for call in (
            lambda: client.upload_chunk(REPO, "https://evil.example/upload", stream(b"x"), offset=0, length=1),
            lambda: client.finish_upload(REPO, "https://evil.example/upload", sha256(b"x")),
        ):
            with pytest.raises(OCIProtocolError, match="not on the registry's origin"):
                await call()
        await client.cancel_upload(REPO, "https://evil.example/upload")  # best effort: swallowed
    assert not any("evil.example" in url for _m, url, _a, _b in registry.seen)


async def test_blob_size_answers_only_what_the_registry_states():
    registry = Registry()
    digest = sha256(b"x")
    async with client_for(registry) as client:
        assert await client.blob_size(REPO, digest) == 42
        registry.answers[f"HEAD /v2/{REPO}/blobs/{digest}"] = httpx.Response(404)
        assert await client.blob_size(REPO, digest) is None
        registry.answers[f"HEAD /v2/{REPO}/blobs/{digest}"] = httpx.Response(200)
        assert await client.blob_size(REPO, digest) is None, "no usable length is no answer"
        registry.answers[f"HEAD /v2/{REPO}/blobs/{digest}"] = httpx.Response(200, headers={"Content-Length": "nope"})
        assert await client.blob_size(REPO, digest) is None
        with pytest.raises(OCIInvalidReference):
            await client.blob_size(REPO, "latest")


async def test_has_tags_is_one_request_and_reads_no_into_nothing_but_a_clear_answer():
    answers: list[httpx.Response] = []
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return answers.pop(0)

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        for answer, expected in (
            (httpx.Response(200, json={"name": REPO, "tags": ["v1"]}, headers={"Link": '</v2/x>; rel="next"'}), True),
            (httpx.Response(200, json={"name": REPO, "tags": []}), False),
            (httpx.Response(200, json={"name": REPO, "tags": None}), False),
            (httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]}), False),
        ):
            answers.append(answer)
            assert await client.has_tags(REPO) is expected
        assert seen == [f"{REGISTRY}/v2/{REPO}/tags/list?n=1"] * 4, "one request each, never a second page"
        # "Nothing there" is only ever read from an answer that says so. A document that is not this
        # repository's tag list is not an answer, whatever its status.
        for unclear in (
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={}),
            httpx.Response(200, json={"name": REPO}),
            httpx.Response(200, json={"tags": []}),
            httpx.Response(200, json={"name": "cogs/another", "tags": []}),
            httpx.Response(200, json={"name": REPO, "tags": "v1"}),
            httpx.Response(200, json={"name": REPO, "tags": {}}),
            httpx.Response(200, json=[]),
            httpx.Response(200, text="not json"),
        ):
            answers.append(unclear)
            with pytest.raises(OCIError):
                await client.has_tags(REPO)
        # The listing other callers use stays as tolerant as it was.
        answers.append(httpx.Response(200, json={}))
        assert await client.list_tags(REPO) == []
        with pytest.raises(OCIInvalidReference):
            await client.has_tags("Not/Valid")


async def test_cancel_says_whether_the_session_is_gone_and_transport_failures_are_wrapped():
    registry = Registry()
    async with client_for(registry) as client:
        assert await client.cancel_upload(REPO, SESSION) is True
        assert ("DELETE", SESSION, True, b"") in registry.seen
        # A session the registry no longer knows is as gone as one it just dropped; anything else is not.
        for status, gone in ((204, True), (404, True), (500, False), (405, False), (403, False)):
            registry.answers[f"DELETE /v2/{REPO}/blobs/uploads/abc"] = httpx.Response(status, text="whatever")
            assert await client.cancel_upload(REPO, SESSION) is gone, status
        assert await client.cancel_upload(REPO, "https://elsewhere.example/v2/x/blobs/uploads/abc") is False

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot reach registry.example", request=request)

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(unreachable)) as client:
        with pytest.raises(OCITransportError) as caught:
            await client.start_upload(REPO)
        assert str(caught.value) == "upload start: ConnectError"
        assert await client.cancel_upload(REPO, SESSION) is False  # swallowed, and reported as not gone


async def test_a_write_is_logged_as_its_operation_and_source_and_a_read_as_before(caplog):
    """httpx logs every request's URL at INFO. For a write that is the upload session: host, path and state."""

    import logging

    from collab_hub_api.cogs import oci

    oci.install_log_redaction()
    registry = Registry()
    caplog.set_level(logging.INFO, logger="httpx")
    async with client_for(registry, name="backing") as client:
        location = await client.start_upload(REPO)
        client._tokens.clear()  # no token in hand: the challenge is taken on a status request to the session
        await client.upload_chunk(REPO, location, stream(b"payload"), offset=0, length=7)
        await client.finish_upload(REPO, location, sha256(b"payload"))
        await client.cancel_upload(REPO, location)
        await client.put_manifest(REPO, "1.0.0", b"{}", "application/vnd.oci.image.manifest.v1+json")
        await client.blob_size(REPO, sha256(b"payload"))
    async with client_for(registry) as unnamed:
        await unnamed.start_upload(REPO)
    lines = [record.getMessage() for record in caplog.records if record.name == "httpx"]
    assert [line for line in lines if "/token" not in line] == [
        'HTTP Request: POST [registry write: upload start, source backing] "HTTP/1.1 401 Unauthorized"',
        'HTTP Request: POST [registry write: upload start, source backing] "HTTP/1.1 202 Accepted"',
        'HTTP Request: GET [registry write: upload status, source backing] "HTTP/1.1 401 Unauthorized"',
        'HTTP Request: GET [registry write: upload status, source backing] "HTTP/1.1 204 No Content"',
        'HTTP Request: PATCH [registry write: upload chunk, source backing] "HTTP/1.1 202 Accepted"',
        'HTTP Request: PUT [registry write: upload finish, source backing] "HTTP/1.1 201 Created"',
        'HTTP Request: DELETE [registry write: upload cancel, source backing] "HTTP/1.1 201 Created"',
        'HTTP Request: PUT [registry write: manifest put, source backing] "HTTP/1.1 201 Created"',
        'HTTP Request: HEAD [registry write: blob check, source backing] "HTTP/1.1 401 Unauthorized"',
        'HTTP Request: HEAD [registry write: blob check, source backing] "HTTP/1.1 200 OK"',
        'HTTP Request: POST [registry write: upload start, source registry] "HTTP/1.1 401 Unauthorized"',
        'HTTP Request: POST [registry write: upload start, source registry] "HTTP/1.1 202 Accepted"',
    ]
    text = "\n".join(lines)
    assert "abc" not in text and "_state" not in text and "opaque" not in text, "no session path, no session state"
    # The token endpoint is a read, logged as reads always were; and once the write is over, so is everything else.
    assert f"HTTP Request: GET {REGISTRY}/token" in text
    assert oci._registry_write.get() is None


async def test_an_undeclared_length_is_sent_chunked_and_a_declared_one_with_its_range():
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        captured.append(request)
        return httpx.Response(202, headers={"Location": SESSION})

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        client._use_basic = True
        client._credentials = CREDS
        await client.upload_chunk(REPO, SESSION, stream(b"abc"), offset=5, length=3)
        await client.upload_chunk(REPO, SESSION, stream(b"abc"), offset=5, length=None)
        await client.upload_chunk(REPO, SESSION, stream(), offset=5, length=0)
    declared, undeclared, empty = captured
    assert declared.headers["content-length"] == "3" and declared.headers["content-range"] == "5-7"
    assert "content-length" not in undeclared.headers and undeclared.headers["transfer-encoding"] == "chunked"
    assert "content-range" not in undeclared.headers and "content-range" not in empty.headers
