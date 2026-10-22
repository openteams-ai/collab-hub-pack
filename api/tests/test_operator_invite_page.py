"""The operator invitation page (issue #91), including its amendment.

What these prove, and what they deliberately do not.

**Proven here.** A signed-in non-operator reaches neither the page nor the
action, and the action refuses independently of the route it was reached
through. Every invitation the page issues carries a null ``org_id``, so no
organization row exists until acceptance — checked against a live database by
issuing, revoking, and finding nothing left behind. Issuing twice for one
address mints no second live token, including when the two issuances are
genuinely concurrent. Issuing hands the secret to the invitation mail adapter
and the page reports the sanitized outcome. And the token: it reaches the
handler through an **in-process** call — proven by issuing on an app whose
``/v1`` invitation routes have been removed — appears in **no** HTTP response
at all, and is in no log record at any level.

**Previously proven here, and deliberately inverted:** between the 2026-08-07
amendment to #91 and the completion of an internal issue,
this page rendered the live secret for an operator to send by hand and
suppressed the SES send. ``web/invite_link.py`` held that decision and its end
condition; the condition is met, the module is deleted, and the tests that
pinned the rendered link are gone with it. One secret, one route.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient

# #88's live stub IdP and sign-in helpers, reused for the same reason #90
# reused them: this page has to work on the surface as built.
from test_web_surface import (  # noqa: E402
    _StubIdp,
    make_web_app,
    sign_in,
    web_client,
    web_values,
)

from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
from collab_hub_api.frames.credentials import InvitationSecret
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.invitation_email import (
    DELIVERY_FAILED,
    DELIVERY_PROVIDER_ACCEPTED,
    DELIVERY_UNKNOWN,
    DeliveryOutcome,
)
from collab_hub_api.frames.invitations import (
    Invitation,
    InvitationAlreadyUsedError,
    InvitationNotFoundError,
    InvitationPage,
    InvitationsUnavailableError,
    IssuedInvitation,
    LiveInvitationExists,
    invitation_email_lock_key,
)
from collab_hub_api.frames.org_source import (
    DEFAULT_ORG_ENV,
    DEFAULT_WORKSPACE_ENV,
    ORG_SOURCE_ENV,
)
from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR
from collab_hub_api.routers import admin as admin_router
from collab_hub_api.routers.invitations import InvitationCreateResponse
from collab_hub_api.web.acceptance import ACCEPT_PAGE_PATH
from collab_hub_api.web.admin import (
    EMAIL_FIELD,
    INVITATION_ID_FIELD,
    NOTICE_ALREADY_LIVE,
    NOTICE_INVALID_EMAIL,
    NOTICE_ISSUED_SEND_FAILED,
    NOTICE_ISSUED_SEND_UNKNOWN,
    NOTICE_ISSUED_SENT,
    NOTICE_NOT_FOUND,
    NOTICE_REVOKE_REFUSED,
    NOTICE_REVOKED,
    NOTICE_UNAVAILABLE,
    NOTICES,
)
from collab_hub_api.web.pages import CONTENT_SECURITY_POLICY, headers_for_path
from collab_hub_api.web.surface import (
    ADMIN_INVITATIONS_PATH,
    ADMIN_INVITATIONS_REVOKE_PATH,
    ADMIN_PATHS,
    PUBLIC_WEB_PATHS,
)

SENTINEL_TOKEN = "S3cr3tOperatorTokenThatMustNotLeak"
"""A token satisfying the accept model's alphabet, so a redaction that only
held for values validation rejects would not pass here.

The mail adapter is the one thing in these tests allowed to see it."""

OPERATOR_SUB = "subject-alice"
"""The stub IdP's subject, and the identity the pin resolves to."""

INVITEE = "bob@example.com"

PUBLIC_BASE_URL = "https://web.test"
"""The deployment's configured external origin.

Required at startup on a membership-resolving deployment: the browser surface
builds its own absolute URLs (the OIDC ``redirect_uri`` among them) from this
rather than from a request whose ``Host`` a caller chooses.
"""


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
    """Membership mode, which is what makes an operator role exist at all.

    Under claims-sourced auth the ``collab_`` tables are never read, so
    ``platform_role`` is structurally ``None`` and there are no operators —
    which is why ``make_app`` does not mount this page there. A test asserts
    that directly.
    """

    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)



def notice_text(kind: str, address: str = "") -> str:
    """One notice's copy, escaped the way the page renders it.

    Escaped rather than compared raw because these sentences contain
    apostrophes, and :func:`html.escape` turns those into entities — a raw
    comparison would silently pass for the sentences that happen not to.
    """

    return html.escape(NOTICES[kind].format(address=address))


def an_invitation(
    *,
    invitation_id: str = "inv-1",
    email: str = INVITEE,
    status: str = "pending",
    org_id: str | None = None,
    expires_in: timedelta = timedelta(days=7),
) -> Invitation:
    # An arbitrary fixture expiry, deliberately NOT ``INVITATION_TTL``: what
    # these pages render is the stored ``expires_at``, so the tests must not
    # move when the default does.
    now = datetime.now(tz=timezone.utc)
    return Invitation(
        id=invitation_id,
        org_id=org_id,
        email=email,
        status=status,
        created_at=now - timedelta(minutes=1),
        created_by=OPERATOR_SUB,
        expires_at=now + expires_in,
    )


class FakeInvitationService:
    """Stands in for #89's lifecycle service, recording what it was asked.

    Installed on ``app.state`` because these tests do not run the lifespan.
    The lifecycle semantics are #89's and are tested there; the live-Postgres
    tests at the bottom of this file are what prove the two halves meet.
    """

    available = True

    def __init__(
        self,
        *,
        rows: list[Invitation] | None = None,
        issue_result=None,
        revoke_raises: Exception | None = None,
        unavailable: bool = False,
    ) -> None:
        self.rows = list(rows or [])
        self.issue_calls: list[dict] = []
        self.revoke_calls: list[dict] = []
        self.plain_create_calls: list[dict] = []
        self.issue_result = issue_result
        self.revoke_raises = revoke_raises
        self.unavailable = unavailable

    def _guard(self) -> None:
        if self.unavailable:
            raise InvitationsUnavailableError("no postgres here")

    def server_now(self) -> datetime:
        self._guard()
        return datetime.now(tz=timezone.utc)

    def list_all(self, *, limit: int, offset: int = 0) -> InvitationPage:
        self._guard()
        return InvitationPage(invitations=self.rows[:limit], has_more=len(self.rows) > limit)

    def create(self, ctx, *, email, org_id):  # pragma: no cover - must not be called
        self.plain_create_calls.append({"email": email, "org_id": org_id})
        raise AssertionError("the operator page must call create_unless_live, not create")

    def create_unless_live(self, ctx, *, email, org_id):
        self._guard()
        self.issue_calls.append({"actor": ctx.user, "email": email, "org_id": org_id})
        if self.issue_result is not None:
            return self.issue_result
        invitation = an_invitation(email=email, org_id=org_id)
        self.rows.insert(0, invitation)
        return IssuedInvitation(invitation=invitation, raw_secret=InvitationSecret(SENTINEL_TOKEN))

    def revoke(self, ctx, invitation_id, *, expect_org_id=None):
        self._guard()
        self.revoke_calls.append(
            {"actor": ctx.user, "invitation_id": invitation_id, "expect_org_id": expect_org_id}
        )
        if self.revoke_raises is not None:
            raise self.revoke_raises
        for index, row in enumerate(self.rows):
            if row.id == invitation_id:
                revoked = Invitation(**{**row.__dict__, "status": "revoked"})
                self.rows[index] = revoked
                return revoked
        raise InvitationNotFoundError("Invitation not found")


