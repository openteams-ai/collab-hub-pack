"""The streamed reads the Hub's ``/v2/`` surface is built on (issue #179).

``OCIClient.fetch_manifest`` returns a manifest as stored (an index is not
followed), and ``OCIClient.open_blob`` returns a :class:`BlobStream` whose
bytes are hashed as they pass. The property under test for the stream is the
one the relay depends on: a body that is wrong -- too long, too short, the
wrong bytes, cut off -- never has its last chunk released.
"""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest

from collab_hub_api.cogs import oci
from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_OCI_INDEX,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_CONFIG,
    BlobStream,
    OCIClient,
    OCIDigestMismatch,
    OCIInvalidReference,
    OCINotFound,
    OCIProtocolError,
    OCITooLarge,
    OCITransportError,
    index_children,
    is_index_manifest,
    is_sha256_digest,
    is_tag,
)

REGISTRY = "https://registry.example"
REPO = "cogs/alpha"
CHUNK = oci._STREAM_CHUNK_BYTES


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


BODY = bytes(range(256)) * 1024  # 256 KiB: four stream chunks
DIGEST = sha256(BODY)


def client_for(handler) -> OCIClient:
    return OCIClient(REGISTRY, transport=httpx.MockTransport(handler))


def blob_handler(body: bytes = BODY, *, headers: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return handler


async def drain(stream: BlobStream, **kwargs) -> tuple[bytes, BaseException | None]:
    """Everything the stream released, and what ended it (``None`` for a clean end)."""

    received = bytearray()
    try:
        async for chunk in stream.iter_verified(**kwargs):
            received.extend(chunk)
    except oci.OCIError as exc:
        return bytes(received), exc
    return bytes(received), None


async def test_a_good_blob_streams_whole_in_chunks():
    async with client_for(blob_handler()) as client:
        stream = await client.open_blob(REPO, DIGEST)
        assert stream.digest == DIGEST and stream.content_length == len(BODY)
        chunks = [chunk async for chunk in stream.iter_verified(max_bytes=len(BODY), expected_size=len(BODY))]
    assert b"".join(chunks) == BODY
    assert len(chunks) == 4 and all(len(chunk) <= CHUNK for chunk in chunks), "relayed chunk by chunk, not buffered"


async def test_an_empty_blob_streams_nothing():
    async with client_for(blob_handler(b"")) as client:
        stream = await client.open_blob(REPO, sha256(b""))
        assert await drain(stream, max_bytes=10, expected_size=0) == (b"", None)


@pytest.mark.parametrize(
    ("body", "kwargs", "error", "match"),
    [
        # Same length, one byte different: only the hash can tell.
        (BODY[:-1] + b"\x00", {"max_bytes": len(BODY), "expected_size": len(BODY)}, OCIDigestMismatch, "hashes to"),
        (BODY + b"x", {"max_bytes": len(BODY) * 2, "expected_size": len(BODY)}, OCIDigestMismatch, "longer than"),
        (BODY[:-1], {"max_bytes": len(BODY), "expected_size": len(BODY)}, OCIDigestMismatch, "declared"),
        (BODY, {"max_bytes": len(BODY) - 1}, OCITooLarge, "exceeds the"),
    ],
)
async def test_a_wrong_blob_never_has_its_last_chunk_released(body, kwargs, error, match):
    async with client_for(blob_handler(body)) as client:
        stream = await client.open_blob(REPO, DIGEST)
        received, raised = await drain(stream, **kwargs)
    assert isinstance(raised, error) and match in str(raised)
    assert len(received) <= len(body) - 1 and len(received) < len(BODY)
    assert BODY.startswith(received)


async def test_a_single_chunk_blob_that_is_wrong_releases_nothing():
    async with client_for(blob_handler(b"tampered")) as client:
        stream = await client.open_blob(REPO, sha256(b"original"))
        assert (await drain(stream, max_bytes=100))[0] == b""


async def test_a_connection_cut_mid_body_is_a_transport_error():
    class Cut(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield BODY[:CHUNK]
            yield BODY[CHUNK : 2 * CHUNK]
            raise httpx.ReadError("connection reset by registry.example")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Cut())

    async with client_for(handler) as client:
        stream = await client.open_blob(REPO, DIGEST)
        assert stream.content_length is None
        received, raised = await drain(stream, max_bytes=len(BODY))
    assert isinstance(raised, OCITransportError)
    # The httpx class, never its message: that one names the host.
    assert str(raised) == f"blob {DIGEST}: ReadError while reading"
    assert received == BODY[:CHUNK]


async def test_an_encoded_body_is_refused_before_any_byte():
    class Raw(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield BODY

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Raw(), headers={"Content-Encoding": "gzip"})

    async with client_for(handler) as client:
        stream = await client.open_blob(REPO, DIGEST)
        received, raised = await drain(stream, max_bytes=len(BODY))
    assert received == b"" and isinstance(raised, OCIProtocolError) and "Content-Encoding" in str(raised)


async def test_the_stream_is_closed_however_it_ends():
    async with client_for(blob_handler()) as client:
        stream = await client.open_blob(REPO, DIGEST)
        iterator = stream.iter_verified(max_bytes=len(BODY))
        assert await anext(iterator)
        await iterator.aclose()  # a client that went away
        assert stream._response.is_closed
        await stream.aclose()  # idempotent
        unread = await client.open_blob(REPO, DIGEST)
        await unread.aclose()
        assert unread._response.is_closed


async def test_open_blob_follows_redirects_and_maps_statuses():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.example":
            return httpx.Response(200, content=BODY)
        if request.url.path.endswith(DIGEST):
            return httpx.Response(307, headers={"Location": f"https://storage.example/{DIGEST}?sig=abc"})
        if request.url.path.endswith("a" * 64):
            return httpx.Response(500, text="registry.example exploded")
        return httpx.Response(404, text="nope")

    async with client_for(handler) as client:
        stream = await client.open_blob(REPO, DIGEST)
        assert (await drain(stream, max_bytes=len(BODY), expected_size=len(BODY))) == (BODY, None)
        with pytest.raises(OCINotFound):
            await client.open_blob(REPO, "sha256:" + "b" * 64)
        with pytest.raises(OCIProtocolError, match="HTTP 500") as caught:
            await client.open_blob(REPO, "sha256:" + "a" * 64)
        assert "exploded" not in str(caught.value) and "registry.example" not in str(caught.value)
        for bad in ("latest", "sha256:short", "md5:" + "a" * 32):
            with pytest.raises(OCIInvalidReference):
                await client.open_blob(REPO, bad)
        with pytest.raises(OCIInvalidReference):
            await client.open_blob("Not/Valid", DIGEST)


def test_content_length_is_read_only_when_usable():
    assert BlobStream(httpx.Response(200, headers={"Content-Length": "12"}), DIGEST).content_length == 12
    assert BlobStream(httpx.Response(200, headers={"Content-Length": "-1"}), DIGEST).content_length is None
    assert BlobStream(httpx.Response(200, headers={"Content-Length": "many"}), DIGEST).content_length is None


def _index(children: list[dict] | None) -> bytes:
    document: dict = {"schemaVersion": 2, "mediaType": MEDIA_TYPE_OCI_INDEX}
    if children is not None:
        document["manifests"] = children
    return json.dumps(document).encode()


async def test_fetch_manifest_returns_an_index_as_stored():
    child = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": {"mediaType": MEDIA_TYPE_PIXI_CONFIG, "digest": sha256(b"{}"), "size": 2},
            "layers": [],
        }
    ).encode()
    index = _index([{"mediaType": MEDIA_TYPE_OCI_MANIFEST, "digest": sha256(child), "size": len(child)}])
    bodies = {sha256(index): index, "multi": index, sha256(child): child}

    def handler(request: httpx.Request) -> httpx.Response:
        ref = request.url.path.rsplit("/", 1)[-1]
        if ref not in bodies:
            return httpx.Response(404)
        return httpx.Response(200, content=bodies[ref], headers={"Docker-Content-Digest": sha256(bodies[ref])})

    async with client_for(handler) as client:
        for ref in ("multi", sha256(index)):
            fetched = await client.fetch_manifest(REPO, ref)
            assert fetched.raw == index and fetched.digest == sha256(index), "not resolved to a child"
            assert is_index_manifest(fetched)
            assert [entry.digest for entry in index_children(fetched)] == [sha256(child)]
        image = await client.fetch_manifest(REPO, sha256(child))
        assert not is_index_manifest(image) and index_children(image) == []
        with pytest.raises(OCINotFound):
            await client.fetch_manifest(REPO, "missing")
        with pytest.raises(OCIDigestMismatch):
            # Asked for one digest, handed another manifest's bytes.
            bodies["sha256:" + "c" * 64] = child
            await client.fetch_manifest(REPO, "sha256:" + "c" * 64)
        with pytest.raises(OCIInvalidReference):
            await client.fetch_manifest(REPO, "not a ref")
        with pytest.raises(OCIInvalidReference):
            await client.fetch_manifest("Not/Valid", "latest")


