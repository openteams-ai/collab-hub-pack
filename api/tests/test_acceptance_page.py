"""The browser invitation-acceptance page (issue #90).

What these prove, and what they deliberately do not.

**Proven here (the half the application owns, per the issue's ⚠️):** the
one-time secret never appears in a request line, a query string, a path, a
``Referer``, a rendered document, or any log record this application emits —
across success and every terminal state; the page is reachable without a
session while its redemption endpoint is not; and the
server half of the registration round trip works — an anonymous visit, a real
OIDC sign-in against the live stub IdP, and a redemption on the way back.

**Also proven, after gate 1:** a redemption never runs on a stale
verified-address assertion. A live, valid session whose ``email_verified`` was
recorded more than
:data:`~collab_hub_api.web.session.VERIFIED_CLAIM_MAX_AGE_SECONDS` ago is
refused by the *endpoint* — not merely warned about by the page — and the
re-authentication it sends the person through really does re-read the claims:
the IdP withdraws the verification mid-test and the renewed session carries
the withdrawal. The real-time bound is
:data:`~collab_hub_api.web.session.VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS`,
which is the nominal window plus the replica clock skew the codec allows, and
a test demonstrates the skewed case rather than asserting the arithmetic only.

**Also proven, after gate 2:** the request-size limit binds what is actually
read, not what the caller declares — a chunked body with no ``Content-Length``
and a ``Content-Length`` that understates the body are both refused — and no
unbounded read is delegated either, because a non-JSON content type is
refused before ``require_csrf`` could reach its form-parsing fallback.

**Not proven here, and not provable at this level:** that a gateway
configured to log request bodies does not capture the token. It would. That
is an internal issue, verified against the running
deployment.

**Not here: the page itself.** It is the registration app, a React bundle
built by ``admin-ui``, and how it reads the fragment, where it keeps the code,
what it posts and what it says in each state are executed by that project's
own suite (``admin-ui/registration/*.test.ts``). What this suite holds is the
contract between the two: every outcome word the endpoint can answer has a page
in the app's ``states.json``. Serving the bundle is ``test_registration_ui.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from registration_bundle import built_dist  # noqa: E402

# The live stub IdP, the https client, and the sign-in helpers are #88's, and
# reusing them is the point: this page has to work on the surface as built,
# not on a stand-in for it.
from test_web_surface import (  # noqa: E402
    SESSION_SECRET,
    _StubIdp,
    make_web_app,
    sign_in,
    web_client,
    web_values,
)

from collab_hub_api.config import WEB_SESSION_LIFETIME_CEILING_SECONDS, Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.invitation_email import build_setup_url
from collab_hub_api.frames.invitations import (
    AlreadyInOrganizationError,
    EmailNotVerifiedError,
    InvitationAcceptance,
    InvitationAlreadyUsedError,
    InvitationEmailMismatchError,
    InvitationError,
    InvitationExpiredError,
    InvitationNotFoundError,
    InvitationRevokedError,
    InvitationsUnavailableError,
    OrgNotFoundError,
)
from collab_hub_api.frames.org_source import (
    DEFAULT_ORG_ENV,
    DEFAULT_WORKSPACE_ENV,
    ORG_SOURCE_ENV,
)
from collab_hub_api.routers import invite as invite_router
from collab_hub_api.routers.invitations import redact_validation_details
from collab_hub_api.routers.invite import MAX_REDEEM_BODY_BYTES
from collab_hub_api.routers.web import make_router
from collab_hub_api.web.acceptance import (
    ACCEPT_PAGE_PATH,
    ACCEPT_REDEEM_PATH,
    OUTCOME_ACCEPTED,
    OUTCOME_ERROR,
    OUTCOME_NOT_FOUND,
    OUTCOME_REAUTHENTICATION_REQUIRED,
    OUTCOME_UNAVAILABLE,
)
from collab_hub_api.web.bundle import BuiltBundle
from collab_hub_api.web.pages import (
    CONTENT_SECURITY_POLICY,
    REGISTRATION_APP_HEADERS,
    headers_for_path,
)
from collab_hub_api.web.session import (
    CLOCK_SKEW_SECONDS,
    SESSION_COOKIE,
    SESSION_PURPOSE,
    VERIFIED_CLAIM_MAX_AGE_SECONDS,
    VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS,
    SessionCodec,
    WebSession,
    verified_claims_are_current,
)
from collab_hub_api.web.surface import ACCEPT_SESSION_PATH, PUBLIC_WEB_PATHS, build_web_surface

SENTINEL_TOKEN = "S3cr3tTokenValueThatMustNeverBePrinted"
"""A token that satisfies the accept model's alphabet and length.