class RecordingDelivery:
    """The invitation mail seam, recording what it was handed.

    Every issue on this page goes through here — it is the secret's only route
    off the page — so these tests read the token from the recorded call rather
    than from a response body, which is the point.
    """

    configured = True

    def __init__(self, status: str = DELIVERY_PROVIDER_ACCEPTED, error_code: str | None = None):
        self.outcome = DeliveryOutcome(status=status, error_code=error_code)
        self.calls: list[dict] = []

    def deliver(self, *, invitation_id, recipient, invitation_secret, organization_name, expires_at):
        self.calls.append(
            {
                "invitation_id": invitation_id,
                "recipient": recipient,
                "invitation_secret": invitation_secret,
                "organization_name": organization_name,
                "expires_at": expires_at,
            }
        )
        return self.outcome

    def secret(self) -> str:
        assert len(self.calls) == 1, "exactly one send per issued invitation"
        return self.calls[0]["invitation_secret"]


def build_app(
    tmp_path,
    idp,
    *,
    operator: bool = True,
    role_source: bool = True,
    web: dict | None = None,
    delivery=None,
    **service_kwargs,
):
    """An app with the page mounted, a stub service, and a recording mail seam.

    ``operator`` chooses what the role source *answers*; ``role_source``
    chooses whether one exists at all. The two are different states and the
    surface answers them differently on purpose — a plain 403 for "not an
    operator", a loud 503 for "cannot tell".
    """

    app = make_web_app(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL, **(web or {})})
    service = FakeInvitationService(**service_kwargs)
    delivery = delivery if delivery is not None else RecordingDelivery()
    app.state.invitation_service = service
    app.state.invitation_email_delivery = delivery
    if role_source:
        # #88's documented seam, consulted only because these tests do not run
        # the lifespan that installs an org store, so `resolve_principal` — the
        # canonical source — is absent.
        app.state.platform_role_resolver = lambda user: (
            PLATFORM_ROLE_OPERATOR if operator and user == OPERATOR_SUB else None
        )
    return app, service, delivery


async def signed_in(client: AsyncClient, idp: _StubIdp):
    response = await sign_in(client, idp, next_path=ADMIN_INVITATIONS_PATH)
    assert response.status_code == 303
    return response


def csrf_from(document: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', document)
    assert match, "every form on this surface carries the CSRF token"
    return match.group(1)


async def issue(client: AsyncClient, *, email: str = INVITEE, csrf: str | None = None):
    page = await client.get(ADMIN_INVITATIONS_PATH)
    return await client.post(
        ADMIN_INVITATIONS_PATH,
        data={
            "csrf_token": csrf if csrf is not None else csrf_from(page.text),
            EMAIL_FIELD: email,
        },
    )


async def revoke(client: AsyncClient, invitation_id: str, *, csrf: str | None = None):
    page = await client.get(ADMIN_INVITATIONS_PATH)
    return await client.post(
        ADMIN_INVITATIONS_REVOKE_PATH,
        data={
            "csrf_token": csrf if csrf is not None else csrf_from(page.text),
            INVITATION_ID_FIELD: invitation_id,
        },
    )


# ===========================================================================
# Operators only — the page and the action, checked separately
# ===========================================================================


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_no_admin_path_is_ever_anonymous(path):
    assert path not in PUBLIC_WEB_PATHS


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_an_anonymous_browser_is_sent_to_sign_in(tmp_path, idp, path):
    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        got = await client.get(path)
        posted = await client.post(path, data={"x": "y"})
    for response in (got, posted):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/web/signin?")
    assert service.issue_calls == [] and service.revoke_calls == []


async def test_a_signed_in_non_operator_is_refused_with_a_page_not_a_shrug(tmp_path, idp):
    """The acceptance criterion: a refusal, not a 404 and not a blank page."""

    app, service, _ = build_app(tmp_path, idp, operator=False)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        posted = await client.post(
            ADMIN_INVITATIONS_PATH, data={"csrf_token": "x", EMAIL_FIELD: INVITEE}
        )
        revoked = await client.post(
            ADMIN_INVITATIONS_REVOKE_PATH, data={"csrf_token": "x", INVITATION_ID_FIELD: "inv-1"}
        )

    for response in (page, posted, revoked):
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "You don't have access to this page" in response.text
    assert service.issue_calls == [] and service.revoke_calls == []


async def test_a_deployment_that_cannot_decide_operator_authority_says_so(tmp_path, idp):
    """503, not 403: a missing role source locks every operator out, and that
    must not be indistinguishable from a correct refusal."""

    app, service, _ = build_app(tmp_path, idp, role_source=False)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.get(ADMIN_INVITATIONS_PATH)
    assert response.status_code == 503
    assert service.issue_calls == []


async def test_revoking_the_operator_role_locks_out_a_live_session(tmp_path, idp):
    app, service, _ = build_app(tmp_path, idp)
    allowed = {OPERATOR_SUB}
    app.state.platform_role_resolver = lambda user: (
        PLATFORM_ROLE_OPERATOR if user in allowed else None
    )
    async with web_client(app) as client:
        await signed_in(client, idp)
        assert (await client.get(ADMIN_INVITATIONS_PATH)).status_code == 200
        allowed.clear()
        assert (await client.get(ADMIN_INVITATIONS_PATH)).status_code == 403
        assert (await issue(client, csrf="whatever")).status_code == 403
    assert service.issue_calls == []


def _context(role: str | None) -> AuthContext:
    return AuthContext(
        user=OPERATOR_SUB,
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(email="op@example.com", email_verified=True),
        org_role=None,
        platform_role=role,
    )


@pytest.mark.parametrize("role", [None, "owner", "member", "Operator", "OPERATOR"])
def test_the_action_itself_refuses_without_the_operator_platform_role(role):
    """#87's guard is on the action, not only on the route.

    Called directly, the way a future CLI or MCP tool would call it, so the
    router's dependency is not in the picture at all.
    """

    from fastapi import HTTPException

    service = FakeInvitationService()
    for call in (
        lambda: admin_router.issue_invitation(_context(role), service, email=INVITEE),
        lambda: admin_router.revoke_invitation(_context(role), service, invitation_id="inv-1"),
    ):
        with pytest.raises(HTTPException) as raised:
            call()
        assert raised.value.status_code == 403
    assert service.issue_calls == [] and service.revoke_calls == []


def test_an_org_role_never_stands_in_for_the_platform_role():
    """The two axes are separate and neither implies the other (#87)."""

    from fastapi import HTTPException

    owner_of_everything = AuthContext(
        user=OPERATOR_SUB,
        home_org_id="org-a",
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(email="op@example.com"),
        org_role="owner",
        platform_role=None,
    )
    with pytest.raises(HTTPException):
        admin_router.issue_invitation(owner_of_everything, FakeInvitationService(), email=INVITEE)


async def test_the_context_carries_the_resolved_role_rather_than_a_constant(tmp_path, idp):
    """The single most important line in ``web.operator``.

    Stamping ``platform_role="operator"`` because the request got this far
    would make #87's guard compare a constant with itself. Here the resolver
    answers "operator" once (satisfying the page's own gate) and then stops;
    the action must refuse, which it can only do if the value it checked came
    from the second resolution rather than from an assumption.
    """

    app, service, _ = build_app(tmp_path, idp)
    answers = [PLATFORM_ROLE_OPERATOR, PLATFORM_ROLE_OPERATOR, None]

    def resolver(user: str):
        return answers.pop(0) if answers else None

    app.state.platform_role_resolver = resolver
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)  # consumes the first answer
        response = await client.post(
            ADMIN_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
    assert page.status_code == 200
    assert response.status_code == 403
    assert service.issue_calls == []


async def test_the_page_is_absent_where_operators_cannot_exist(tmp_path, idp, monkeypatch):
    """Claims-sourced deployments have no platform-role axis at all, so the
    page is not mounted — absent is a truer answer than refusing everyone."""

    monkeypatch.setenv(ORG_SOURCE_ENV, "claims")
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    app = make_web_app(tmp_path, idp)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert ADMIN_INVITATIONS_PATH not in paths
    assert ADMIN_INVITATIONS_REVOKE_PATH not in paths


async def test_the_page_is_absent_on_a_single_organization_deployment(tmp_path, idp, monkeypatch):
    """The page's one send is the org-creating invitation, which a deployment
    declaring a single organization (issue #91) refuses — absent is a truer
    answer than a page whose every submit is refused. The owner surface and
    the /v1 operator routes still serve invitations into the declared org."""

    monkeypatch.setenv(ORG_SOURCE_ENV, "single")
    monkeypatch.setenv("FRAMES_AUTH_SINGLE_ORG_ID", "org-solo")
    monkeypatch.setenv("FRAMES_AUTH_SINGLE_ORG_NAME", "Example Corp")
    monkeypatch.setenv("FRAMES_AUTH_SINGLE_ORG_MEMBER_SOURCES", "corp-google")
    app = make_web_app(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})
    paths = {getattr(route, "path", None) for route in app.routes}
    assert ADMIN_INVITATIONS_PATH not in paths
    assert ADMIN_INVITATIONS_REVOKE_PATH not in paths