def test_index_children_refuses_an_index_without_a_list():
    broken = oci._parse_manifest(_index(None), "sha256:" + "0" * 64, content_type_fallback="")
    with pytest.raises(OCIProtocolError, match="no 'manifests' list"):
        index_children(broken)


def test_the_reference_grammar_helpers():
    assert is_tag("latest") and is_tag("1.0.0-rc_1") and not is_tag("has space") and not is_tag(None)
    assert not is_tag("sha256:" + "a" * 64)
    assert is_sha256_digest("sha256:" + "a" * 64)
    assert not is_sha256_digest("sha256:" + "A" * 64) and not is_sha256_digest("sha512:" + "a" * 128)
    assert not is_sha256_digest(None)


# -- redirects ------------------------------------------------------------------


def policed(handler, registry: str = REGISTRY) -> OCIClient:
    """A client built the way serving builds its own: with the redirect policy on."""

    return OCIClient(registry, transport=httpx.MockTransport(handler), restrict_redirects=True)


def redirecting(location: str, *, registry: str = REGISTRY):
    """A registry that redirects every blob to ``location`` and records which hosts were then asked."""

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url.copy_with(query=None)))
        if "/v2/" in request.url.path and request.url.host == httpx.URL(registry).host:
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(200, content=BODY)

    return handler, asked


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1:9000/blob",
        "https://[::1]/blob",
        "https://[fe80::1]/blob",
        "https://[::ffff:169.254.169.254]/blob",
        "https://[::ffff:127.0.0.1]/blob",
        "https://0.0.0.0/blob",
    ],
)
async def test_a_redirect_to_a_loopback_or_link_local_address_is_never_followed(location):
    handler, asked = redirecting(location)
    async with policed(handler) as client:
        with pytest.raises(OCIProtocolError, match="refusing a redirect") as caught:
            await client.open_blob(REPO, DIGEST)
    assert len(asked) == 1, "the destination was never contacted"
    assert "169.254" not in str(caught.value) and "127.0.0.1" not in str(caught.value)