Deliberately valid: a redaction that only held for values validation rejects
would prove nothing about the values that actually travel.
"""

INVITEE_EMAIL = "alice@example.com"


@pytest.fixture
def idp(monkeypatch):
    from collab_hub_api.frames import auth

    endpoint = _StubIdp()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


@pytest.fixture(autouse=True)
def membership_env(monkeypatch):
    """Membership mode, which is what makes acceptance mean anything.

    ``make_routers`` takes ``org_source_resolves_membership()`` at startup: on a
    claims-sourced deployment a redemption would write ``collab_org_members``
    rows nothing reads, so the endpoint refuses instead of reporting a
    success that granted nothing (#89's reasoning, applied to this surface).
    """

    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)


class FakeInvitationService:
    """Stands in for #89's lifecycle service, recording what it was asked.

    Injected on ``app.state`` because these tests do not run the lifespan;
    every terminal state is then reachable without a database, which is what
    makes the page's *presentation* of them cheap to cover exhaustively. The
    lifecycle semantics themselves are #89's and are tested there — the live
    end-to-end test at the bottom of this file is the one that proves the two
    halves meet.
    """

    available = True

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.settled: list[tuple] = []
        self.recorded: list[dict] = []
        self.raises = raises

    def accept(self, *, user_id, display, token_hash, claim_email, email_verified, service_groups=()):
        self.calls.append(
            {
                "user_id": user_id,
                "token_hash": token_hash,
                "claim_email": claim_email,
                "email_verified": email_verified,
                "display_email": display.email,
                "display_verified": display.email_verified,
                # #180: what the acceptance transaction is being asked to owe.
                # Recorded rather than ignored, so a page that stopped passing
                # it would fail a test instead of silently promising nothing.
                "service_groups": tuple(service_groups),
            }
        )
        if self.raises is not None:
            raise self.raises
        return InvitationAcceptance(invitation_id="inv-1", org_id="org-a", role="owner", org_created=True)

    def settle_service_access_grant(self, *, user_id, group_path, granted) -> None:
        self.settled.append((user_id, group_path, granted))

    def record_service_access_grant(self, user_id, display, **kwargs) -> None:
        self.recorded.append({"user_id": user_id, **kwargs})


PUBLIC_BASE_URL = "https://web.test"
"""Required on a membership-resolving deployment (#91): the operator page
renders a redemption link, and its origin must be configuration rather than a
forgeable request ``Host``. These tests run in membership mode, so startup
refuses without it."""


def build_app(
    tmp_path, idp, *, raises: Exception | None = None, web: dict | None = None
) -> tuple[object, FakeInvitationService]:
    app = make_web_app(
        tmp_path,
        idp,
        web={"public_base_url": PUBLIC_BASE_URL, "admin_ui_dist": str(built_dist(tmp_path)), **(web or {})},
    )
    service = FakeInvitationService(raises=raises)
    # The lifespan is what normally installs this; these tests drive the ASGI
    # app directly, so the state is installed here instead.
    app.state.invitation_service = service
    return app, service


PAGE_COPY = json.loads((Path(__file__).parents[1] / "admin-ui" / "registration" / "states.json").read_text())
"""The registration app's own table of states: the copy for each, and which
outcomes end the invitation. Read from the app's source, so the two halves are
checked against each other and not against a second list kept in step."""

PAGE_STATES = set(PAGE_COPY["pages"])
SETTLED_OUTCOMES = set(PAGE_COPY["settled"])


async def csrf_token(client: AsyncClient) -> str:
    """The CSRF token the app is handed for this browser's session."""

    token = (await client.get(ACCEPT_SESSION_PATH)).json()["csrf_token"]
    assert token, "the signed-in app must be handed a CSRF token"
    return token


async def signed_in(client: AsyncClient, idp: _StubIdp, *, verified: bool = True, email: str = INVITEE_EMAIL):
    idp.claims_override = {"email": email, "email_verified": verified}
    response = await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
    assert response.status_code == 303
    return response


async def redeem(client: AsyncClient, *, token: str = SENTINEL_TOKEN, csrf: str | None = None):
    headers = {"Content-Type": "application/json"}
    headers["X-CSRF-Token"] = csrf if csrf is not None else await csrf_token(client)
    return await client.post(ACCEPT_REDEEM_PATH, content=json.dumps({"token": token}), headers=headers)


# ===========================================================================
# The CSP relaxation: scoped to the app's own paths
# ===========================================================================


async def test_the_acceptance_page_is_the_only_path_that_may_run_script(tmp_path, idp):
    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(ACCEPT_PAGE_PATH)
        others = [
            await client.get("/web"),  # a redirect to sign-in
            await client.get("/web/signin"),
            await client.get("/web/signed-out"),
            await client.get("/web/app.css"),
            await client.get("/invite/something-else"),
            await client.get(ACCEPT_SESSION_PATH),
            await client.post(ACCEPT_REDEEM_PATH),
        ]
    assert "script-src" in page.headers["content-security-policy"]
    for response in others:
        assert "script" not in response.headers["content-security-policy"], response.url
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_the_acceptance_page_keeps_every_other_security_header(tmp_path, idp):
    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(ACCEPT_PAGE_PATH)
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp


async def test_a_failing_acceptance_page_answers_with_the_no_script_policy(tmp_path, idp, monkeypatch):
    """The worst-case response must not be the one that hands out a budget."""

    def explode(_bundle):
        raise RuntimeError("page render failed")

    monkeypatch.setattr(BuiltBundle, "document", explode)
    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get(ACCEPT_PAGE_PATH)
    assert response.status_code == 500
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "page render failed" not in response.text


def test_the_policy_exception_is_keyed_on_the_apps_own_paths_and_no_other():
    assert headers_for_path(ACCEPT_PAGE_PATH) == REGISTRATION_APP_HEADERS
    assert headers_for_path("/invite/assets/index-abc123.js") == REGISTRATION_APP_HEADERS
    for path in (
        "/web",
        "/web/signin",
        ACCEPT_REDEEM_PATH,
        ACCEPT_SESSION_PATH,
        "/invite/accept/",
        "/admin/invitations",
    ):
        assert headers_for_path(path)["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


# ===========================================================================
# Anonymous by design — and only the page
# ===========================================================================


def test_the_page_is_public_and_its_redemption_endpoint_is_not():
    assert ACCEPT_PAGE_PATH in PUBLIC_WEB_PATHS
    assert ACCEPT_REDEEM_PATH not in PUBLIC_WEB_PATHS


async def test_an_invitee_with_no_account_sees_the_page(tmp_path, idp):
    """The document is a build output: the same bytes whoever asks.

    Nothing about the person is in it, which is half of what makes it safe to
    serve anonymously. What differs per browser is the session answer.
    """

    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        anonymous = await client.get(ACCEPT_PAGE_PATH)
        answer = (await client.get(ACCEPT_SESSION_PATH)).json()
        await signed_in(client, idp)
        returning = await client.get(ACCEPT_PAGE_PATH)
    assert anonymous.status_code == 200
    assert anonymous.text == returning.text
    # No CSRF token for a browser with no session — there is nothing to bind
    # it to, and the app will send them to sign in rather than POST.
    assert answer["signed_in"] is False
    assert answer["csrf_token"] is None


ACCOUNT_URL = "https://billing.example.com/account"


async def test_the_signed_in_app_is_handed_the_account_link_when_set(tmp_path, idp):
    # The app draws the link beside Sign out; this route is what hands it the
    # value (#275): a dropped `account_url` here fails here.
    app, _ = build_app(tmp_path, idp, web={"account_page_url": ACCOUNT_URL})
    async with web_client(app) as client:
        await signed_in(client, idp)
        answer = (await client.get(ACCEPT_SESSION_PATH)).json()
    assert answer["account_url"] == ACCOUNT_URL


async def test_the_anonymous_app_is_never_handed_the_account_link(tmp_path, idp):
    # This is the one public page that shows the signed-in footer, so it is
    # the page where "set, but signed out" can actually go wrong.
    app, _ = build_app(tmp_path, idp, web={"account_page_url": ACCOUNT_URL})
    async with web_client(app) as client:
        answer = (await client.get(ACCEPT_SESSION_PATH)).json()
    assert answer["account_url"] is None
    assert ACCOUNT_URL not in json.dumps(answer)


async def test_an_anonymous_redemption_is_refused_by_the_guard(tmp_path, idp):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=json.dumps({"token": SENTINEL_TOKEN}),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/signin?")
    assert service.calls == []


async def test_the_rest_of_the_invite_prefix_stays_guarded(tmp_path, idp):
    # Listing /invite as a surface prefix is what puts the acceptance page
    # under the security headers; it must not make the prefix anonymous.
    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        for path in ("/invite", "/invite/accept/redeem", "/invite/anything"):
            response = await client.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"].startswith("/web/signin?"), path


async def test_redemption_without_a_csrf_token_is_refused(tmp_path, idp):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await redeem(client, csrf="")
        wrong = await redeem(client, csrf="not-the-token")
    assert response.status_code == 403
    assert wrong.status_code == 403
    assert service.calls == []


def test_a_public_page_router_not_on_the_allowlist_is_refused(tmp_path, idp):
    """The seam cannot be used to obtain anonymity without the reviewed line."""

    from fastapi import APIRouter

    rogue = APIRouter()

    @rogue.get("/invite/backdoor")
    async def backdoor():
        return {"secret": "invitee data"}

    surface = build_web_surface(Config.parse(web_values(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})))
    with pytest.raises(RuntimeError, match="PUBLIC_WEB_PATHS"):
        make_router(surface, public_page_routers=[rogue])


# ===========================================================================
# The token: never in a URL, a document, or a log
# ===========================================================================


async def test_the_secret_never_appears_in_a_request_line_or_a_referer(tmp_path, idp):
    """The fragment-to-body property, asserted on what the server saw.

    ``httpx`` sends everything before the ``#`` and nothing after it, exactly
    as a browser does, so requesting the page with the real minted link and
    then reading the server's own view of the request is a faithful check
    that the fragment does not arrive.
    """

    seen: list[str] = []
    app, service = build_app(tmp_path, idp)

    @app.middleware("http")
    async def record(request, call_next):
        seen.append(str(request.url))
        seen.append(request.headers.get("referer", ""))
        return await call_next(request)

    link = build_setup_url("https://web.test/invite/accept", SENTINEL_TOKEN)
    assert link.endswith(f"#token={SENTINEL_TOKEN}")
    async with web_client(app) as client:
        await client.get(link)
        await signed_in(client, idp)
        response = await redeem(client)

    assert response.status_code == 200
    assert SENTINEL_TOKEN not in "".join(seen)
    # It did reach the service, in the body — that is the design, not a leak.
    assert service.calls[0]["token_hash"] == sha256(SENTINEL_TOKEN.encode()).hexdigest()


@pytest.mark.parametrize(
    "raises",
    [
        None,
        InvitationNotFoundError("no"),
        InvitationExpiredError("no"),
        InvitationRevokedError("no"),
        InvitationAlreadyUsedError("no"),
        EmailNotVerifiedError("no"),
        InvitationEmailMismatchError("no"),
        AlreadyInOrganizationError("no"),
        OrgNotFoundError("no"),
        InvitationsUnavailableError("no"),
    ],
    ids=lambda value: "success" if value is None else type(value).__name__,
)
async def test_no_outcome_puts_the_secret_in_a_log_or_a_response(tmp_path, idp, caplog, raises):
    app, _ = build_app(tmp_path, idp, raises=raises)
    with caplog.at_level(logging.DEBUG):
        async with web_client(app) as client:
            await signed_in(client, idp)
            response = await redeem(client)

    assert SENTINEL_TOKEN not in response.text
    for record in caplog.records:
        assert SENTINEL_TOKEN not in record.getMessage()
        assert SENTINEL_TOKEN not in repr(record.args)
        for value in record.__dict__.values():
            assert SENTINEL_TOKEN not in repr(value), record.name


async def test_a_near_miss_token_is_not_echoed_back(tmp_path, idp, caplog):
    """Validation must not become the oracle the API route closed.

    The body is parsed by hand precisely so pydantic's 422 machinery — which
    reflects the rejected input — never sees the value. This asserts the
    outcome of that decision rather than the decision itself.
    """

    near_miss = SENTINEL_TOKEN + "!"  # outside the token alphabet
    app, service = build_app(tmp_path, idp)
    with caplog.at_level(logging.DEBUG):
        async with web_client(app) as client:
            await signed_in(client, idp)
            response = await redeem(client, token=near_miss)

    assert response.status_code == 404
    assert response.json() == {"outcome": OUTCOME_NOT_FOUND}
    assert SENTINEL_TOKEN not in response.text
    assert service.calls == []
    for record in caplog.records:
        assert SENTINEL_TOKEN not in repr(record.__dict__)


def test_the_redeem_path_is_registered_for_validation_redaction():
    # Belt to the hand-parsing braces: if a future author declares a body
    # model on this route, the 422 that results still drops its details.
    assert redact_validation_details(ACCEPT_REDEEM_PATH)
    assert redact_validation_details("/nexus" + ACCEPT_REDEEM_PATH)
    assert redact_validation_details("/v1/invitations/accept")
    assert not redact_validation_details("/v1/frames")


@pytest.mark.parametrize(
    "body",
    ['{"token": 5}', '{"token": null}', "[]", '"a string"', "not json at all", "", '{"nope": "x"}'],
)
async def test_an_unusable_body_answers_one_uniform_outcome(tmp_path, idp, body):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=body,
            headers={"Content-Type": "application/json", "X-CSRF-Token": await csrf_token(client)},
        )
    assert response.status_code == 404
    assert response.json() == {"outcome": OUTCOME_NOT_FOUND}
    assert service.calls == []


async def test_an_oversized_body_is_refused_before_it_is_parsed(tmp_path, idp):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=json.dumps({"token": "a" * 40_000}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": await csrf_token(client)},
        )
    assert response.status_code == 413
    assert response.json() == {"outcome": OUTCOME_ERROR}
    assert service.calls == []


async def test_a_chunked_body_cannot_slip_past_the_limit(tmp_path, idp):
    """The gate-2 major, as a regression.

    A chunked request carries no ``Content-Length``, so a limit that consults
    only that header is not a limit against exactly the caller it needs to
    bound — and the read that followed had no cap of its own. This sends far
    more than the cap with no declared length at all.
    """

    sent = 0

    async def stream():
        nonlocal sent
        for _ in range(64):
            chunk = b"a" * 4096
            sent += len(chunk)
            yield chunk

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=stream(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": await csrf_token(client)},
        )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.request.headers.get("transfer-encoding") == "chunked"
    assert response.status_code == 413
    assert response.json() == {"outcome": OUTCOME_ERROR}
    assert service.calls == []
    # And it stopped reading rather than draining the whole thing.
    assert sent <= MAX_REDEEM_BODY_BYTES + 8192, f"read {sent} bytes past a {MAX_REDEEM_BODY_BYTES} cap"


async def test_a_content_length_that_understates_the_body_does_not_help(tmp_path, idp):
    """The header is a fast path, never the gate — so lying in it gains nothing."""

    app, service = build_app(tmp_path, idp)
    oversized = json.dumps({"token": "a" * 40_000}).encode()
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=oversized,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": await csrf_token(client),
                # Understated by three orders of magnitude.
                "Content-Length": "12",
            },
        )
    assert response.status_code == 413
    assert response.json() == {"outcome": OUTCOME_ERROR}
    assert service.calls == []