# ===========================================================================
# Issuing: null org_id, exact address, one live token per address
# ===========================================================================


async def test_issuing_creates_an_org_creating_invitation_and_nothing_else(tmp_path, idp):
    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert service.issue_calls == [{"actor": OPERATOR_SUB, "email": INVITEE, "org_id": None}]
    # The plain `create` would have skipped the one-live-token rule.
    assert service.plain_create_calls == []


async def test_the_address_is_trimmed_and_ascii_lowercased(tmp_path, idp):
    """Amended on #157: lowered here, because Keycloak asserts it lowered.

    The form normalizes once, at the same point it validates, so the address
    the service stores, the listing shows, and the onboarding email tells the
    invitee to use are one string. Under the previous exact-match rule a
    capital typed here produced an invitation nobody could ever redeem.

    Only case and surrounding whitespace change: the dot is still part of the
    address, not a spelling to canonicalize away.
    """

    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        await issue(client, email="  Bob.Smith@Example.COM  ")
    assert service.issue_calls[0]["email"] == "bob.smith@example.com"


async def test_the_page_offers_no_way_to_name_an_organization(tmp_path, idp):
    """Pre-creating or naming an org here would contradict Gate B and would be
    the cross-org capability Gate E scoped out."""

    app, _, _ = build_app(tmp_path, idp, rows=[an_invitation()])
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
    fields = re.findall(r'<input[^>]*name="([^"]+)"', page.text)
    assert set(fields) <= {"csrf_token", EMAIL_FIELD, INVITATION_ID_FIELD}
    assert "org_id" not in page.text


@pytest.mark.parametrize("address", ["", "   ", "not an address", "a@b@c", "Bob <bob@x.test>", "bob@"])
async def test_an_unusable_address_creates_nothing(tmp_path, idp, address):
    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client, email=address)
    assert response.status_code == 400
    assert notice_text(NOTICE_INVALID_EMAIL) in response.text
    assert service.issue_calls == []


async def test_issuing_twice_for_one_address_mints_no_second_token(tmp_path, idp):
    """The amendment's criterion, at the page level: the refusal renders no
    link at all, and points at the remedy that exists on this same page."""

    existing = an_invitation(invitation_id="inv-live")
    app, service, _ = build_app(
        tmp_path,
        idp,
        rows=[existing],
        issue_result=LiveInvitationExists(existing=existing),
    )
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 409
    assert "revoke that invitation below" in response.text
    assert SENTINEL_TOKEN not in response.text
    assert "<code>" not in response.text
    assert "<textarea" not in response.text
    assert NOTICE_ALREADY_LIVE in NOTICES


async def test_an_unavailable_service_creates_nothing_and_says_so(tmp_path, idp):
    app, service, _ = build_app(tmp_path, idp, unavailable=True)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        response = await client.post(
            ADMIN_INVITATIONS_PATH, data={"csrf_token": "x", EMAIL_FIELD: INVITEE}
        )
    assert page.status_code == 503
    assert notice_text(NOTICE_UNAVAILABLE) in page.text
    # The CSRF refusal comes first; the point here is only that the page still
    # renders rather than raising through the surface.
    assert response.status_code == 403


# ===========================================================================
# Revoke
# ===========================================================================


async def test_revoking_names_the_invitation_and_stays_hub_scoped(tmp_path, idp):
    app, service, _ = build_app(tmp_path, idp, rows=[an_invitation(invitation_id="inv-9")])
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await revoke(client, "inv-9")
    assert response.status_code == 200
    assert service.revoke_calls == [
        {"actor": OPERATOR_SUB, "invitation_id": "inv-9", "expect_org_id": None}
    ]
    assert "Revoked" in response.text


@pytest.mark.parametrize(
    ("raises", "status_code", "notice"),
    [
        (InvitationNotFoundError("no"), 404, NOTICE_NOT_FOUND),
        (InvitationAlreadyUsedError("no"), 409, NOTICE_REVOKE_REFUSED),
        (InvitationsUnavailableError("no"), 503, NOTICE_UNAVAILABLE),
    ],
    ids=["not_found", "already_accepted", "unavailable"],
)
async def test_a_revoke_that_cannot_happen_renders_its_own_sentence(
    tmp_path, idp, raises, status_code, notice
):
    app, _, _ = build_app(tmp_path, idp, revoke_raises=raises)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await revoke(client, "inv-missing")
    assert response.status_code == status_code
    assert notice_text(notice) in response.text