@pytest.mark.parametrize(
    "location",
    [
        # The same loopback address, written the ways a resolver accepts and a strict parser does not.
        "http://2130706433/latest",
        "http://127.1/latest",
        "http://0x7f000001/latest",
        "http://0x7f.0.0.1/latest",
        "http://0177.0.0.1/latest",
        "http://127.0.0.1./latest",
        "http://2852039166/latest",  # 169.254.169.254
        "http://169.254.43518/latest",
        "http://0/latest",
        "http://localhost/latest",
        "http://localhost./latest",
        "http://metadata.localhost:8080/latest",
        "http://1.2.3./latest",
    ],
)
async def test_numeric_and_local_host_forms_are_refused_through_the_redirect_path(location):
    """Not just the classifier: the registry redirects there, and the destination is never contacted."""

    handler, asked = redirecting(location, registry="http://registry.example")
    async with policed(handler, "http://registry.example") as client:
        # Refused by this client's own rule or, for a form httpx will not even parse, by httpx; never followed.
        with pytest.raises(oci.OCIError) as caught:
            await client.open_blob(REPO, DIGEST)
    assert "refusing a redirect" in str(caught.value) or isinstance(caught.value, OCITransportError)
    assert len(asked) == 1, asked


def test_redirect_hosts_are_canonical_addresses_or_real_names():
    kind = oci._redirect_host_kind
    for host in ("storage.example.com", "s3.amazonaws.com", "8.8.8.8", "2606:4700::1111", "7f.example"):
        assert kind(host) == "ok", host
    # Not globally routable: followed only to an address the operator listed.
    for host in ("10.0.4.7", "192.168.1.9", "172.16.0.1", "100.64.1.1", "fd12:3456::1", "::ffff:10.0.0.1"):
        assert kind(host) == "private", host
    for host in ("minio", "minio.storage.svc.cluster.local", "0x7f.example", "1.2.3.4.example"):
        assert kind(host) == "ok", host
    for host in ("127.0.0.1", "169.254.169.254", "::1", "fe80::1", "::ffff:127.0.0.1", "0.0.0.0", "::", "ff02::1"):
        assert kind(host) == "forbidden", host
    # Metadata endpoints no range check names: AWS over IPv6 is unique-local,
    # Alibaba is carrier-grade NAT space, Azure's is a public address.
    for host in ("fd00:ec2::254", "100.100.100.200", "168.63.129.16", "192.0.0.192"):
        assert kind(host) == "forbidden", host
    # The same destinations written inside an IPv6 literal: IPv4-mapped, NAT64, 6to4.
    for host in ("::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1", "::ffff:100.100.100.200"):
        assert kind(host) == "forbidden", host
    for host in ("localhost", "a.localhost"):
        assert kind(host) == "forbidden", host
    for host in ("2130706433", "127.1", "0x7f000001", "0177.0.0.1", "1.2.3", "0x", "1.2.3.4.5", "", "::g"):
        assert kind(host) == "ambiguous", host