async def test_a_body_at_the_cap_still_works(tmp_path, idp):
    """The bound must not have moved: a legitimate body is well under it."""

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        body = json.dumps({"token": SENTINEL_TOKEN, "padding": "p" * 1000}).encode()
        assert len(body) < MAX_REDEEM_BODY_BYTES
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=body,
            headers={"Content-Type": "application/json", "X-CSRF-Token": await csrf_token(client)},
        )
    assert response.status_code == 200
    assert len(service.calls) == 1


@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain", ""],
)
async def test_only_a_json_body_is_accepted_at_all(tmp_path, idp, content_type):
    """The gate that keeps require_csrf's form fallback out of reach.

    That dependency parses a **form** when no ``X-CSRF-Token`` header is
    present, and ``request.form()`` buffers a urlencoded body with no cap of
    its own — an unbounded read this endpoint would have been delegating.
    Refusing a non-JSON content type before the CSRF check runs makes that
    branch unreachable here.
    """

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        headers = {"X-CSRF-Token": await csrf_token(client)}
        if content_type:
            headers["Content-Type"] = content_type
        response = await client.post(ACCEPT_REDEEM_PATH, content=b"token=x", headers=headers)
    assert response.status_code == 415
    assert response.json() == {"outcome": OUTCOME_ERROR}
    assert service.calls == []


async def test_a_form_encoded_body_is_refused_before_the_csrf_check_reads_it(tmp_path, idp):
    """Ordering, asserted by outcome rather than by reading the source.

    The form carries a **valid** CSRF token. If the CSRF dependency ran first
    it would parse this body to find it — which is the read being prevented.
    A 415 rather than any CSRF-shaped answer is what says the gate went first.
    """

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            data={"csrf_token": await csrf_token(client), "token": SENTINEL_TOKEN},
        )
    assert response.status_code == 415
    assert response.json() == {"outcome": OUTCOME_ERROR}
    assert service.calls == []