async def test_only_revocable_rows_get_a_revoke_control(tmp_path, idp):
    rows = [
        an_invitation(invitation_id="pending-1"),
        an_invitation(invitation_id="accepted-1", status="accepted"),
        an_invitation(invitation_id="revoked-1", status="revoked"),
        an_invitation(invitation_id="lapsed-1", expires_in=-timedelta(seconds=1)),
    ]
    app, _, _ = build_app(tmp_path, idp, rows=rows)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
    offered = set(re.findall(rf'name="{INVITATION_ID_FIELD}" value="([^"]+)"', page.text))
    # A lapsed invitation is still revocable — that is how an issuer retires a
    # link without waiting out its expiry. An accepted one is not: undoing an
    # acceptance is member removal, a different action.
    assert offered == {"pending-1", "lapsed-1"}
    assert "Expired" in page.text and "Accepted" in page.text


# ===========================================================================
# CSRF, request size, and what a form may be
# ===========================================================================


@pytest.mark.parametrize("csrf", ["", "not-the-token"])
async def test_a_post_without_the_session_csrf_token_changes_nothing(tmp_path, idp, csrf):
    app, service, _ = build_app(tmp_path, idp, rows=[an_invitation(invitation_id="inv-9")])
    async with web_client(app) as client:
        await signed_in(client, idp)
        issued = await issue(client, csrf=csrf)
        revoked = await revoke(client, "inv-9", csrf=csrf)
    assert issued.status_code == 403 and revoked.status_code == 403
    assert service.issue_calls == [] and service.revoke_calls == []


async def test_a_csrf_token_from_another_session_is_refused(tmp_path, idp):
    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as first:
        await signed_in(first, idp)
        stolen = csrf_from((await first.get(ADMIN_INVITATIONS_PATH)).text)
    async with web_client(app) as second:
        await signed_in(second, idp)
        response = await issue(second, csrf=stolen)
    assert response.status_code == 403
    assert service.issue_calls == []


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_an_oversized_form_is_refused_before_it_is_parsed(tmp_path, idp, path):
    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        response = await client.post(
            path,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: "a" * admin_router.MAX_FORM_BYTES},
        )
    assert response.status_code == admin_router.REQUEST_TOO_LARGE
    assert service.issue_calls == [] and service.revoke_calls == []