async def test_a_chain_cannot_be_led_back_down_to_an_http_registry():
    """http registry -> https storage -> the http registry again: the last hop is a downgrade, origin or not."""

    seen: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), "authorization" in request.headers))
        if request.url.host == "storage.example":
            return httpx.Response(302, headers={"Location": "http://registry.example/v2/internal/blob"})
        if request.url.path.endswith("/internal/blob"):
            return httpx.Response(200, content=BODY)
        if "authorization" not in request.headers:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="r"'})
        return httpx.Response(307, headers={"Location": "https://storage.example/hop"})

    client = OCIClient(
        "http://registry.example",
        credentials=oci.BasicCredentials("robot", "secret"),
        transport=httpx.MockTransport(handler),
        restrict_redirects=True,
    )
    async with client:
        with pytest.raises(OCIProtocolError, match="from https to http"):
            await client.open_blob(REPO, DIGEST)
    assert [url for url, _ in seen][-1] == "https://storage.example/hop", "the third hop was never made"
    assert seen[-1][1] is False, "and the credential did not go to storage"


async def test_a_redirect_never_downgrades_https_to_http():
    handler, asked = redirecting("http://storage.example/blob")
    async with policed(handler) as client:
        with pytest.raises(OCIProtocolError, match="from https to http"):
            await client.open_blob(REPO, DIGEST)
    assert len(asked) == 1
    # A registry reached over http may redirect to http: nothing is downgraded.
    handler, asked = redirecting("http://storage.example/blob", registry="http://registry.example")
    async with policed(handler, "http://registry.example") as client:
        stream = await client.open_blob(REPO, DIGEST)
        await stream.aclose()
    assert asked[-1] == "http://storage.example/blob"


async def test_internal_names_and_the_registry_itself_are_allowed_without_an_allowlist():
    for location in ("https://minio.storage.svc.cluster.local/blob", "/v3/elsewhere"):
        handler, asked = redirecting(location)
        async with policed(handler) as client:
            stream = await client.open_blob(REPO, DIGEST)
            await stream.aclose()
        assert len(asked) == 2, location
    # A loopback registry redirecting within its own origin is the registry, not a destination.
    local = "http://127.0.0.1:5000"
    handler, asked = redirecting("/blobstore/x", registry=local)
    async with OCIClient(local, transport=httpx.MockTransport(handler), restrict_redirects=True) as client:
        stream = await client.open_blob(REPO, DIGEST)
        await stream.aclose()
    assert asked[-1] == f"{local}/blobstore/x"