async def test_every_refusal_issued_before_the_body_is_read_closes_the_connection(tmp_path, idp):
    """The invariant, checked on each path rather than on the one that failed.

    Answering an HTTP/1.1 request whose body has not been read to
    end-of-message leaves the server unable to start the next cycle on that
    connection — it buffers what is still arriving and then stalls. Every
    refusal below is issued before the body was consumed, so every one of them
    has to close, not only the oversize path that made it obvious.
    """

    async def refusals(client):
        csrf = await csrf_token(client)
        json_headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        body = json.dumps({"token": SENTINEL_TOKEN})
        return {
            "unsupported media type": await client.post(
                ACCEPT_REDEEM_PATH, content=b"token=x", headers={"X-CSRF-Token": csrf}
            ),
            "bad csrf": await client.post(
                ACCEPT_REDEEM_PATH,
                content=body,
                headers={"Content-Type": "application/json", "X-CSRF-Token": "wrong"},
            ),
            "declared oversize": await client.post(
                ACCEPT_REDEEM_PATH,
                content=json.dumps({"token": "a" * 40_000}),
                headers=json_headers,
            ),
        }

    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        answers = await refusals(client)
        # ...and the two that refuse before any of the above even runs.
        _age_session(client, VERIFIED_CLAIM_MAX_AGE_SECONDS + 60)
        answers["stale claims"] = await client.post(
            ACCEPT_REDEEM_PATH,
            content=json.dumps({"token": SENTINEL_TOKEN}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": "unused"},
        )

    for label, response in answers.items():
        assert response.headers.get("connection") == "close", f"{label} left the connection open"
        assert response.json()["outcome"]


async def test_a_completed_redemption_leaves_the_connection_usable(tmp_path, idp):
    # The other half: a response issued *after* the body was read must not
    # close, or every acceptance would cost a fresh connection.
    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        accepted = await redeem(client)
        # `!` is outside the token alphabet, so this is refused by parsing —
        # after the body was read, which is the case being distinguished.
        unparseable = await redeem(client, token="not-a-token!")
    assert accepted.status_code == 200
    assert "connection" not in {name.lower() for name in accepted.headers}
    assert unparseable.status_code == 404
    assert "connection" not in {name.lower() for name in unparseable.headers}


async def test_a_json_body_with_a_bad_csrf_token_answers_json(tmp_path, idp):
    # The endpoint's contract is total JSON: the page's script must never have
    # to parse an HTML 403 page to learn what happened.
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=json.dumps({"token": SENTINEL_TOKEN}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": "wrong"},
        )
    assert response.status_code == 403
    assert response.json() == {"outcome": OUTCOME_ERROR}
    assert service.calls == []


# ===========================================================================
# Terminal states
# ===========================================================================


def test_every_terminal_state_the_service_can_raise_has_a_page_outcome():
    """Enumerated from the class hierarchy, not from a list kept in step.

    A new terminal state added to #89 fails here rather than rendering the
    generic error page to whoever hits it.
    """

    states = set(InvitationError.__subclasses__()) | {InvitationsUnavailableError}
    for state in states:
        resolved = invite_router.outcome_for(state("message"))
        assert resolved is not None, f"{state.__name__} has no acceptance-page outcome"
        outcome, status_code = resolved
        assert outcome in PAGE_STATES, f"{state.__name__} maps to copy that does not exist"
        assert 400 <= status_code < 600


def test_every_outcome_word_has_copy_on_the_page():
    """Every word the endpoint can answer is a state the app has copy for."""

    for _exception, outcome, _status in invite_router.TERMINAL_OUTCOMES:
        assert outcome in PAGE_STATES
    for outcome in (
        OUTCOME_ACCEPTED,
        OUTCOME_ERROR,
        OUTCOME_UNAVAILABLE,
        OUTCOME_REAUTHENTICATION_REQUIRED,
    ):
        assert outcome in PAGE_STATES
    for state, copy in PAGE_COPY["pages"].items():
        assert copy["heading"], state
        assert copy["paragraphs"], state


@pytest.mark.parametrize(
    ("raises", "outcome", "status_code"),
    [(exc("m"), outcome, code) for exc, outcome, code in invite_router.TERMINAL_OUTCOMES],
    ids=[exc.__name__ for exc, _, _ in invite_router.TERMINAL_OUTCOMES],
)
async def test_each_terminal_state_answers_its_own_word(tmp_path, idp, raises, outcome, status_code):
    app, _ = build_app(tmp_path, idp, raises=raises)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await redeem(client)
    assert response.status_code == status_code
    assert response.json() == {"outcome": outcome}
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_a_successful_redemption_answers_accepted_and_nothing_else(tmp_path, idp):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await redeem(client)
    assert response.status_code == 200
    # No org id, no role: the page renders fixed copy, so membership facts in
    # this body would have no reader and every reason to end up in a log.
    assert response.json() == {"outcome": OUTCOME_ACCEPTED}
    assert len(service.calls) == 1


async def test_an_unexpected_failure_is_not_dressed_up_as_an_outcome(tmp_path, idp):
    """A bug must look like a bug, not like a terminal state of the flow."""

    app, _ = build_app(tmp_path, idp, raises=ValueError("something else entirely"))
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await redeem(client)
    assert response.status_code == 500
    assert "something else entirely" not in response.text
    assert "outcome" not in response.text


async def test_claims_mode_refuses_rather_than_granting_nothing(tmp_path, idp, monkeypatch):
    """#89's reasoning, applied here: absent is a truer answer than broken.

    A redemption on a claims-sourced deployment would write membership rows
    the authentication choke point never reads — success on the page, nothing
    granted in practice.
    """

    monkeypatch.setenv(ORG_SOURCE_ENV, "claims")
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await redeem(client)
    assert response.status_code == 503
    assert response.json() == {"outcome": OUTCOME_UNAVAILABLE}
    assert service.calls == []


# ===========================================================================
# The registration / sign-in round trip
# ===========================================================================


async def test_the_flow_survives_the_sign_in_round_trip(tmp_path, idp):
    """Anonymous page → real OIDC sign-in → back to the page → redemption.

    The browser's half (holding the token across that navigation in
    ``sessionStorage``) is not executable here; what this proves is that the
    server returns the invitee to the acceptance page rather than somewhere
    else, and that the page they land on is the one that can finish the job.
    """

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        anonymous = await client.get(ACCEPT_PAGE_PATH)
        assert anonymous.status_code == 200
        assert (await client.get(ACCEPT_SESSION_PATH)).json()["signed_in"] is False

        # The app's own create-account link (`CREATE_ACCOUNT_URL` in its
        # `links.ts`), as a browser resolves it from the page's address.
        href = "/web/signin?next=%2Finvite%2Faccept&register=1"
        start = await client.get(href)
        assert start.status_code == 303
        params = {key: value[0] for key, value in parse_qs(urlsplit(start.headers["location"]).query).items()}
        idp.nonce = params["nonce"]
        idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": True}
        callback = await client.get("/web/oidc/callback", params={"code": "stub-code", "state": params["state"]})
        assert callback.status_code == 303
        # Back where they started, not at the operator overview.
        assert callback.headers["location"] == ACCEPT_PAGE_PATH

        landed = await client.get(ACCEPT_PAGE_PATH)
        assert landed.status_code == 200
        assert (await client.get(ACCEPT_SESSION_PATH)).json()["signed_in"] is True
        response = await redeem(client)

    assert response.status_code == 200
    assert service.calls[0]["claim_email"] == INVITEE_EMAIL
    assert service.calls[0]["email_verified"] is True


@pytest.mark.parametrize("verified", [True, False])
async def test_the_session_carries_the_verified_email_claim_through(tmp_path, idp, verified):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp, verified=verified)
        await redeem(client)
    assert service.calls[0]["email_verified"] is verified
    assert service.calls[0]["display_verified"] is verified


@pytest.mark.parametrize("claim", ["true", 1, None, "yes"])
async def test_only_a_boolean_true_counts_as_verified(tmp_path, idp, claim):
    """A string ``"false"`` is truthy, and a string ``"true"`` is not a claim
    anyone verified. Both must come out unverified."""

    app, service = build_app(tmp_path, idp)
    idp.claims_override = {"email": INVITEE_EMAIL}
    if claim is not None:
        idp.claims_override["email_verified"] = claim
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
        await redeem(client)
    assert service.calls[0]["email_verified"] is False


async def test_a_session_minted_before_the_field_existed_is_unverified(tmp_path, idp):
    """Fail-closed on an old cookie rather than letting it through."""

    app, service = build_app(tmp_path, idp)
    codec = SessionCodec(SESSION_SECRET)
    now = int(datetime.now(tz=timezone.utc).timestamp())
    legacy = codec.encode(
        SESSION_PURPOSE,
        {
            "sub": "subject-alice",
            "name": "Alice",
            "email": INVITEE_EMAIL,
            "csrf": "legacy-csrf",
            "iat": now,
            "exp": now + 600,
        },
    )
    async with web_client(app) as client:
        client.cookies.set(SESSION_COOKIE, legacy, domain="web.test", path="/")
        response = await redeem(client, csrf="legacy-csrf")
    assert response.status_code == 200
    assert service.calls[0]["email_verified"] is False
    assert codec.decode_session(legacy).email_verified is False