@pytest.mark.parametrize(
    "content_type",
    [
        "application/x-www-form-urlencoded",
        "application/x-www-form-urlencoded; charset=UTF-8",
        "APPLICATION/X-WWW-FORM-URLENCODED",
        " application/x-www-form-urlencoded ;charset=utf-8",
    ],
    ids=["exact", "charset_param", "uppercase", "padded"],
)
async def test_the_form_type_is_matched_as_a_media_type(tmp_path, idp, content_type):
    """Parameters and case do not change what the type is (#71).

    Media types are case-insensitive and ``charset`` is a parameter, so each
    of these is the urlencoded form and is read as one. The uppercase spelling
    is the case a prefix match got wrong in the other direction: it refused a
    form it should have read.
    """

    app, service, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        response = await client.post(
            ADMIN_INVITATIONS_PATH,
            content=urlencode({"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE}),
            headers={"Content-Type": content_type},
        )
    assert response.status_code == 201, response.text
    assert len(service.issue_calls) == 1


@pytest.mark.parametrize("path", ADMIN_PATHS)
@pytest.mark.parametrize(
    "content_type",
    [
        "application/x-www-form-urlencoded-not-really",
        "application/x-www-form-urlencodedx; charset=utf-8",
        "multipart/form-data; boundary=x",
        "text/plain",
        "",
        None,
    ],
    ids=["suffix_extended", "suffix_extended_with_param", "multipart", "text", "empty", "absent"],
)
async def test_anything_but_the_form_type_is_refused_unread(tmp_path, idp, path, content_type):
    """A type that merely *begins* with the form type is not the form type (#71).

    The gate used to be ``startswith``, so the first two were parsed as forms.
    They are refused with the same 415 multipart gets, before the body is read,
    and the connection is let go because that body was never consumed. The
    body is a valid form carrying the right token, so the only thing that can
    refuse it is the type gate.
    """

    pulled = 0

    app, service, _ = build_app(tmp_path, idp, rows=[an_invitation(invitation_id="inv-9")])
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        body = {"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE, INVITATION_ID_FIELD: "inv-9"}

        async def form_body():
            nonlocal pulled
            pulled += 1
            yield urlencode(body).encode()

        # None sends no Content-Type at all; httpx adds none for raw content.
        headers = {} if content_type is None else {"Content-Type": content_type}
        response = await client.post(path, content=form_body(), headers=headers)
        assert (content_type is None) == ("content-type" not in response.request.headers)
    assert response.status_code == admin_router.UNSUPPORTED_MEDIA_TYPE, response.text
    # Refused before the read, not after it: the server pulled nothing.
    assert pulled == 0
    assert response.headers.get("connection") == "close"
    assert service.issue_calls == [] and service.revoke_calls == []


async def test_an_overlong_address_is_refused_by_shape(tmp_path, idp):
    """`maxlength` is a hint to a browser and nothing at all to anything else."""

    app, service, _ = build_app(tmp_path, idp)
    long_address = "a" * 300 + "@example.com"
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client, email=long_address)
    assert response.status_code == 400
    assert service.issue_calls == []
    assert long_address not in response.text


# ===========================================================================
# The token: in-process, no response, no log
# ===========================================================================


async def test_the_page_issues_with_the_api_invitation_routes_removed(tmp_path, idp):
    """The amendment's criterion, proven structurally.

    If the token reached the page over HTTP, there would be an API route
    answering with it. Deleting every ``/v1`` invitation route and issuing
    anyway shows the page is not going through one — the call is in-process,
    the same call #89's router makes.
    """

    app, service, delivery = build_app(tmp_path, idp)
    removed = [
        route
        for route in list(app.router.routes)
        if "invitation" in (getattr(route, "path", "") or "").lower()
        and not getattr(route, "path", "").startswith("/admin")
    ]
    assert removed, "the API invitation routes must exist for this test to mean anything"
    for route in removed:
        app.router.routes.remove(route)

    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert delivery.secret() == SENTINEL_TOKEN
    assert len(service.issue_calls) == 1


async def test_no_http_client_is_used_to_obtain_the_token(tmp_path, idp, monkeypatch):
    """The same claim from the other side: nothing outbound happens.

    Patched after sign-in, so the OIDC round trip (which legitimately uses
    httpx) has already completed and any client construction during the issue
    is a failure rather than a fixture problem.
    """

    app, _, delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)

        import http.client
        import socket
        import urllib.request

        import httpx

        def refuse(*_args, **_kwargs):
            raise AssertionError("the operator page made an outbound HTTP call")

        # The async surface as well as the sync one: an `httpx.AsyncClient`
        # implementation would otherwise satisfy this test while doing exactly
        # what it forbids. Transports too, since a client can be handed one.
        for name in (
            "Client",
            "AsyncClient",
            "HTTPTransport",
            "AsyncHTTPTransport",
            "request",
            "get",
            "post",
        ):
            monkeypatch.setattr(httpx, name, refuse, raising=False)
        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        monkeypatch.setattr(http.client.HTTPConnection, "request", refuse)
        monkeypatch.setattr(http.client.HTTPSConnection, "request", refuse)
        # The floor under all of it: no library can reach a network without
        # opening a connection, so this catches one this list has not named.
        # The test client speaks ASGI in-process and opens none.
        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)

        response = await client.post(
            ADMIN_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
    assert response.status_code == 201
    # The send seam is in-process too, so the issue completes with every
    # outbound path refused — and the secret still went nowhere but there.
    assert delivery.secret() == SENTINEL_TOKEN


def whole_response(response) -> str:
    """A response in full: status line, every header, and the body.

    Bodies alone would not support the claim this file makes. A secret can
    leave in a ``Location``, a ``Set-Cookie``, an ``ETag``, or any header a
    future handler adds, and a check that only reads ``.text`` would report
    those responses clean. Headers are rendered with their names so a repeated
    one (``Set-Cookie``) is included once per value.
    """

    lines = [f"HTTP {response.status_code}"]
    lines += [f"{name}: {value}" for name, value in response.headers.multi_items()]
    lines.append(response.text)
    return "\n".join(lines)


async def test_no_response_of_the_whole_flow_carries_the_secret(tmp_path, idp):
    """Every response this browser received, in full, checked.

    The secret appears in none of them — not the issue response, not the
    listing that follows, not a reload, not anything else on the surface.
    Whole responses rather than bodies, so a header, a redirect ``Location``,
    or a cookie cannot be the hole this test claims to close. The client's
    cookie jar is checked too, since a ``Set-Cookie`` the browser accepted
    would otherwise have been read only as a header on the response that set
    it.

    Until #91's amendment lapsed, exactly one response carried it by design.
    That this now holds for *all* of them is the amendment lapsing, measured.
    """

    app, _, delivery = build_app(tmp_path, idp)
    seen: list[tuple[str, str]] = []

    def record(label: str, response) -> None:
        seen.append((label, whole_response(response)))

    async with web_client(app) as client:
        signin = await sign_in(client, idp, next_path=ADMIN_INVITATIONS_PATH)
        record("/web/oidc/callback", signin)
        record("/web", await client.get("/web"))
        page = await client.get(ADMIN_INVITATIONS_PATH)
        record(ADMIN_INVITATIONS_PATH, page)
        issued = await client.post(
            ADMIN_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
        record("POST " + ADMIN_INVITATIONS_PATH, issued)
        record("reload", await client.get(ADMIN_INVITATIONS_PATH))
        record("css", await client.get("/web/app.css"))
        record(ACCEPT_PAGE_PATH, await client.get(ACCEPT_PAGE_PATH))
        record("signed-out", await client.get("/web/signed-out"))
        jar = {name: value for name, value in client.cookies.items()}

    assert delivery.secret() == SENTINEL_TOKEN, "the flow must have minted it at all"
    assert [label for label, whole in seen if SENTINEL_TOKEN in whole] == []
    assert not any(SENTINEL_TOKEN in value for value in jar.values())


def test_the_api_create_response_has_no_field_a_token_could_ride():
    """No API surface had to be widened, versioned, or later deprecated.

    #89's create response is what the desktop is built against; the amendment
    turns on that response staying exactly as it is.
    """

    assert "token" not in InvitationCreateResponse.model_fields
    assert "secret" not in InvitationCreateResponse.model_fields
    assert "link" not in InvitationCreateResponse.model_fields
    assert "url" not in InvitationCreateResponse.model_fields


async def test_the_browser_session_is_not_api_credentials(tmp_path, idp):
    """The capability is not reachable from the page's own credential."""

    app, _, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await client.post(
            "/v1/operator/invitations", json={"email": INVITEE, "org_id": None}
        )
    assert response.status_code == 401


async def test_the_issued_page_offers_no_url_a_secret_could_ride(tmp_path, idp):
    """No href, action or src on the issue response carries a token — and none
    carries a fragment at all, which is where the secret used to travel."""

    app, _, delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)

    assert delivery.secret() == SENTINEL_TOKEN
    for url in re.findall(r'(?:href|action|src)="([^"]*)"', response.text):
        assert SENTINEL_TOKEN not in url
        assert "#" not in url
    assert "<code>" not in response.text
    assert "<textarea" not in response.text


async def test_the_secret_reaches_no_log_record(tmp_path, idp, caplog):
    """R3's logging half, across the whole flow — never relaxed, still held."""

    app, _, _ = build_app(tmp_path, idp)
    with caplog.at_level(logging.DEBUG):
        async with web_client(app) as client:
            await signed_in(client, idp)
            await issue(client)
            await revoke(client, "inv-1")
            await client.get(ADMIN_INVITATIONS_PATH)

    assert caplog.records, "the flow must actually log something for this to mean anything"
    for record in caplog.records:
        assert SENTINEL_TOKEN not in record.getMessage()
        assert SENTINEL_TOKEN not in repr(record.args)
        for value in record.__dict__.values():
            assert SENTINEL_TOKEN not in repr(value), record.name


async def test_the_submitted_address_is_not_logged_either(tmp_path, idp, caplog):
    """Log lines name the invitation id, which is opaque; #87's audit row is
    where the address belongs, behind database access."""

    app, _, _ = build_app(tmp_path, idp)
    with caplog.at_level(logging.DEBUG):
        async with web_client(app) as client:
            await signed_in(client, idp)
            await issue(client, email="private.person@example.com")
    for record in caplog.records:
        assert "private.person@example.com" not in repr(record.__dict__)


# ===========================================================================
# Headers: an operator page, cached nowhere
# ===========================================================================


async def test_the_page_carries_no_store_no_referrer_and_the_no_script_policy(tmp_path, idp):
    app, _, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        issued = await issue(client)

    for response in (page, issued):
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        csp = response.headers["content-security-policy"]
        assert csp == CONTENT_SECURITY_POLICY
        assert "script" not in csp
    assert 'content="no-referrer"' in issued.text


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_the_acceptance_pages_script_budget_does_not_reach_admin(path):
    assert headers_for_path(path)["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


async def test_a_hardened_map_that_hides_admin_fails_the_rollout(tmp_path, idp):
    """An operator holds a browser session, not a bearer token, so a map that
    does not leave /admin public would refuse every operator action with a
    credential error the page cannot explain."""

    values = web_values(
        tmp_path,
        idp,
        web={"public_base_url": PUBLIC_BASE_URL},
        security={
            "paths": [
                {"path": "/web", "match": "prefix", "access": "public"},
                {"path": "/invite", "match": "prefix", "access": "public"},
            ],
            "default_access": "authenticated",
        },
    )
    with pytest.raises(RuntimeError, match="/admin/invitations"):
        make_app(Config.parse(values))


# ===========================================================================
# The SES send — the secret's one route off this page
# ===========================================================================


async def test_issuing_hands_the_secret_to_the_mail_adapter_and_renders_none_of_it(tmp_path, idp):
    """The restored send (#91's amendment lapsing), from the page's side.

    The operator page composes nothing and copies nothing: the same adapter
    #89's API route uses is handed the freshly minted secret, and the page
    words the outcome.
    """

    app, _, delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert notice_text(NOTICE_ISSUED_SENT, INVITEE) in response.text
    assert len(delivery.calls) == 1
    call = delivery.calls[0]
    assert call["recipient"] == INVITEE
    assert call["invitation_secret"] == SENTINEL_TOKEN
    # An operator invitation creates the organization at acceptance, so there
    # is no organization to name in the message yet.
    assert call["organization_name"] is None
    assert SENTINEL_TOKEN not in response.text


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (DELIVERY_FAILED, NOTICE_ISSUED_SEND_FAILED),
        (DELIVERY_UNKNOWN, NOTICE_ISSUED_SEND_UNKNOWN),
    ],
)
async def test_a_failed_or_unconfirmed_send_is_worded_not_hidden(tmp_path, idp, status, kind):
    """The residual risk of sending after the commit, made visible.

    A send cannot be rolled back, so it happens after the audited transaction
    commits — which leaves the other failure available: a committed invitation
    whose mail did not go. The page says so rather than reporting success, and
    never quotes the provider's own error code.
    """

    app, _, _ = build_app(
        tmp_path, idp, delivery=RecordingDelivery(status=status, error_code="boom")
    )
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert notice_text(kind, INVITEE) in response.text
    assert "boom" not in response.text, "provider error codes are for the API, not the page"
    assert SENTINEL_TOKEN not in response.text