async def test_an_allowlist_is_enforced_strictly_when_set():
    allowed = ("storage.example.com", ".s3.amazonaws.com")

    async def follow(location: str) -> int:
        handler, asked = redirecting(location)
        client = OCIClient(REGISTRY, transport=httpx.MockTransport(handler), redirect_hosts=allowed)
        async with client:
            stream = await client.open_blob(REPO, DIGEST)
            await stream.aclose()
        return len(asked)

    assert await follow("https://storage.example.com/blob?sig=1") == 2
    assert await follow("https://bucket.s3.amazonaws.com/blob") == 2
    assert await follow("https://STORAGE.example.com/blob") == 2, "hosts compare case-insensitively"
    assert await follow("/same-origin") == 2, "the registry's own origin needs no entry"
    for location in (
        "https://evil.example.com/blob",
        "https://storage.example.com.evil.example/blob",
        "https://s3.amazonaws.com/blob",  # the suffix's own apex is not a subdomain of it
        "https://evils3.amazonaws.com.example/blob",
        "https://10.0.4.7/blob",
    ):
        with pytest.raises(OCIProtocolError, match="not in blob_redirect_hosts"):
            await follow(location)
    # The allowlist cannot re-admit what is always refused.
    handler, _ = redirecting("https://127.0.0.1/blob")
    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler), redirect_hosts=("127.0.0.1",)) as client:
        with pytest.raises(OCIProtocolError, match="loopback, link-local or metadata"):
            await client.open_blob(REPO, DIGEST)


@pytest.mark.parametrize(
    "location",
    ["http://[fd00:ec2::254]/latest/meta-data/", "http://100.100.100.200/latest/meta-data/"],
)
async def test_a_metadata_endpoint_outside_the_link_local_range_is_never_followed(location):
    handler, asked = redirecting(location, registry="http://registry.example")
    async with policed(handler, "http://registry.example") as client:
        with pytest.raises(OCIProtocolError, match="loopback, link-local or metadata"):
            await client.open_blob(REPO, DIGEST)
    assert len(asked) == 1, "the metadata endpoint was never asked"


async def test_a_private_address_is_followed_only_when_the_operator_listed_it():
    handler, asked = redirecting("https://10.0.4.7/blob")
    async with policed(handler, REGISTRY) as client:
        with pytest.raises(OCIProtocolError, match="private address that is not in blob_redirect_hosts"):
            await client.open_blob(REPO, DIGEST)
    assert len(asked) == 1

    handler, asked = redirecting("https://10.0.4.7/blob")
    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler), redirect_hosts=("10.0.4.7",)) as client:
        stream = await client.open_blob(REPO, DIGEST)
        await stream.aclose()
    assert len(asked) == 2

    # Listed means that address: a suffix entry that happens to end the same way does not admit it.
    handler, asked = redirecting("https://10.0.4.7/blob")
    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler), redirect_hosts=(".4.7",)) as client:
        with pytest.raises(OCIProtocolError, match="private address"):
            await client.open_blob(REPO, DIGEST)


async def test_every_hop_of_a_redirect_chain_is_checked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "registry.example":
            return httpx.Response(307, headers={"Location": "https://storage.example/one"})
        if request.url.path == "/one":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
        return httpx.Response(200, content=BODY)

    async with policed(handler) as client:
        with pytest.raises(OCIProtocolError, match="refusing a redirect"):
            await client.open_blob(REPO, DIGEST)


async def test_without_the_policy_redirects_are_followed_as_they_always_were():
    """The policy is an opt-in of the client: one built without it follows what main's client followed."""

    for location in ("http://storage.internal/blob", "http://169.254.169.254/blob", "http://127.1/blob"):
        handler, asked = redirecting(location)
        async with client_for(handler) as client:
            stream = await client.open_blob(REPO, DIGEST)
            assert await drain(stream, max_bytes=len(BODY), expected_size=len(BODY)) == (BODY, None)
        assert len(asked) == 2, location
    # An allowlist is itself the opt-in, for that source.
    handler, asked = redirecting("http://storage.internal/blob")
    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler), redirect_hosts=("x.example",)) as client:
        with pytest.raises(OCIProtocolError, match="refusing a redirect"):
            await client.open_blob(REPO, DIGEST)


# -- what the HTTP libraries log ---------------------------------------------------


@pytest.fixture
def redacted_logs():
    """The filters installed, as a Hub that indexes or serves has them."""

    oci.install_log_redaction()
    oci.install_log_redaction()  # idempotent
    names = (oci._REQUEST_LOGGER, *oci._TRANSPORT_LOGGERS)
    assert all(len(logging.getLogger(name).filters) == 1 for name in names)
    return names