def test_the_session_round_trips_the_flag_and_refuses_a_string():
    codec = SessionCodec(SESSION_SECRET)
    now = int(datetime.now(tz=timezone.utc).timestamp())
    session = WebSession(
        user="u",
        name=None,
        email=INVITEE_EMAIL,
        csrf="c",
        issued_at=now,
        expires_at=now + 60,
        email_verified=True,
    )
    assert codec.decode_session(codec.encode_session(session)) == session
    stringly = codec.encode(
        SESSION_PURPOSE,
        {"sub": "u", "csrf": "c", "email_verified": "true", "iat": now, "exp": now + 60},
    )
    assert codec.decode_session(stringly).email_verified is False


# ===========================================================================
# The verified address must be current, not merely once-asserted
# ===========================================================================


def _age_session(client: AsyncClient, seconds: int) -> None:
    """Re-sign the browser's session cookie with an older ``iat``/``exp``.

    Re-signed rather than time-travelled so the cookie is genuinely valid —
    the codec verifies it, the guard accepts it, and the only thing that has
    changed is how long ago its claims were minted. That is exactly the state
    the fix exists for: a live session whose verified-address assertion is
    stale.
    """

    codec = SessionCodec(SESSION_SECRET)
    session = codec.decode_session(client.cookies[SESSION_COOKIE])
    assert session is not None
    now = int(datetime.now(tz=timezone.utc).timestamp())
    aged = codec.encode(
        SESSION_PURPOSE,
        {
            "sub": session.user,
            "name": session.name,
            "email": session.email,
            "email_verified": session.email_verified,
            "csrf": session.csrf,
            "iat": now - seconds,
            "exp": now + 3600,
        },
    )
    client.cookies.set(SESSION_COOKIE, aged, domain="web.test", path="/")


async def test_a_stale_verified_address_cannot_redeem(tmp_path, idp):
    """The gate-1 major, as a regression.

    The session still says ``email_verified: true`` — it was true when it was
    minted. That is precisely the assertion that must not be acted on hours
    later: the IdP can withdraw a verification, and what this would bind is
    permanent.
    """

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        _age_session(client, VERIFIED_CLAIM_MAX_AGE_SECONDS + 60)
        answer = (await client.get(ACCEPT_SESSION_PATH)).json()
        response = await redeem(client)

    assert response.status_code == 401
    assert response.json() == {"outcome": OUTCOME_REAUTHENTICATION_REQUIRED}
    assert service.calls == [], "no redemption may run on a stale assertion"
    # And the app was told so before the click, to save a refusal.
    assert answer["signed_in"] is True
    assert answer["claims_current"] is False


async def test_the_endpoint_refuses_even_when_the_page_said_otherwise(tmp_path, idp):
    """The render-time flag is a hint; the endpoint is the control.

    The window can lapse between the page load and the click, so a page that
    rendered ``ready`` must not be able to authorize a redemption on its own.
    """

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        fresh = (await client.get(ACCEPT_SESSION_PATH)).json()
        assert fresh["claims_current"] is True
        csrf = fresh["csrf_token"]
        # Time passes between reading the page and clicking Accept.
        _age_session(client, VERIFIED_CLAIM_MAX_AGE_SECONDS + 1)
        response = await client.post(
            ACCEPT_REDEEM_PATH,
            content=json.dumps({"token": SENTINEL_TOKEN}),
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
        )
    assert response.status_code == 401
    assert response.json() == {"outcome": OUTCOME_REAUTHENTICATION_REQUIRED}
    assert service.calls == []


async def test_a_fresh_session_redeems_and_the_window_is_the_only_difference(tmp_path, idp):
    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        # Old, but inside the window: the same request must succeed, so the
        # refusal above is the freshness rule and not something else.
        _age_session(client, VERIFIED_CLAIM_MAX_AGE_SECONDS - 30)
        response = await redeem(client)
    assert response.status_code == 200
    assert len(service.calls) == 1


async def test_re_authentication_re_reads_the_claims_from_the_idp(tmp_path, idp):
    """The whole point of the bounce: current claims, not cached ones.

    The IdP withdraws the verification while the browser holds a session that
    still asserts it. Renewing mints a new ID token from the IdP's current
    view, and the redemption that follows is refused on the *live* fact.
    """

    app, service = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp, verified=True)
        _age_session(client, VERIFIED_CLAIM_MAX_AGE_SECONDS + 60)
        # The app's own renew link (`RENEW_SIGN_IN_URL` in its `links.ts`), as
        # a browser resolves it from the page's address.
        href = "/web/signin?next=%2Finvite%2Faccept&renew=1"

        idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": False}
        start = await client.get(href)
        assert start.status_code == 303, "renew must run the flow, not short-circuit"
        params = {key: value[0] for key, value in parse_qs(urlsplit(start.headers["location"]).query).items()}
        idp.nonce = params["nonce"]
        callback = await client.get("/web/oidc/callback", params={"code": "stub-code", "state": params["state"]})
        assert callback.status_code == 303
        assert callback.headers["location"] == ACCEPT_PAGE_PATH

        renewed = (await client.get(ACCEPT_SESSION_PATH)).json()
        assert renewed["claims_current"] is True
        response = await redeem(client)

    # Redemption now runs — on the withdrawn verification, which the service
    # refuses. The stale session would have handed it `True`.
    assert response.status_code == 200
    assert service.calls[0]["email_verified"] is False


async def test_signin_without_renew_still_short_circuits_an_existing_session(tmp_path, idp):
    # The renew flag must be the deliberate act; the ordinary sign-in route
    # keeps the behaviour #88 shipped.
    app, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        plain = await client.get("/web/signin", params={"next": ACCEPT_PAGE_PATH})
        renewed = await client.get("/web/signin", params={"next": ACCEPT_PAGE_PATH, "renew": "1"})
    assert plain.status_code == 303
    assert plain.headers["location"] == ACCEPT_PAGE_PATH
    assert renewed.status_code == 303
    assert renewed.headers["location"].startswith(idp.issuer)


def test_the_freshness_window_is_far_below_the_session_lifetime():
    # The bound is what the claim rests on. If someone raises it to the
    # session lifetime, the fix is gone and this says so.
    assert VERIFIED_CLAIM_MAX_AGE_SECONDS <= 900
    assert VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS * 8 < WEB_SESSION_LIFETIME_CEILING_SECONDS


def test_the_documented_worst_case_accounts_for_replica_skew():
    """The gate-2 minor: the guarantee is six minutes, not five.

    The codec accepts an ``iat`` up to ``CLOCK_SKEW_SECONDS`` in the future,
    so a session minted by a replica at the skew limit is that much older in
    real time than this server computes. Derived rather than written down, so
    raising the skew allowance moves the documented number with it.
    """

    assert VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS == VERIFIED_CLAIM_MAX_AGE_SECONDS + CLOCK_SKEW_SECONDS

    # Demonstrated, not just asserted arithmetically: a session a replica at
    # the skew limit minted is still accepted when its real age is the worst
    # case, and refused past it.
    now = int(datetime.now(tz=timezone.utc).timestamp())
    minted_at = now - VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS
    skewed = WebSession(
        user="u",
        name=None,
        email=INVITEE_EMAIL,
        csrf="c",
        issued_at=minted_at + CLOCK_SKEW_SECONDS,  # the replica's clock ran fast
        expires_at=now + 3600,
        email_verified=True,
    )
    assert verified_claims_are_current(skewed, now=now)
    assert not verified_claims_are_current(skewed, now=now + 1)