async def test_the_send_happens_only_after_the_invitation_exists(tmp_path, idp):
    """Ordering, not sequence-in-the-source: nothing is mailed for an
    invitation that was never created."""

    existing = an_invitation(invitation_id="inv-live")
    app, _, delivery = build_app(
        tmp_path,
        idp,
        rows=[existing],
        issue_result=LiveInvitationExists(existing=existing),
    )
    async with web_client(app) as client:
        await signed_in(client, idp)
        refused = await issue(client)
    assert refused.status_code == 409
    assert delivery.calls == []


# ===========================================================================
# The configured external origin
# ===========================================================================
#
# This requirement outlived the rendered link it was introduced for. It now
# stands on the plainer fact that the browser surface builds absolute URLs —
# the OIDC ``redirect_uri`` among them — and a request-derived base URL behind
# a proxy that does not forward its scheme comes out ``http://``, which
# Keycloak refuses.


def test_startup_refuses_a_membership_deployment_with_no_configured_origin(tmp_path, idp):
    """Refused at boot rather than at an operator's first sign-in."""

    values = web_values(tmp_path, idp)
    assert not values["web"].get("public_base_url")
    with pytest.raises(RuntimeError, match="public_base_url"):
        make_app(Config.parse(values))


def test_a_claims_deployment_still_needs_no_configured_origin(tmp_path, idp, monkeypatch):
    """The requirement follows the pages, not the surface.

    Claims-sourced deployments mount neither invitation page — there are no
    operators and no memberships there — so the setting stays optional.
    """

    monkeypatch.setenv(ORG_SOURCE_ENV, "claims")
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    app = make_app(Config.parse(web_values(tmp_path, idp)))
    assert app is not None


async def test_a_forged_host_cannot_choose_where_the_surface_points(tmp_path, idp):
    """A ``Host`` the operator's browser did not choose reaches nothing.

    The origin comes from configuration and from nowhere else, so the header is
    simply not an input — and the issue succeeds without it appearing anywhere
    in the response.
    """

    app, _, delivery = build_app(tmp_path, idp, web={"public_base_url": "https://collab.example.com"})
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
        response = await client.post(
            ADMIN_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
            headers={"Host": "attacker.example.net"},
        )
    assert response.status_code == 201
    assert delivery.secret() == SENTINEL_TOKEN
    assert "attacker.example.net" not in response.text


# ===========================================================================
# Copy and structure
# ===========================================================================


def test_every_notice_the_router_can_raise_has_copy():
    import inspect

    used = set(re.findall(r"Notice\((NOTICE_[A-Z_]+)", inspect.getsource(admin_router)))
    from collab_hub_api.web import admin as admin_page

    assert used, "the router must present at least one notice"
    for name in used:
        assert getattr(admin_page, name) in NOTICES, name
    assert set(NOTICES) == {
        NOTICE_ISSUED_SENT,
        NOTICE_ISSUED_SEND_FAILED,
        NOTICE_ISSUED_SEND_UNKNOWN,
        NOTICE_ALREADY_LIVE,
        NOTICE_INVALID_EMAIL,
        NOTICE_REVOKED,
        NOTICE_NOT_FOUND,
        NOTICE_REVOKE_REFUSED,
        NOTICE_UNAVAILABLE,
    }


async def test_a_hostile_address_cannot_inject_markup(tmp_path, idp):
    """The listing renders stored addresses; every one is escaped."""

    hostile = '"><script>alert(1)</script>@example.com'
    app, _, _ = build_app(tmp_path, idp, rows=[an_invitation(email=hostile)])
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ADMIN_INVITATIONS_PATH)
    assert "<script>" not in page.text
    assert "&lt;script&gt;" in page.text


async def test_the_overview_links_to_the_page(tmp_path, idp):
    app, _, _ = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        overview = await client.get("/web")
    assert f'href="{ADMIN_INVITATIONS_PATH}"' in overview.text


# ===========================================================================
# Live-Postgres (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# ===========================================================================

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live operator tests",
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


@pytest.fixture
def live_db():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=8, timeout_seconds=15.0)

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
    """A real app on a real database, with only the mail seam replaced.

    With the link display deleted, an invitation's secret leaves the page only
    through the delivery adapter — so a live test that has to redeem one reads
    it from a recording adapter here. That the real adapter renders and sends
    the same secret is ``test_invitation_email.py``'s subject.
    """

    values = web_values(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})
    values["frames"]["postgres"] = {"url": POSTGRES_URL, "auto_migrate": True}
    values["frames"]["orgs"] = {"backend": "postgres"}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        app.state.invitation_email_delivery = RecordingDelivery()
        with live_db.connection() as conn:
            conn.execute(
                "INSERT INTO collab_platform_roles (user_id, role, status, granted_by)"
                " VALUES (%s, 'operator', 'active', %s)"
                " ON CONFLICT (user_id) DO NOTHING",
                (OPERATOR_SUB, OPERATOR_SUB),
            )
        yield app


def _live_service(live_db):
    from collab_hub_api.frames.invitations import PostgresInvitationService

    return PostgresInvitationService(live_db)