def test_httpx_request_logs_lose_their_query_string_and_userinfo(caplog, redacted_logs):
    """A pre-signed storage URL's query is its credential; httpx would log it at INFO."""

    signed = httpx.URL("https://user:pw@storage.example/blob/sha256:abc?X-Amz-Signature=secret-signature&x=1")
    plain = httpx.URL("https://registry.example/v2/")
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET", signed, "HTTP/1.1 200 OK")
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET", plain, "HTTP/1.1 200 OK")
        logging.getLogger("httpx").info("no arguments at all")
        logging.getLogger("httpx").info("mapping %(a)s", {"a": 1})
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == 'HTTP Request: GET https://storage.example/blob/sha256:abc "HTTP/1.1 200 OK"'
    assert messages[1] == 'HTTP Request: GET https://registry.example/v2/ "HTTP/1.1 200 OK"'
    assert messages[2:] == ["no arguments at all", "mapping 1"]
    assert "secret-signature" not in caplog.text and "pw@" not in caplog.text


SENSITIVE_HEADERS = [
    (b"Location", b"https://user:pw@storage.internal/blob?X-Amz-Signature=SIGNED-SECRET"),
    (b"Set-Cookie", b"session=COOKIE-SECRET; HttpOnly"),
    (b"WWW-Authenticate", b'Bearer realm="https://auth.internal/token?hint=CHALLENGE-SECRET"'),
    (b"Content-Length", b"0"),
]
TRACE_SECRETS = ("SIGNED-SECRET", "COOKIE-SECRET", "CHALLENGE-SECRET", "pw@")


@pytest.mark.parametrize(
    ("logger_name", "return_value"),
    [
        ("httpcore.http11", (b"HTTP/1.1", 307, b"Temporary Redirect", SENSITIVE_HEADERS)),
        ("httpcore.http2", (307, SENSITIVE_HEADERS)),
    ],
)
async def test_the_transport_trace_never_logs_header_values(caplog, redacted_logs, logger_name, return_value):
    """httpcore's own trace path (``httpcore._trace.Trace``), as its HTTP/1.1 and HTTP/2 connections drive it."""

    import httpcore
    from httpcore._trace import Trace

    request = httpcore.Request("GET", "https://user:pw@registry.internal/v2/x?token=QUERY-SECRET")
    target = logging.getLogger(logger_name)
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        async with Trace("receive_response_headers", target, request, {"request": request}) as trace:
            trace.return_value = return_value
        async with Trace("send_request_headers", target, request, {"request": request, "stream_id": 1}):
            pass
        async with Trace("connect_tcp", target, request, {"host": "storage.internal", "port": 443}) as trace:
            trace.return_value = "https://user:pw@storage.internal/x?sig=QUERY-SECRET"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "receive_response_headers.complete status=307 [header values redacted]" in text
    assert "connect_tcp.started host='storage.internal' port=443" in text, "non-header lines keep their content"
    assert "https://storage.internal/x" in text
    for secret in (*TRACE_SECRETS, "QUERY-SECRET"):
        assert secret not in text, secret


async def test_a_real_redirect_over_a_real_socket_logs_no_signature(caplog, redacted_logs):
    """End to end through the installed transport: a local server, a signed ``Location``, DEBUG logging on."""

    import asyncio

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        while (await reader.readline()).strip():
            pass
        if b"/v2/" in request_line:
            port = writer.get_extra_info("sockname")[1]
            head = (
                "HTTP/1.1 307 Temporary Redirect\r\n"
                f"Location: http://localhost:{port}/store/blob?X-Amz-Signature=SIGNED-SECRET\r\n"
                "Set-Cookie: session=COOKIE-SECRET\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head)
        else:
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(BODY)}\r\nConnection: close\r\n\r\n".encode() + BODY
            )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with caplog.at_level(logging.DEBUG):
            # A name, not a loopback literal: the redirect goes to another port on the same host.
            async with OCIClient(f"http://localhost:{port + 0}") as client:
                stream = await client.open_blob(REPO, DIGEST)
                received, raised = await drain(stream, max_bytes=len(BODY), expected_size=len(BODY))
    finally:
        server.close()
        await server.wait_closed()
    assert raised is None and received == BODY
    transport_lines = [r.getMessage() for r in caplog.records if r.name.startswith("httpcore")]
    assert any("receive_response_headers.complete" in line for line in transport_lines), "the trace path ran"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SIGNED-SECRET" not in text and "COOKIE-SECRET" not in text
    assert "/store/blob" in text, "the path is still logged"