def test_a_future_dated_session_is_not_fresh_forever():
    # A replica an instant ahead must not produce a negative age that reads as
    # eternally current.
    now = int(datetime.now(tz=timezone.utc).timestamp())
    session = WebSession(
        user="u",
        name=None,
        email=INVITEE_EMAIL,
        csrf="c",
        issued_at=now + 10_000,
        expires_at=now + 20_000,
        email_verified=True,
    )
    assert verified_claims_are_current(session, now=now)
    assert not verified_claims_are_current(session, now=now + 20_000)


def test_reauthentication_keeps_the_token_and_has_copy():
    assert OUTCOME_REAUTHENTICATION_REQUIRED not in SETTLED_OUTCOMES
    assert OUTCOME_REAUTHENTICATION_REQUIRED in PAGE_STATES


# ===========================================================================
# The public-page seam
# ===========================================================================


def test_a_state_changing_handler_cannot_ride_a_public_path(tmp_path, idp):
    """Gate 1's minor, as a regression — codex's exact construction.

    ``PUBLIC_WEB_PATHS`` is a set of *paths*, because the guard runs before
    routing and has no route to ask about methods. So a path in it is
    anonymous for every method, and a POST registered on one would be too.
    """

    from fastapi import APIRouter

    rogue = APIRouter()

    @rogue.post(ACCEPT_PAGE_PATH)
    async def anonymous_post():
        return {"granted": True}

    surface = build_web_surface(Config.parse(web_values(tmp_path, idp)))
    with pytest.raises(RuntimeError, match="POST"):
        make_router(surface, public_page_routers=[rogue])


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_no_unsafe_method_may_be_public(tmp_path, idp, method):
    from fastapi import APIRouter

    rogue = APIRouter()
    getattr(rogue, method)(ACCEPT_PAGE_PATH)(lambda: {"granted": True})

    surface = build_web_surface(Config.parse(web_values(tmp_path, idp)))
    with pytest.raises(RuntimeError, match=method.upper()):
        make_router(surface, public_page_routers=[rogue])


def test_a_non_apiroute_cannot_be_public(tmp_path, idp):
    # An isinstance, not a duck-typed `.path`: a fabricated object satisfied
    # the structural version of this check in #88's review.
    from fastapi import APIRouter
    from starlette.routing import Mount, WebSocketRoute

    surface = build_web_surface(Config.parse(web_values(tmp_path, idp)))
    for route in (
        Mount(ACCEPT_PAGE_PATH, app=lambda *_: None),
        WebSocketRoute(ACCEPT_PAGE_PATH, endpoint=lambda *_: None),
    ):
        rogue = APIRouter()
        rogue.routes.append(route)
        with pytest.raises(RuntimeError, match="public page route"):
            make_router(surface, public_page_routers=[rogue])


def test_the_acceptance_page_itself_passes_the_seam(tmp_path, idp):
    # The check has to admit the one router that legitimately uses it, or it
    # is only proving that nothing can be public.
    surface = build_web_surface(Config.parse(web_values(tmp_path, idp)))
    public, gated = invite_router.make_routers(
        memberships_enabled=True,
        require_verified_email=True,
        bundle=BuiltBundle.at(built_dist(tmp_path) / "registration"),
    )
    make_router(surface, page_routers=[gated], public_page_routers=[public])
    # The page, its session answer, and its bundle files: every one a GET.
    assert sorted(route.path for route in public.routes) == [
        "/invite/accept",
        "/invite/accept/session",
        "/invite/assets/{filename}",
    ]
    assert all(route.methods == {"GET"} for route in public.routes)


def test_the_settled_list_is_exactly_the_states_that_kill_the_invitation():
    # After these the browser drops the token and remembers the result, so a
    # reload cannot re-POST it. The rest are recoverable by the person and
    # deliberately keep the token.
    assert OUTCOME_ACCEPTED in SETTLED_OUTCOMES
    for recoverable in (
        "invitation_email_mismatch",
        "email_not_verified",
        "already_in_organization",
        "organization_creation_refused",
        "invitations_unavailable",
        OUTCOME_REAUTHENTICATION_REQUIRED,
        OUTCOME_ERROR,
    ):
        assert recoverable not in SETTLED_OUTCOMES
    assert SETTLED_OUTCOMES <= PAGE_STATES


# ===========================================================================
# Against a real HTTP server: the oversize request must not hold a connection
# ===========================================================================


@contextlib.contextmanager
def running_server(app):
    """The app under a real uvicorn, on a loopback port.

    ASGI-transport tests cannot see this class of bug at all: httpx hands the
    app a complete request and there is no socket, no keep-alive, and no
    flow control. The failure being guarded against lives entirely in those.
    """

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():  # pragma: no cover
            raise AssertionError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class _LoopbackBrowser:
    """A client that carries this surface's ``Secure`` cookies over loopback http.

    Cookies are tracked by hand rather than through ``httpx``'s jar, for a
    reason worth writing down so nobody replaces this with the obvious
    two-liner:

    * the surface's cookies are ``Secure``, and ``http.cookiejar`` correctly
      refuses to return a Secure cookie over plain ``http`` — which is why
      every other test in this file uses an https base URL;
    * widening the jar's policy (``secure_protocols=("http", "https")``) looks
      like the fix and silently is not — httpx 0.28 does not consult the jar's
      policy when it builds the outgoing ``Cookie`` header, so the cookie is
      stored, the policy reads as widened, and nothing is ever sent. That cost
      a debugging round; it is recorded here rather than rediscovered.

    Serving real TLS would be the other answer, and it would add a
    certificate to the suite for no property these two tests are about. What
    they need is a real socket with real keep-alive, which this has.

    Browsers treat ``localhost`` as a secure context and send these cookies,
    so this models the deployed behaviour rather than working around it.
    """

    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=15.0, follow_redirects=False)
        self._cookies: dict[str, str] = {}

    def __enter__(self) -> "_LoopbackBrowser":
        return self

    def __exit__(self, *exc_info) -> None:
        self._client.close()

    def _absorb(self, response: httpx.Response) -> None:
        for name, value in response.headers.multi_items():
            if name.lower() != "set-cookie":
                continue
            key, _, raw = value.split(";", 1)[0].partition("=")
            if raw.strip(' "'):
                self._cookies[key.strip()] = raw
            else:
                # A deletion (``Max-Age=0`` with an empty value), which the
                # sign-in flow uses to retire the transient cookie.
                self._cookies.pop(key.strip(), None)

    def cookie_header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self._cookies.items())

    def request(self, method: str, path: str, *, headers=None, **kwargs) -> httpx.Response:
        merged = dict(headers or {})
        if self._cookies:
            merged["Cookie"] = self.cookie_header()
        response = self._client.request(method, path, headers=merged, **kwargs)
        self._absorb(response)
        return response

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)


def _post_body_over_socket(
    base_url: str, *, headers: dict[str, str], path: str = ACCEPT_REDEEM_PATH, chunks: int = 16
) -> tuple[float, bytes, bool]:
    """Send a chunked POST on a raw socket; report ``(elapsed, bytes, closed)``.

    A raw socket rather than ``httpx``, because the property under test is
    *"the server let this connection go"* and no client library reports that.
    End-of-file on a socket the client never closed is the direct observation.

    ``chunks`` × 4 KiB is deliberately sized: 64 KiB is comfortably past the
    2 KiB cap and small enough to fit in socket buffers, so the send cannot
    block and confuse the measurement.
    """

    parts = urlsplit(base_url)
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {parts.hostname}:{parts.port}",
        "Transfer-Encoding: chunked",
        *(f"{name}: {value}" for name, value in headers.items()),
    ]
    head = ("\r\n".join(lines) + "\r\n\r\n").encode()

    received = b""
    closed = False
    with socket.create_connection((parts.hostname, parts.port), timeout=25) as sock:
        started = time.monotonic()
        sock.sendall(head)
        with contextlib.suppress(OSError):
            for _ in range(chunks):
                sock.sendall(b"1000\r\n" + b"a" * 4096 + b"\r\n")
            sock.sendall(b"0\r\n\r\n")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except OSError:
                closed = True
                break
            if not data:
                closed = True
                break
            received += data
        elapsed = time.monotonic() - started
    return elapsed, received, closed