@live_postgres
async def test_live_an_operator_takes_an_address_through_to_a_solo_org(live_app, idp, live_db):
    """The acceptance criterion, end to end, with no mail involved.

    Operator signs in and issues; the secret leaves through the mail seam (read
    here from the recording adapter) and an invitee redeems it through #90's
    acceptance page. What the audit log then says is the load-bearing part: the
    send is the operator's, the org creation is the accepter's.
    """

    from test_acceptance_page import redeem as redeem_through_page  # noqa: E402

    async with web_client(live_app) as operator:
        await sign_in(operator, idp, next_path=ADMIN_INVITATIONS_PATH)
        page = await operator.get(ADMIN_INVITATIONS_PATH)
        assert page.status_code == 200
        issued = await operator.post(
            ADMIN_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
    assert issued.status_code == 201
    token = live_app.state.invitation_email_delivery.secret()
    assert token not in issued.text, "the page renders the outcome, never the secret"
    link = f"{ACCEPT_PAGE_PATH}#token={token}"

    idp.sub = "invitee-0000-4000-8000-abcdefabcdef"
    idp.claims_override = {"email": INVITEE, "email_verified": True}
    async with web_client(live_app) as invitee:
        await invitee.get(link)  # the fragment stays in the browser
        await sign_in(invitee, idp, next_path=ACCEPT_PAGE_PATH)
        accepted = await redeem_through_page(invitee, token=token)
    assert accepted.status_code == 200

    with live_db.connection() as conn:
        membership = conn.execute(
            "SELECT user_id, org_id, role FROM collab_org_members"
        ).fetchall()
        orgs = conn.execute("SELECT id, created_by FROM collab_orgs").fetchall()
        events = conn.execute(
            "SELECT action, actor, target_label FROM collab_audit_events ORDER BY id"
        ).fetchall()
        leaked = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE detail::text LIKE %s",
            (f"%{token}%",),
        ).fetchone()["n"]

    assert len(membership) == 1
    assert membership[0]["role"] == "owner"
    assert membership[0]["user_id"] == idp.sub
    assert len(orgs) == 1 and orgs[0]["created_by"] == idp.sub
    assert [(row["action"], row["actor"]) for row in events] == [
        ("invitation.send", OPERATOR_SUB),
        ("org.create", idp.sub),
    ]
    assert leaked == 0, "no audit row may quote the raw secret"


@live_postgres
async def test_live_no_organization_exists_until_acceptance(live_app, idp, live_db):
    """Issue, revoke, and confirm nothing was left behind."""

    async with web_client(live_app) as operator:
        await sign_in(operator, idp, next_path=ADMIN_INVITATIONS_PATH)
        page = await operator.get(ADMIN_INVITATIONS_PATH)
        issued = await operator.post(
            ADMIN_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
        assert issued.status_code == 201
        with live_db.connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM collab_orgs").fetchone()["n"] == 0
            invitation_id = conn.execute(
                "SELECT id, org_id FROM collab_invitations"
            ).fetchone()
        assert invitation_id["org_id"] is None

        listing = await operator.get(ADMIN_INVITATIONS_PATH)
        revoked = await operator.post(
            ADMIN_INVITATIONS_REVOKE_PATH,
            data={
                "csrf_token": csrf_from(listing.text),
                INVITATION_ID_FIELD: invitation_id["id"],
            },
        )
    assert revoked.status_code == 200

    with live_db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM collab_orgs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM collab_org_members").fetchone()["n"] == 0
        actions = [
            row["action"]
            for row in conn.execute(
                "SELECT action FROM collab_audit_events ORDER BY id"
            ).fetchall()
        ]
        actors = {
            row["actor"]
            for row in conn.execute("SELECT actor FROM collab_audit_events").fetchall()
        }
    assert actions == ["invitation.send", "invitation.revoke"]
    assert actors == {OPERATOR_SUB}


@live_postgres
async def test_live_issuing_twice_mints_one_token_and_writes_one_event(live_app, idp, live_db):
    async with web_client(live_app) as operator:
        await sign_in(operator, idp, next_path=ADMIN_INVITATIONS_PATH)
        page = await operator.get(ADMIN_INVITATIONS_PATH)
        csrf = csrf_from(page.text)
        first = await operator.post(
            ADMIN_INVITATIONS_PATH, data={"csrf_token": csrf, EMAIL_FIELD: INVITEE}
        )
        second = await operator.post(
            ADMIN_INVITATIONS_PATH, data={"csrf_token": csrf, EMAIL_FIELD: INVITEE}
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert "<code>" not in second.text
    assert "<textarea" not in second.text

    with live_db.connection() as conn:
        rows = conn.execute("SELECT token_hash FROM collab_invitations").fetchall()
        sends = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE action = 'invitation.send'"
        ).fetchone()["n"]
    assert len(rows) == 1
    assert sends == 1, "a refusal is not an action and writes no event row"


@live_postgres
def test_live_concurrent_issues_for_one_address_produce_one_invitation(live_db):
    """The advisory lock, which is the part a check-then-insert cannot do.

    Two transactions that both read "no live invitation" and both insert is
    exactly what a double-submitted form produces, and a row that does not
    exist yet cannot be locked. This is the test that fails if the lock is
    removed.
    """

    from concurrent.futures import ThreadPoolExecutor

    service = _live_service(live_db)
    context = _context(PLATFORM_ROLE_OPERATOR)

    def issue_one():
        return service.create_unless_live(context, email=INVITEE, org_id=None)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = [future.result() for future in [pool.submit(issue_one) for _ in range(6)]]

    minted = [outcome for outcome in outcomes if isinstance(outcome, IssuedInvitation)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, LiveInvitationExists)]
    assert len(minted) == 1
    assert len(refused) == 5
    assert {outcome.existing.id for outcome in refused} == {minted[0].invitation.id}

    with live_db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM collab_invitations").fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM collab_audit_events WHERE action = 'invitation.send'"
            ).fetchone()["n"]
            == 1
        )


@live_postgres
@pytest.mark.parametrize("retired", ["revoked", "expired", "accepted"])
def test_live_a_retired_invitation_does_not_block_a_fresh_one(live_db, retired):
    """"Live" is pending and unexpired. Everything else is how an issuer makes
    room for a new link, and blocking on it would strand them."""

    service = _live_service(live_db)
    context = _context(PLATFORM_ROLE_OPERATOR)
    first = service.create_unless_live(context, email=INVITEE, org_id=None)
    assert isinstance(first, IssuedInvitation)

    if retired == "revoked":
        service.revoke(context, first.invitation.id)
    elif retired == "expired":
        with live_db.connection() as conn:
            conn.execute(
                "UPDATE collab_invitations SET expires_at = now() - interval '1 second'"
                " WHERE id = %s",
                (first.invitation.id,),
            )
    else:
        with live_db.connection() as conn:
            conn.execute(
                "UPDATE collab_invitations SET status = 'accepted', accepted_at = now(),"
                " accepted_by = 'someone' WHERE id = %s",
                (first.invitation.id,),
            )

    second = service.create_unless_live(context, email=INVITEE, org_id=None)
    assert isinstance(second, IssuedInvitation)
    assert second.invitation.id != first.invitation.id


@live_postgres
def test_live_the_one_live_token_rule_is_scoped_to_this_page(live_db):
    """The scope, pinned so the claim cannot quietly widen.

    ``create_unless_live`` is what the page calls; ``create`` is what #89's
    ``/v1`` routes call and it still mints unconditionally. So an address
    **can** hold two live invitations if one came from the API — the rule is a
    property of a call, not of the deployment. Unifying them would change the
    semantics of a shipped endpoint the desktop is built against; #93 owns
    re-send policy.
    """

    service = _live_service(live_db)
    context = _context(PLATFORM_ROLE_OPERATOR)
    page_issued = service.create_unless_live(context, email=INVITEE, org_id=None)
    assert isinstance(page_issued, IssuedInvitation)

    api_issued = service.create(context, email=INVITEE, org_id=None)
    assert isinstance(api_issued, IssuedInvitation)
    assert api_issued.invitation.id != page_issued.invitation.id

    with live_db.connection() as conn:
        live = conn.execute(
            "SELECT count(*) AS n FROM collab_invitations"
            " WHERE email = %s AND status = 'pending' AND expires_at > now()",
            (INVITEE,),
        ).fetchone()["n"]
    assert live == 2, "the documented scope: the API path is not covered by the rule"

    # And the page still refuses, against whichever live invitation it finds.
    assert isinstance(
        service.create_unless_live(context, email=INVITEE, org_id=None), LiveInvitationExists
    )