def _read_one_response(sock: socket.socket, buffered: bytes) -> tuple[bytes, bytes] | None:
    """One ``Content-Length``-framed response off ``sock``, or None at EOF.

    Returns ``(response, leftover)``. None means the server ended the
    connection before a complete response — end-of-file or a reset — which is
    what the keep-alive controls must fail on. A socket timeout is raised
    instead: a server that leaves the connection open and never answers has
    not closed it, and the failure should say so.
    """

    data = buffered
    while b"\r\n\r\n" not in data:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            raise
        except OSError:
            return None
        if not chunk:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lengths = [
        line.split(b":", 1)[1].strip()
        for line in head.split(b"\r\n")[1:]
        if line.lower().startswith(b"content-length:")
    ]
    # These controls only issue requests whose responses are length-framed; a
    # chunked answer would need a decoder this helper deliberately lacks.
    assert len(lengths) == 1, head
    size = int(lengths[0])
    while len(rest) < size:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            raise
        except OSError:
            return None
        if not chunk:
            return None
        rest += chunk
    return head + b"\r\n\r\n" + rest[:size], rest[size:]


def _requests_over_one_socket(base_url: str, requests: list[bytes]) -> list[bytes]:
    """Send ``requests`` one after another on **one** raw socket.

    Returns the responses that were served, in order; a list shorter than
    ``requests`` means the server closed or reset the connection before
    serving the next one. A server that never answers raises ``TimeoutError``.
    A raw socket rather than an HTTP client because a client library quietly
    opens a fresh connection when the server closes one, so "the second
    request was served" proves nothing about reuse there. Here there is only
    the one socket: a second response on it *is* the connection kept alive.
    """

    parts = urlsplit(base_url)
    served: list[bytes] = []
    leftover = b""
    with socket.create_connection((parts.hostname, parts.port), timeout=15) as sock:
        for raw in requests:
            try:
                sock.sendall(raw)
            except OSError:
                break
            result = _read_one_response(sock, leftover)
            if result is None:
                break
            response, leftover = result
            served.append(response)
    return served


def _raw_post(base_url: str, path: str, *, headers: dict[str, str], body: bytes) -> bytes:
    """A ``Content-Length``-framed HTTP/1.1 POST, as bytes, with no ``Connection`` header.

    HTTP/1.1's default is persistent, so leaving the header out asks for
    keep-alive the way an ordinary client does.
    """

    parts = urlsplit(base_url)
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {parts.hostname}:{parts.port}",
        f"Content-Length: {len(body)}",
        *(f"{name}: {value}" for name, value in headers.items()),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _sign_in_over_http(client: _LoopbackBrowser, idp: _StubIdp) -> str:
    start = client.get("/web/signin", params={"next": ACCEPT_PAGE_PATH})
    assert start.status_code == 303
    params = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    idp.nonce = params["nonce"]
    idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": True}
    callback = client.get("/web/oidc/callback", params={"code": "stub-code", "state": params["state"]})
    assert callback.status_code == 303, callback.text
    return client.get(ACCEPT_SESSION_PATH).json()["csrf_token"]


def test_an_oversize_request_does_not_hold_the_connection(tmp_path, idp):
    """The gate-3 major, asserted at the socket.

    Stopping the read at the cap was the right fix for memory and the wrong
    one for connections: answering an HTTP/1.1 request whose body has not been
    read to end-of-message leaves the server unable to begin the next cycle on
    that connection, so it holds it — the same exhaustion, moved from one
    resource to another.

    A raw socket rather than ``httpx``, because the property is *"the server
    let this connection go"* and that is not something a client library
    reports. End-of-file on a socket the client never closed is the direct
    observation.

    **What actually failed without the fix is the timing, not the close.**
    Measured against this uvicorn: the response comes back immediately either
    way, and the connection is eventually released either way — but without
    the header it is held for about ten seconds first, while the server works
    through what the client is still sending, versus under one second with it.
    So the assertion that does the work here is the elapsed time; the
    header and the EOF are corroborating detail. That distinction is worth
    keeping in the test rather than in a commit message, because a future
    reader measuring "does it close" would conclude the fix is unnecessary.

    64 KiB is deliberate: comfortably past the 2 KiB cap, and small enough to
    fit in socket buffers so the send cannot block and confuse the result.
    """

    app, service = build_app(tmp_path, idp)
    with running_server(app) as base_url, _LoopbackBrowser(base_url) as browser:
        csrf = _sign_in_over_http(browser, idp)
        elapsed, received, closed = _post_body_over_socket(
            base_url,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Cookie": browser.cookie_header(),
            },
        )

    # The timing first, deliberately: it is the property that regressed, and
    # the one that fails when the header is removed. Under one second with the
    # close; about ten without, which is exactly the connection being held.
    assert elapsed < 3, f"the connection was held for {elapsed:.1f}s after the refusal"
    assert closed, "the server kept the connection open after refusing an oversize body"
    assert received.startswith(b"HTTP/1.1 413"), received[:200]
    assert b"connection: close" in received.lower()
    assert service.calls == []


def test_a_guard_refusal_does_not_hold_a_body_bearing_connection(tmp_path, idp):
    """The one refusal the handler's fix cannot reach.

    ``WebSessionGuardMiddleware`` answers **before routing**, so an
    unauthenticated ``POST`` to the redemption endpoint is refused without the
    handler — and therefore without the handler's close — ever running. The
    same body-bearing connection is held, one layer earlier.

    Both of the guard's HTTP refusal shapes are covered: the sign-in redirect
    for a request with no session, and the 503 for a deployment where the
    session cannot be decided at all. The WebSocket refusal needs nothing —
    a handshake carries no body and that path closes the socket outright.
    """

    app, _ = build_app(tmp_path, idp)
    with running_server(app) as base_url:
        anonymous = _post_body_over_socket(base_url, headers={"Content-Type": "application/json"})

    # A different app: the guard cannot reach the surface's codec, so it
    # serves the documented 503 instead of a sign-in redirect.
    unavailable_app, _ = build_app(tmp_path, idp)
    unavailable_app.state.web_surface = None
    with running_server(unavailable_app) as base_url:
        undecidable = _post_body_over_socket(base_url, headers={"Content-Type": "application/json"})

    for label, status_line, (elapsed, received, closed) in (
        ("sign-in redirect", b"HTTP/1.1 303", anonymous),
        ("authorization unavailable", b"HTTP/1.1 503", undecidable),
    ):
        assert elapsed < 3, f"{label} held the connection for {elapsed:.1f}s"
        assert closed, f"{label} kept the connection open"
        assert received.startswith(status_line), (label, received[:200])
        assert b"connection: close" in received.lower(), label
        # The refusal is still the one #88 documents, headers and all.
        assert b"referrer-policy: no-referrer" in received.lower(), label


def test_the_server_keeps_serving_after_refusing_an_oversize_body(tmp_path, idp):
    # The close must free the connection, not the process: a caller who does
    # this repeatedly must not be able to take the surface down.
    app, _ = build_app(tmp_path, idp)
    with running_server(app) as base_url, _LoopbackBrowser(base_url) as browser:
        csrf = _sign_in_over_http(browser, idp)
        for _ in range(5):
            with contextlib.suppress(httpx.HTTPError):
                browser.post(
                    ACCEPT_REDEEM_PATH,
                    content=json.dumps({"token": "a" * 40_000}),
                    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
                )
        assert browser.get("/health").status_code == 200