@live_postgres
def test_live_a_case_variant_is_the_same_address_for_the_live_check(live_db):
    """Amended on #157: case folds, so these are one address, not two.

    Under the previous rule the second call minted a *second* live invitation
    for what Keycloak considers one account — and only one of them could ever
    be redeemed. The fold at issuance is what makes the advisory lock key, the
    live-invitation query and the stored row all see the same string.
    """

    service = _live_service(live_db)
    context = _context(PLATFORM_ROLE_OPERATOR)
    assert isinstance(
        service.create_unless_live(context, email="Bob@example.com", org_id=None),
        IssuedInvitation,
    )
    assert isinstance(
        service.create_unless_live(context, email="bob@example.com", org_id=None),
        LiveInvitationExists,
    )
    assert isinstance(
        service.create_unless_live(context, email="BOB@EXAMPLE.COM", org_id=None),
        LiveInvitationExists,
    )
    # Still a different address: the fold is case-only.
    assert isinstance(
        service.create_unless_live(context, email="bob+tag@example.com", org_id=None),
        IssuedInvitation,
    )


def test_the_lock_key_is_a_signed_32_bit_integer():
    """``pg_advisory_xact_lock(int, int)`` takes int4s; a value outside the
    range would raise from inside the audited transaction."""

    for address in ("a@b.test", "Bob@example.com", "é@example.com", "x" * 200 + "@y.test"):
        key = invitation_email_lock_key(address)
        assert -(2**31) <= key < 2**31
    assert invitation_email_lock_key("a@b.test") == invitation_email_lock_key("a@b.test")
    assert invitation_email_lock_key("a@b.test") != invitation_email_lock_key("A@b.test")


# ===========================================================================
# The size cap, at the socket — the class of bug ASGI transport cannot see
# ===========================================================================
#
# The first version of this page decided its 4 KiB cap from `Content-Length`
# alone and returned False when the header was absent, which is every chunked
# request; 2,000,000 bytes went through the cap. Nothing in an ASGI-transport
# test can see that — httpx hands the app a complete request, and there is no
# socket, no chunked framing, and no keep-alive. So these run the app under a
# real uvicorn, and they are the negative control the earlier suite lacked.
#
# The helpers are #90's, imported rather than re-implemented: one raw-socket
# probe for two route families, so a future fix to either reaches both.

from test_acceptance_page import (  # noqa: E402
    _LoopbackBrowser,
    _post_body_over_socket,
    _raw_post,
    _requests_over_one_socket,
    running_server,
)


def _operator_app(tmp_path, idp):
    """An app whose live operator holds the role the raw-socket tests need."""

    app, service, delivery = build_app(tmp_path, idp)
    return app, service, delivery


def _sign_in_operator(browser, idp) -> str:
    """Sign in over loopback http and return this session's CSRF token."""

    start = browser.get("/web/signin", params={"next": ADMIN_INVITATIONS_PATH})
    assert start.status_code == 303
    params = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    idp.nonce = params["nonce"]
    callback = browser.get(
        "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
    )
    assert callback.status_code == 303, callback.text
    return csrf_from(browser.get(ADMIN_INVITATIONS_PATH).text)


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_a_chunked_body_cannot_slip_past_the_cap(tmp_path, idp, path):
    """The blocking defect, asserted where it lives.

    A chunked request carries no ``Content-Length``, so the original
    header-only check returned "not too large" and the body reached
    ``request.form()`` — an unbounded read. 64 KiB against a 4 KiB cap.

    Both routes are parametrized because they are two handlers: fixing one and
    not the other is exactly the shape of the original defect, which was itself
    a fix #90 had already made on its own route.
    """

    app, service, _ = _operator_app(tmp_path, idp)
    with running_server(app) as base_url, _LoopbackBrowser(base_url) as browser:
        csrf = _sign_in_operator(browser, idp)
        elapsed, received, closed = _post_body_over_socket(
            base_url,
            path=path,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-Token": csrf,
                "Cookie": browser.cookie_header(),
            },
        )

    # Timing first, for #90's reason: it is the property that regresses when
    # the connection-close header is dropped, and the close alone would read as
    # unnecessary to someone measuring only "did it eventually close".
    assert elapsed < 3, f"the connection was held for {elapsed:.1f}s after the refusal"
    assert closed, "the server kept the connection open after refusing an oversize body"
    assert received.startswith(b"HTTP/1.1 413"), received[:200]
    assert b"connection: close" in received.lower()
    # And nothing was issued or revoked on the way to refusing.
    assert service.issue_calls == [] and service.revoke_calls == []


def test_the_server_keeps_serving_after_refusing_an_oversize_form(tmp_path, idp):
    """The refusal must cost one connection, not the worker.

    Closing is the right answer only if the next request still works; a fix
    that wedged the server would satisfy every assertion above.
    """

    app, _, _ = _operator_app(tmp_path, idp)
    with running_server(app) as base_url, _LoopbackBrowser(base_url) as browser:
        csrf = _sign_in_operator(browser, idp)
        _post_body_over_socket(
            base_url,
            path=ADMIN_INVITATIONS_PATH,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-Token": csrf,
                "Cookie": browser.cookie_header(),
            },
        )
        after = browser.get(ADMIN_INVITATIONS_PATH)
    assert after.status_code == 200
    assert "Invitations" in after.text


def test_a_normal_submission_over_a_real_connection_keeps_it_alive(tmp_path, idp):
    """The other half: a request whose body *was* read must not be closed.

    Without this, "always send Connection: close" would pass every test above
    while throwing away keep-alive for every ordinary submission. So it is
    asserted as reuse: two submissions on **one raw socket**, and the second
    is served on it (#71). The absence of a ``Connection: close`` header is
    not enough — the server could drop the connection for any other reason
    and that control would still pass, which is the regression it is for.
    """

    app, service, _ = _operator_app(tmp_path, idp)
    with running_server(app) as base_url, _LoopbackBrowser(base_url) as browser:
        csrf = _sign_in_operator(browser, idp)
        submissions = [
            _raw_post(
                base_url,
                ADMIN_INVITATIONS_PATH,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": browser.cookie_header(),
                },
                body=urlencode({"csrf_token": csrf, EMAIL_FIELD: address}).encode(),
            )
            for address in (INVITEE, "carol@example.com")
        ]
        served = _requests_over_one_socket(base_url, submissions)
    assert len(served) == 2, "the server closed the connection after a normal submission"
    assert all(response.startswith(b"HTTP/1.1 201") for response in served), [
        response[:200] for response in served
    ]
    assert len(service.issue_calls) == 2