def test_a_normal_redemption_over_a_real_connection_keeps_it_alive(tmp_path, idp):
    """The close must be the exception, not the rule.

    Two redemptions on **one socket**: if the successful path closed the
    connection, every acceptance would cost a fresh TCP (and TLS) handshake.
    Asserted as reuse — the second request is served on the same connection —
    rather than as the absence of a ``Connection: close`` header, which would
    still pass if the server dropped the connection for any other reason.
    """

    app, service = build_app(tmp_path, idp)
    with running_server(app) as base_url, _LoopbackBrowser(base_url) as client:
        csrf = _sign_in_over_http(client, idp)
        request = _raw_post(
            base_url,
            ACCEPT_REDEEM_PATH,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Cookie": client.cookie_header(),
            },
            body=json.dumps({"token": SENTINEL_TOKEN}).encode(),
        )
        served = _requests_over_one_socket(base_url, [request, request])
    assert len(served) == 2, "the server closed the connection after a successful redemption"
    assert all(response.startswith(b"HTTP/1.1 200") for response in served), served
    assert len(service.calls) == 2


# ===========================================================================
# Live-Postgres end to end (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# ===========================================================================

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live acceptance test",
)

COLLAB_TABLES = (
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_org_members",
    "collab_platform_roles",
    "collab_audit_events",
    "collab_orgs",
    "collab_schema_migrations",
)


def _database():
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=6, timeout_seconds=15.0)


@pytest.fixture
def live_db():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations

    database = _database()

    def drop():
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop()
        run_collab_schema_migrations(database)
        yield database
        drop()
    finally:
        database.close()


@pytest_asyncio.fixture
async def live_app(tmp_path, idp, live_db):
    values = web_values(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})
    values["frames"]["postgres"] = {"url": POSTGRES_URL, "auto_migrate": True}
    values["frames"]["orgs"] = {"backend": "postgres"}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        yield app


@live_postgres
async def test_live_an_invitation_is_redeemed_through_the_browser_page(live_app, idp, live_db):
    """The real thing: a real invitation row, redeemed by the real page.

    Everything above stubs the lifecycle service so that presentation can be
    covered exhaustively without a database. This one closes the loop — the
    digest the page's endpoint computes has to match the one #89's issuance
    stored, or nothing else in this file means anything.
    """

    from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
    from collab_hub_api.frames.invitations import PostgresInvitationService
    from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR

    service = PostgresInvitationService(live_db)
    operator = AuthContext(
        user="0perator-1111-4111-8111-abcdefabcdef",
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(email="op@example.com", email_verified=True),
        org_role=None,
        platform_role=PLATFORM_ROLE_OPERATOR,
    )
    issued = service.create(operator, email=INVITEE_EMAIL, org_id=None)
    token = issued.raw_secret.reveal()
    link = build_setup_url("https://web.test/invite/accept", token)
    assert token in link.split("#", 1)[1] and "#" in link

    async with web_client(live_app) as client:
        await client.get(link)  # the fragment stays in the browser
        idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": True}
        await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
        first = await redeem(client, token=token)
        second = await redeem(client, token=token)

    assert first.status_code == 200
    assert first.json() == {"outcome": OUTCOME_ACCEPTED}
    # A replay by the same login is the same success with nothing created —
    # #89's semantics, and what keeps a reloaded page from stranding someone.
    assert second.status_code == 200
    assert second.json() == {"outcome": OUTCOME_ACCEPTED}

    with live_db.connection() as conn:
        members = conn.execute("SELECT user_id, role FROM collab_org_members WHERE user_id = %s", (idp.sub,)).fetchall()
        # By actor, not by action name: this is an org-creating invitation,
        # so #89 records it as `org.create` rather than `invitation.redeem`.
        # The question the replay raises is "did the second POST write
        # anything at all", and the actor is what answers it.
        redemptions = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE actor = %s", (idp.sub,)
        ).fetchone()["n"]
        audited_secrets = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE detail::text LIKE %s",
            (f"%{token}%",),
        ).fetchone()["n"]
        row = conn.execute("SELECT status FROM collab_invitations WHERE id = %s", (issued.invitation.id,)).fetchone()
    assert len(members) == 1
    assert row["status"] == "accepted"
    assert redemptions == 1, "the replay must not write a second audit row"
    assert audited_secrets == 0, "no audit row may quote the raw secret"


@live_postgres
async def test_live_a_stale_token_answers_already_used_to_a_second_login(live_app, idp, live_db):
    from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
    from collab_hub_api.frames.invitations import PostgresInvitationService
    from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR

    service = PostgresInvitationService(live_db)
    operator = AuthContext(
        user="0perator-1111-4111-8111-abcdefabcdef",
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(email="op@example.com", email_verified=True),
        org_role=None,
        platform_role=PLATFORM_ROLE_OPERATOR,
    )
    issued = service.create(operator, email=INVITEE_EMAIL, org_id=None)
    token = issued.raw_secret.reveal()

    async with web_client(live_app) as client:
        idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": True}
        await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
        assert (await redeem(client, token=token)).status_code == 200

    idp.sub = "someone-else-0000-4000-8000-abcdefabcdef"
    async with web_client(live_app) as client:
        idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": True}
        await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
        replay = await redeem(client, token=token)
    assert replay.status_code == 410
    assert replay.json() == {"outcome": "invitation_already_used"}


@live_postgres
async def test_live_an_expired_invitation_renders_its_own_state(live_app, idp, live_db):
    from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
    from collab_hub_api.frames.invitations import PostgresInvitationService
    from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR

    service = PostgresInvitationService(live_db)
    operator = AuthContext(
        user="0perator-1111-4111-8111-abcdefabcdef",
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(email="op@example.com", email_verified=True),
        org_role=None,
        platform_role=PLATFORM_ROLE_OPERATOR,
    )
    issued = service.create(operator, email=INVITEE_EMAIL, org_id=None)
    with live_db.connection() as conn:
        conn.execute(
            "UPDATE collab_invitations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (issued.invitation.id,),
        )

    async with web_client(live_app) as client:
        idp.claims_override = {"email": INVITEE_EMAIL, "email_verified": True}
        await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
        response = await redeem(client, token=issued.raw_secret.reveal())
    assert response.status_code == 410
    assert response.json() == {"outcome": "invitation_expired"}
    assert datetime.now(tz=timezone.utc) - issued.invitation.created_at < timedelta(minutes=5)


@pytest.mark.asyncio
async def test_the_app_is_told_whether_the_deployment_requires_a_verified_address(tmp_path, idp) -> None:
    """The wiring seam for the page's copy, exercised end to end.

    Where a verified address is not required, the app must not tell invitees to
    follow a verification link for mail the deployment never sends. Which copy
    it shows is its own business (and its own suite's); what it shows it *on* is
    this value, and a builder that ignored the setting would leave every other
    test green. So this goes through `make_app` and asks over HTTP, which is the
    only way the value's whole path is exercised: config -> make_routers ->
    the session answer.
    """

    for flag in (True, False):
        # No Postgres URL: the route mounts on `config.web.enabled` alone, and
        # a URL here would have `make_app`'s schema-version check dial it.
        values = web_values(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})
        values["frames"]["invitations"] = {"require_verified_email": flag}
        app = make_app(Config.parse(values))
        async with web_client(app) as client:
            response = await client.get(ACCEPT_SESSION_PATH)
        assert response.status_code == 200
        assert response.json()["require_verified_email"] is flag
