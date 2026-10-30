"""The owner invitation page (issue #142).

What these prove, and how they relate to the operator page's tests.

**Proven here.** A signed-in non-owner reaches neither the page nor the
actions, and each action refuses independently of the route it was reached
through — including for an owner naming somebody else's organization. Every
invitation the page issues carries the caller's own ``org_id``, resolved from
their membership row and never from the form: the page offers no field that
could name an organization. Issuing hands the secret to the delivery adapter,
renders the sanitized outcome (sent / could not be sent / unconfirmed), and
puts no token on any page — the page has one delivery mode now that
``web/invite_link.py`` is deleted, and a deployment with no provider is warned
before it issues rather than handed a rendered credential. Revocation is
pinned to the caller's
organization inside the transaction (``expect_org_id``), so another
organization's invitation is a plain not-found. And the live tests take an
owner-issued invitation through #90's acceptance page into an **existing**
organization — the flow #142 exists to make real. And the first-invite naming
step (#44): an organization still carrying the placeholder name shows a naming
form instead of the issue form, issues nothing however the POST is shaped,
takes exactly one audited ``org.rename``, and only then issues. The delivery
adapter receives the resolved name as it always did; what the shipped renderer
does with it is ``test_invitation_email.py``'s subject (today: deliberately
nothing — the email is organization-neutral), not this file's.

**Deliberately not re-proven here:** the form-handling properties
(byte-counted caps, no ``request.form()``, ``Connection: close`` on refusal)
beyond one representative case per route — they moved to ``web.forms`` and
the operator page's tests exercise the same code; and the invitation
lifecycle semantics, which are #89's.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

# #88's live stub IdP and sign-in helpers, reused for the same reason #90 and
# #91 reused them: this page has to work on the surface as built.
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
from collab_hub_api.frames.collab_schema import NEUTRAL_ORG_NAME
from collab_hub_api.frames.credentials import InvitationSecret
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.invitation_email import (
    DELIVERY_FAILED,
    DELIVERY_PROVIDER_ACCEPTED,
    DELIVERY_UNKNOWN,
    DeliveryOutcome,
)
from collab_hub_api.frames.invitations import (
    MAX_ORGANIZATION_NAME_LENGTH,
    Invitation,
    InvitationAlreadyUsedError,
    InvitationNotFoundError,
    InvitationPage,
    InvitationsUnavailableError,
    IssuedInvitation,
    LiveInvitationExists,
    OrganizationAlreadyNamedError,
    is_placeholder_organization_name,
    validate_organization_name,
)
from collab_hub_api.frames.org_source import (
    DEFAULT_ORG_ENV,
    DEFAULT_WORKSPACE_ENV,
    ORG_SOURCE_ENV,
)
from collab_hub_api.frames.orgs import ROLE_MEMBER, ROLE_OWNER, InMemoryOrgStore
from collab_hub_api.routers import org_invitations as org_router
from collab_hub_api.web.acceptance import ACCEPT_PAGE_PATH
from collab_hub_api.web.admin import EMAIL_FIELD, INVITATION_ID_FIELD
from collab_hub_api.web.forms import MAX_FORM_BYTES, REQUEST_TOO_LARGE
from collab_hub_api.web.org_invitations import (
    NOTICE_ALREADY_LIVE,
    NOTICE_ALREADY_NAMED,
    NOTICE_INVALID_EMAIL,
    NOTICE_INVALID_ORGANIZATION_NAME,
    NOTICE_ISSUED_SEND_FAILED,
    NOTICE_ISSUED_SEND_UNKNOWN,
    NOTICE_ISSUED_SENT,
    NOTICE_NAMED,
    NOTICE_NOT_FOUND,
    NOTICE_ORGANIZATION_UNNAMED,
    NOTICE_REVOKE_REFUSED,
    NOTICE_REVOKED,
    NOTICE_UNAVAILABLE,
    NOTICES,
    ORGANIZATION_NAME_FIELD,
)
from collab_hub_api.web.surface import (
    ORG_INVITATIONS_NAME_PATH,
    ORG_INVITATIONS_PATH,
    ORG_INVITATIONS_PATHS,
    ORG_INVITATIONS_REVOKE_PATH,
    PUBLIC_WEB_PATHS,
)

SENTINEL_TOKEN = "S3cr3tOwnerTokenThatMustNotLeak"
"""A token satisfying the accept model's alphabet, so a redaction that only
held for values validation rejects would not pass here."""

OWNER_SUB = "subject-alice"
"""The stub IdP's default subject, seeded below as the owner of ORG_ID."""

MEMBER_SUB = "subject-bob"
OUTSIDER_SUB = "subject-nobody"

ORG_ID = "org-1"
ORG_NAME = "Example Organization"
CHOSEN_NAME = "Acme Widgets"
OTHER_ORG_ID = "org-2"

INVITEE = "carol@example.com"

PUBLIC_BASE_URL = "https://web.test"


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
    """Membership mode, which is what makes an org-owner role exist at all.

    Under claims-sourced auth the ``collab_`` tables are never read, so
    ``org_role`` is structurally ``None`` and there are no owners — which is
    why ``make_app`` does not mount this page there. A test asserts that
    directly.
    """

    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)


def notice_text(kind: str, address: str = "") -> str:
    """One notice's copy, escaped the way the page renders it."""

    return html.escape(NOTICES[kind].format(address=address))


def an_invitation(
    *,
    invitation_id: str = "inv-1",
    email: str = INVITEE,
    status: str = "pending",
    org_id: str | None = ORG_ID,
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
        created_by=OWNER_SUB,
        expires_at=now + expires_in,
    )


class FakeInvitationService:
    """Stands in for #89's lifecycle service, recording what it was asked.

    Installed on ``app.state`` because these tests do not run the lifespan.
    The lifecycle semantics are #89's and are tested there; the live-Postgres
    tests at the bottom of this file are what prove the two halves meet.
    """

    def __init__(
        self,
        *,
        rows: list[Invitation] | None = None,
        issue_result=None,
        revoke_raises: Exception | None = None,
        unavailable: bool = False,
        organization_name: str = ORG_NAME,
    ) -> None:
        self.rows = list(rows or [])
        self.name = organization_name
        self.issue_calls: list[dict] = []
        self.revoke_calls: list[dict] = []
        self.name_calls: list[dict] = []
        self.list_calls: list[str] = []
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

    def organization_name(self, org_id: str) -> str:
        self._guard()
        assert org_id == ORG_ID, "the page may only name the caller's own organization"
        return self.name

    def name_organization(self, ctx, *, org_id, name):
        self._guard()
        self.name_calls.append({"actor": ctx.user, "org_id": org_id, "name": name})
        if not is_placeholder_organization_name(self.name):
            raise OrganizationAlreadyNamedError("Organization already has a name")
        self.name = validate_organization_name(name)
        return self.name

    def list_for_org(self, org_id: str, *, limit: int, offset: int = 0) -> InvitationPage:
        self._guard()
        self.list_calls.append(org_id)
        mine = [row for row in self.rows if row.org_id == org_id]
        return InvitationPage(invitations=mine[:limit], has_more=len(mine) > limit)

    def list_all(self, *, limit: int, offset: int = 0):  # pragma: no cover
        raise AssertionError("the owner page must never list the whole deployment")

    def create(self, ctx, *, email, org_id):  # pragma: no cover - must not be called
        self.plain_create_calls.append({"email": email, "org_id": org_id})
        raise AssertionError("the owner page must call create_unless_live, not create")

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
        self.revoke_calls.append({"actor": ctx.user, "invitation_id": invitation_id, "expect_org_id": expect_org_id})
        if self.revoke_raises is not None:
            raise self.revoke_raises
        for index, row in enumerate(self.rows):
            if row.id == invitation_id and row.org_id == expect_org_id:
                revoked = Invitation(**{**row.__dict__, "status": "revoked"})
                self.rows[index] = revoked
                return revoked
        raise InvitationNotFoundError("Invitation not found")


class UnconfiguredDelivery:
    """The adapter shape of a deployment with no provider.

    It is still *called*: with the link display deleted there is no second
    route for the secret, so a deployment that forgot to configure mail gets a
    visible failed send rather than a rendered credential. ``configured`` only
    decides whether the page warns before the owner issues.
    """

    configured = False

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def deliver(self, *, invitation_id, recipient, invitation_secret, organization_name, expires_at):
        self.calls.append({"invitation_id": invitation_id, "recipient": recipient})
        return DeliveryOutcome(status=DELIVERY_FAILED, error_code="no_provider")


class EmailModeDelivery:
    """A configured provider seam, recording what it was handed."""

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
            }
        )
        return self.outcome


class _NoAttrDelivery:
    """An adapter that never says whether it is configured.

    The page must read that silence as "configured": a mistaken send attempt
    fails visibly, a mistakenly rendered credential does not.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def deliver(self, *, invitation_id, recipient, invitation_secret, organization_name, expires_at):
        self.calls.append({"invitation_id": invitation_id})
        return DeliveryOutcome(status=DELIVERY_PROVIDER_ACCEPTED)


def build_app(
    tmp_path,
    idp,
    *,
    delivery=None,
    memberships: dict[str, tuple[str, str]] | None = None,
    web: dict | None = None,
    **service_kwargs,
):
    """An app with the page mounted, a stub service, and a chosen delivery seam.

    ``memberships`` maps subject → (org_id, role); the default seeds the
    owner, a plain member of the same organization, and an owner of a
    *different* organization, which is the caller the org pinning exists to
    refuse.
    """

    app = make_web_app(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL, **(web or {})})
    service = FakeInvitationService(**service_kwargs)
    delivery = delivery if delivery is not None else EmailModeDelivery()
    app.state.invitation_service = service
    app.state.invitation_email_delivery = delivery
    org_store = InMemoryOrgStore()
    for subject, (org_id, role) in (
        memberships
        if memberships is not None
        else {
            OWNER_SUB: (ORG_ID, ROLE_OWNER),
            MEMBER_SUB: (ORG_ID, ROLE_MEMBER),
            "subject-other-owner": (OTHER_ORG_ID, ROLE_OWNER),
        }
    ).items():
        org_store.set_membership(subject, org_id, role=role)
    app.state.org_store = org_store
    return app, service, delivery


async def signed_in(client: AsyncClient, idp: _StubIdp):
    response = await sign_in(client, idp, next_path=ORG_INVITATIONS_PATH)
    assert response.status_code == 303
    return response


def csrf_from(document: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', document)
    assert match, "every form on this surface carries the CSRF token"
    return match.group(1)


async def issue(client: AsyncClient, *, email: str = INVITEE, csrf: str | None = None):
    page = await client.get(ORG_INVITATIONS_PATH)
    return await client.post(
        ORG_INVITATIONS_PATH,
        data={
            "csrf_token": csrf if csrf is not None else csrf_from(page.text),
            EMAIL_FIELD: email,
        },
    )


async def name_org(client: AsyncClient, *, name: str = CHOSEN_NAME, csrf: str | None = None):
    page = await client.get(ORG_INVITATIONS_PATH)
    return await client.post(
        ORG_INVITATIONS_NAME_PATH,
        data={
            "csrf_token": csrf if csrf is not None else csrf_from(page.text),
            ORGANIZATION_NAME_FIELD: name,
        },
    )


async def revoke(client: AsyncClient, invitation_id: str, *, csrf: str | None = None):
    page = await client.get(ORG_INVITATIONS_PATH)
    return await client.post(
        ORG_INVITATIONS_REVOKE_PATH,
        data={
            "csrf_token": csrf if csrf is not None else csrf_from(page.text),
            INVITATION_ID_FIELD: invitation_id,
        },
    )


# ===========================================================================
# Owners only — the page and the actions, checked separately
# ===========================================================================


@pytest.mark.parametrize("path", ORG_INVITATIONS_PATHS)
def test_no_org_path_is_ever_anonymous(path):
    assert path not in PUBLIC_WEB_PATHS


@pytest.mark.parametrize("path", ORG_INVITATIONS_PATHS)
async def test_an_anonymous_browser_is_sent_to_sign_in(tmp_path, idp, path):
    app, _service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/signin?")


async def test_a_signed_in_member_is_refused_with_a_page_not_a_shrug(tmp_path, idp):
    """Ownership is the gate, and a member without it gets a clear refusal."""

    app, service, _delivery = build_app(tmp_path, idp)
    idp.sub = MEMBER_SUB
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        assert page.status_code == 403
        assert "does not hold the role" in page.text
        posted = await client.post(ORG_INVITATIONS_PATH, data={"csrf_token": "irrelevant", EMAIL_FIELD: INVITEE})
        assert posted.status_code == 403
    assert service.issue_calls == []


async def test_a_login_with_no_membership_is_refused_the_same_way(tmp_path, idp):
    app, service, _delivery = build_app(tmp_path, idp)
    idp.sub = OUTSIDER_SUB
    async with web_client(app) as client:
        await signed_in(client, idp)
        assert (await client.get(ORG_INVITATIONS_PATH)).status_code == 403
    assert service.issue_calls == []


async def test_a_removed_owner_is_refused_on_their_next_request(tmp_path, idp):
    """The stateless cookie grants identity only; ownership is read live."""

    app, _service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        assert (await client.get(ORG_INVITATIONS_PATH)).status_code == 200
        app.state.org_store.set_membership(OWNER_SUB, ORG_ID, role=ROLE_OWNER, status="removed")
        assert (await client.get(ORG_INVITATIONS_PATH)).status_code == 403


def _context(role: str | None, org_id: str | None = ORG_ID) -> AuthContext:
    return AuthContext(
        user="someone",
        home_org_id=org_id,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(name="Someone", email="someone@example.com"),
        org_role=role,
        platform_role=None,
    )


@pytest.mark.parametrize("role", [None, ROLE_MEMBER])
def test_the_actions_refuse_without_the_owner_org_role(role):
    from fastapi import HTTPException

    service = FakeInvitationService()
    for action in (
        lambda: org_router.issue_invitation(_context(role), service, org_id=ORG_ID, email=INVITEE),
        lambda: org_router.revoke_invitation(_context(role), service, org_id=ORG_ID, invitation_id="inv-1"),
        lambda: org_router.name_organization(_context(role), service, org_id=ORG_ID, name=CHOSEN_NAME),
    ):
        with pytest.raises(HTTPException) as refused:
            action()
        assert refused.value.status_code == 403
    assert service.issue_calls == []
    assert service.revoke_calls == []
    assert service.name_calls == []


def test_the_actions_refuse_an_owner_of_a_different_organization():
    """The org pinning: an owner of org B is nobody in org A."""

    from fastapi import HTTPException

    service = FakeInvitationService()
    other = _context(ROLE_OWNER, org_id=OTHER_ORG_ID)
    for action in (
        lambda: org_router.issue_invitation(other, service, org_id=ORG_ID, email=INVITEE),
        lambda: org_router.revoke_invitation(other, service, org_id=ORG_ID, invitation_id="inv-1"),
        lambda: org_router.name_organization(other, service, org_id=ORG_ID, name=CHOSEN_NAME),
    ):
        with pytest.raises(HTTPException) as refused:
            action()
        assert refused.value.status_code == 403
    assert service.issue_calls == []
    assert service.name_calls == []


def test_a_platform_operator_role_never_stands_in_for_ownership():
    from fastapi import HTTPException

    service = FakeInvitationService()
    operator = AuthContext(
        user="op",
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        org_role=None,
        platform_role="operator",
    )
    with pytest.raises(HTTPException):
        org_router.issue_invitation(operator, service, org_id=ORG_ID, email=INVITEE)
    assert service.issue_calls == []


async def test_the_page_is_absent_where_owners_cannot_exist(tmp_path, idp, monkeypatch):
    """Claims-sourced deployments mount neither the page nor its POSTs."""

    monkeypatch.delenv(ORG_SOURCE_ENV, raising=False)
    monkeypatch.setenv(DEFAULT_ORG_ENV, "claims-org")
    values = web_values(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})
    app = make_app(Config.parse(values))
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert ORG_INVITATIONS_PATH not in paths
    assert ORG_INVITATIONS_REVOKE_PATH not in paths


# ===========================================================================
# Issuing — always the caller's organization, never a form's
# ===========================================================================


async def test_issuing_targets_the_callers_own_organization(tmp_path, idp):
    app, service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert service.issue_calls == [{"actor": OWNER_SUB, "email": INVITEE, "org_id": ORG_ID}]
    assert service.plain_create_calls == []
    assert notice_text(NOTICE_ISSUED_SENT, INVITEE) in response.text


async def test_the_page_offers_no_way_to_name_an_organization(tmp_path, idp):
    """No org input exists, and a smuggled org field changes nothing."""

    app, service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        assert page.status_code == 200
        fields = re.findall(r'<input[^>]*name="([^"]+)"', page.text)
        assert set(fields) == {"csrf_token", EMAIL_FIELD}
        smuggled = await client.post(
            ORG_INVITATIONS_PATH,
            data={
                "csrf_token": csrf_from(page.text),
                EMAIL_FIELD: INVITEE,
                "org_id": OTHER_ORG_ID,
            },
        )
    assert smuggled.status_code == 201
    assert service.issue_calls[-1]["org_id"] == ORG_ID


async def test_the_listing_is_the_organizations_not_the_deployments(tmp_path, idp):
    foreign = an_invitation(invitation_id="inv-foreign", email="eve@example.com", org_id=OTHER_ORG_ID)
    hub = an_invitation(invitation_id="inv-hub", email="hub@example.com", org_id=None)
    mine = an_invitation(invitation_id="inv-mine")
    app, service, _delivery = build_app(tmp_path, idp, rows=[foreign, hub, mine])
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert page.status_code == 200
    assert service.list_calls == [ORG_ID]
    assert INVITEE in page.text
    assert "eve@example.com" not in page.text
    assert "hub@example.com" not in page.text


@pytest.mark.parametrize("address", ["", "not-an-address", "two@a.com three@b.com"])
async def test_an_unusable_address_creates_nothing(tmp_path, idp, address):
    app, service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client, email=address)
    assert response.status_code == 400
    assert notice_text(NOTICE_INVALID_EMAIL) in response.text
    assert service.issue_calls == []


async def test_issuing_twice_for_one_address_mints_no_second_token(tmp_path, idp):
    existing = an_invitation()
    app, service, _delivery = build_app(tmp_path, idp, issue_result=LiveInvitationExists(existing=existing))
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 409
    assert notice_text(NOTICE_ALREADY_LIVE, INVITEE) in response.text
    assert "<code>" not in response.text
    assert "<textarea" not in response.text


async def test_an_unavailable_service_creates_nothing_and_says_so(tmp_path, idp):
    app, _service, _delivery = build_app(tmp_path, idp, unavailable=True)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        assert page.status_code == 503
        assert notice_text(NOTICE_UNAVAILABLE) in page.text


# ===========================================================================
# Delivery — one route for the secret
# ===========================================================================


async def test_issuing_sends_and_renders_no_token_anywhere(tmp_path, idp):
    app, _service, delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert notice_text(NOTICE_ISSUED_SENT, INVITEE) in response.text
    assert SENTINEL_TOKEN not in response.text
    assert "<code>" not in response.text
    assert "<textarea" not in response.text
    assert len(delivery.calls) == 1
    assert delivery.calls[0]["recipient"] == INVITEE
    assert delivery.calls[0]["invitation_secret"] == SENTINEL_TOKEN
    assert delivery.calls[0]["organization_name"] == ORG_NAME


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (DELIVERY_FAILED, NOTICE_ISSUED_SEND_FAILED),
        (DELIVERY_UNKNOWN, NOTICE_ISSUED_SEND_UNKNOWN),
    ],
)
async def test_a_failed_or_unconfirmed_send_is_worded_not_hidden(tmp_path, idp, status, kind):
    app, _service, _delivery = build_app(tmp_path, idp, delivery=EmailModeDelivery(status=status, error_code="boom"))
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert notice_text(kind, INVITEE) in response.text
    assert "boom" not in response.text, "provider error codes are for the API, not the page"
    assert SENTINEL_TOKEN not in response.text


async def test_an_adapter_that_does_not_declare_itself_is_still_sent_through(tmp_path, idp):
    """Silence means send: a mistaken send fails visibly, and with the link
    display gone there is no second route a mistake could take the secret."""

    delivery = _NoAttrDelivery()
    app, _service, _delivery = build_app(tmp_path, idp, delivery=delivery)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 201
    assert len(delivery.calls) == 1
    assert SENTINEL_TOKEN not in response.text


async def test_a_deployment_with_no_provider_is_warned_before_it_issues(tmp_path, idp):
    """What replaced the link fallback, and the honest version of it.

    There is nothing useful the page can do with an unconfigured adapter, so it
    says so *above the form* — before an owner spends an invitation on it — and
    an issue that happens anyway reports the failed send rather than falling
    back to a rendered credential.
    """

    delivery = UnconfiguredDelivery()
    app, _service, _delivery = build_app(tmp_path, idp, delivery=delivery)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        assert "no invitation email configured" in page.text
        response = await issue(client)
    assert response.status_code == 201
    assert notice_text(NOTICE_ISSUED_SEND_FAILED, INVITEE) in response.text
    assert SENTINEL_TOKEN not in response.text
    assert "<code>" not in response.text
    assert len(delivery.calls) == 1


async def test_a_configured_deployment_carries_no_warning(tmp_path, idp):
    app, _service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert "no invitation email configured" not in page.text


# ===========================================================================
# Revoking — pinned to the caller's organization
# ===========================================================================


async def test_revoking_names_the_invitation_and_carries_the_org_pin(tmp_path, idp):
    app, service, _delivery = build_app(tmp_path, idp, rows=[an_invitation()])
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await revoke(client, "inv-1")
    assert response.status_code == 200
    assert notice_text(NOTICE_REVOKED, INVITEE) in response.text
    assert service.revoke_calls == [{"actor": OWNER_SUB, "invitation_id": "inv-1", "expect_org_id": ORG_ID}]


async def test_another_organizations_invitation_is_a_plain_not_found(tmp_path, idp):
    foreign = an_invitation(invitation_id="inv-foreign", org_id=OTHER_ORG_ID)
    app, service, _delivery = build_app(tmp_path, idp, rows=[foreign])
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await revoke(client, "inv-foreign")
    assert response.status_code == 404
    assert notice_text(NOTICE_NOT_FOUND) in response.text
    assert service.revoke_calls[-1]["expect_org_id"] == ORG_ID


@pytest.mark.parametrize(
    ("raises", "notice", "code"),
    [
        (InvitationNotFoundError("gone"), NOTICE_NOT_FOUND, 404),
        (InvitationAlreadyUsedError("used"), NOTICE_REVOKE_REFUSED, 409),
        (InvitationsUnavailableError("down"), NOTICE_UNAVAILABLE, 503),
    ],
)
async def test_a_revoke_that_cannot_happen_renders_its_own_sentence(tmp_path, idp, raises, notice, code):
    app, _service, _delivery = build_app(tmp_path, idp, rows=[an_invitation()], revoke_raises=raises)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await revoke(client, "inv-1")
    assert response.status_code == code
    assert notice_text(notice) in response.text


async def test_only_revocable_rows_get_a_revoke_control(tmp_path, idp):
    rows = [
        an_invitation(invitation_id="inv-pending", email="p@example.com"),
        an_invitation(invitation_id="inv-done", email="d@example.com", status="accepted"),
        an_invitation(invitation_id="inv-gone", email="g@example.com", status="revoked"),
        an_invitation(invitation_id="inv-old", email="o@example.com", expires_in=timedelta(days=-1)),
    ]
    app, _service, _delivery = build_app(tmp_path, idp, rows=rows)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert page.status_code == 200
    revocable = re.findall(rf'name="{INVITATION_ID_FIELD}" value="([^"]+)"', page.text)
    assert sorted(revocable) == ["inv-old", "inv-pending"]


# ===========================================================================
# CSRF and request bounds — one representative case per property
# ===========================================================================


@pytest.mark.parametrize("csrf", ["", "wrong-token"])
async def test_a_post_without_the_session_csrf_token_changes_nothing(tmp_path, idp, csrf):
    app, service, _delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        named = await name_org(client, csrf=csrf)
        assert named.status_code == 403
        response = await issue(client, csrf=csrf)
    assert response.status_code == 403
    assert service.issue_calls == []
    assert service.name_calls == []


@pytest.mark.parametrize("path", ORG_INVITATIONS_PATHS)
async def test_an_oversized_form_is_refused_before_it_is_parsed(tmp_path, idp, path):
    app, service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        response = await client.post(
            path,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: "a" * MAX_FORM_BYTES},
        )
    assert response.status_code == REQUEST_TOO_LARGE
    assert response.headers.get("connection") == "close"
    assert service.issue_calls == []
    assert service.revoke_calls == []
    assert service.name_calls == []


# ===========================================================================
# Copy, escaping, and the overview
# ===========================================================================


def test_every_notice_the_router_can_raise_has_copy():
    assert set(NOTICES) == {
        NOTICE_ISSUED_SENT,
        NOTICE_ISSUED_SEND_FAILED,
        NOTICE_ISSUED_SEND_UNKNOWN,
        NOTICE_ALREADY_LIVE,
        NOTICE_INVALID_EMAIL,
        NOTICE_NOT_FOUND,
        NOTICE_REVOKED,
        NOTICE_REVOKE_REFUSED,
        NOTICE_UNAVAILABLE,
        NOTICE_ORGANIZATION_UNNAMED,
        NOTICE_NAMED,
        NOTICE_ALREADY_NAMED,
        NOTICE_INVALID_ORGANIZATION_NAME,
    }


async def test_a_hostile_address_cannot_inject_markup(tmp_path, idp):
    hostile = an_invitation(email='x"><script>alert(1)</script>@example.com')
    app, _service, _delivery = build_app(tmp_path, idp, rows=[hostile])
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert "<script>alert(1)</script>" not in page.text


async def test_the_organization_name_is_on_the_page_and_escaped(tmp_path, idp):
    app, _service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert html.escape(ORG_NAME) in page.text


async def test_the_overview_links_to_the_page(tmp_path, idp):
    app, _service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        overview = await client.get("/web")
    assert ORG_INVITATIONS_PATH in overview.text


async def test_the_page_carries_no_store_no_referrer_and_the_no_script_policy(tmp_path, idp):
    from collab_hub_api.web.pages import CONTENT_SECURITY_POLICY

    app, _service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_the_rendered_secret_reaches_no_log_record(tmp_path, idp, caplog):
    import logging

    app, _service, _delivery = build_app(tmp_path, idp)
    with caplog.at_level(logging.DEBUG):
        async with web_client(app) as client:
            await signed_in(client, idp)
            response = await issue(client)
    assert response.status_code == 201
    for record in caplog.records:
        assert SENTINEL_TOKEN not in record.getMessage()
        assert SENTINEL_TOKEN not in str(record.__dict__)
        assert INVITEE not in record.getMessage()


# ===========================================================================
# The first-invite naming step (#44) — no invitation into "Unnamed organization"
# ===========================================================================


async def test_an_unnamed_organization_gets_the_naming_form_not_the_issue_form(tmp_path, idp):
    app, _service, _delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
    assert page.status_code == 200
    fields = re.findall(r'<input[^>]*name="([^"]+)"', page.text)
    assert set(fields) == {"csrf_token", ORGANIZATION_NAME_FIELD}
    assert ORG_INVITATIONS_NAME_PATH in page.text
    assert "Invite someone to join" not in page.text
    assert "does not have a name yet" in page.text


@pytest.mark.parametrize("stored", [NEUTRAL_ORG_NAME, "unnamed organization", "", "   "])
async def test_issuing_into_an_unnamed_organization_is_refused_by_the_server(tmp_path, idp, stored):
    """The hidden form is the rendering; this is the refusal. A hand-built POST
    changes nothing and reaches neither the service nor the mail seam."""

    app, service, delivery = build_app(tmp_path, idp, organization_name=stored)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await issue(client)
    assert response.status_code == 409
    assert notice_text(NOTICE_ORGANIZATION_UNNAMED, INVITEE) in response.text
    assert service.issue_calls == []
    assert delivery.calls == []


async def test_naming_records_the_owner_and_the_callers_organization(tmp_path, idp):
    app, service, _delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await name_org(client, name=f"  {CHOSEN_NAME}  ")
    assert response.status_code == 200
    assert service.name_calls == [{"actor": OWNER_SUB, "org_id": ORG_ID, "name": CHOSEN_NAME}]
    assert notice_text(NOTICE_NAMED, CHOSEN_NAME) in response.text
    # The page rendered after naming is #142's: the issue form, and the name.
    fields = re.findall(r'<input[^>]*name="([^"]+)"', response.text)
    assert set(fields) == {"csrf_token", EMAIL_FIELD}
    assert f"Invite someone to join {CHOSEN_NAME}" in response.text


async def test_naming_then_issuing_hands_the_new_name_to_the_mail_seam(tmp_path, idp):
    app, service, delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        assert (await name_org(client)).status_code == 200
        issued = await issue(client)
    assert issued.status_code == 201
    assert service.issue_calls == [{"actor": OWNER_SUB, "email": INVITEE, "org_id": ORG_ID}]
    assert [call["organization_name"] for call in delivery.calls] == [CHOSEN_NAME]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        NEUTRAL_ORG_NAME,
        "UNNAMED ORGANIZATION",
        "  unnamed organization ",
        "a" * (MAX_ORGANIZATION_NAME_LENGTH + 1),
        "Acme\nWidgets",
        "Acme\x00",
        "Acme\x7f",
    ],
)
async def test_an_unusable_name_changes_nothing(tmp_path, idp, bad):
    app, service, _delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await name_org(client, name=bad)
    assert response.status_code == 400
    assert notice_text(NOTICE_INVALID_ORGANIZATION_NAME) in response.text
    assert service.name_calls == []
    assert service.name == NEUTRAL_ORG_NAME


async def test_a_smuggled_org_id_does_not_redirect_the_naming(tmp_path, idp):
    app, service, _delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        response = await client.post(
            ORG_INVITATIONS_NAME_PATH,
            data={
                "csrf_token": csrf_from(page.text),
                ORGANIZATION_NAME_FIELD: CHOSEN_NAME,
                "org_id": OTHER_ORG_ID,
            },
        )
    assert response.status_code == 200
    assert service.name_calls[-1]["org_id"] == ORG_ID


async def test_a_named_organization_offers_no_naming_form_and_refuses_the_post(tmp_path, idp):
    """One shot: once named, the form is gone and the route says so."""

    app, service, _delivery = build_app(tmp_path, idp)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        assert ORG_INVITATIONS_NAME_PATH not in page.text
        assert ORGANIZATION_NAME_FIELD not in page.text
        response = await name_org(client, name="Something Else")
    assert response.status_code == 409
    assert notice_text(NOTICE_ALREADY_NAMED) in response.text
    assert service.name == ORG_NAME
    assert html.escape(ORG_NAME) in response.text


async def test_a_hostile_name_cannot_inject_markup(tmp_path, idp):
    hostile = 'Acme"><script>alert(1)</script>'
    app, _service, _delivery = build_app(tmp_path, idp, organization_name=NEUTRAL_ORG_NAME)
    async with web_client(app) as client:
        await signed_in(client, idp)
        response = await name_org(client, name=hostile)
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert html.escape(hostile) in response.text


async def test_naming_on_an_unavailable_service_changes_nothing_and_says_so(tmp_path, idp):
    app, service, _delivery = build_app(tmp_path, idp, unavailable=True)
    async with web_client(app) as client:
        await signed_in(client, idp)
        page = await client.get(ORG_INVITATIONS_PATH)
        response = await client.post(
            ORG_INVITATIONS_NAME_PATH,
            data={"csrf_token": csrf_from(page.text), ORGANIZATION_NAME_FIELD: CHOSEN_NAME},
        )
    assert response.status_code == 503
    assert notice_text(NOTICE_UNAVAILABLE) in response.text
    assert service.name_calls == []


def test_the_naming_route_is_in_the_pages_path_registry():
    """So the CSRF-in-route registry and the oversized-form test both cover it."""

    assert ORG_INVITATIONS_NAME_PATH in ORG_INVITATIONS_PATHS
    assert ORG_INVITATIONS_NAME_PATH not in PUBLIC_WEB_PATHS


# ===========================================================================
# Live Postgres — the org-bound acceptance flow, end to end
# ===========================================================================

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live owner tests",
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


class CapturingDelivery(EmailModeDelivery):
    """The real deployment's mail seam, replaced by one the test can read.

    With the link display deleted, the secret leaves the page only through the
    delivery adapter — so a live test that has to redeem an invitation takes it
    from here. That the *real* adapter renders and sends the same secret is
    ``test_invitation_email.py``'s subject, not this file's.
    """

    def secret(self) -> str:
        assert len(self.calls) == 1, "exactly one send per issued invitation"
        return self.calls[0]["invitation_secret"]


@pytest_asyncio.fixture
async def live_app(tmp_path, idp, live_db):
    values = web_values(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})
    values["frames"]["postgres"] = {"url": POSTGRES_URL, "auto_migrate": True}
    values["frames"]["orgs"] = {"backend": "postgres"}
    app = make_app(Config.parse(values))
    async with app.router.lifespan_context(app):
        app.state.invitation_email_delivery = CapturingDelivery()
        with live_db.connection() as conn:
            # Seeded the way acceptance really creates one: no name supplied,
            # so the column default — the placeholder — applies (#44).
            conn.execute(
                "INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)",
                (ORG_ID, OWNER_SUB),
            )
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, 'owner')",
                (OWNER_SUB, ORG_ID),
            )
        yield app


@live_postgres
async def test_live_an_owner_takes_an_address_into_their_existing_org(live_app, idp, live_db):
    """#142's acceptance criterion: the org-bound flow, runtime-tested.

    Owner signs in and issues from the page; the invitation secret leaves
    through the mail seam (read here from a capturing adapter) and the invitee
    redeems it through #90's acceptance page — landing as a **member of the
    owner's existing organization**, with no second organization created. The
    audit trail attributes the send to the owner and the join to the invitee.
    """

    from test_acceptance_page import redeem as redeem_through_page  # noqa: E402

    async with web_client(live_app) as owner:
        await sign_in(owner, idp, next_path=ORG_INVITATIONS_PATH)
        page = await owner.get(ORG_INVITATIONS_PATH)
        assert page.status_code == 200
        # #44: the organization was created by acceptance and carries the
        # placeholder, so the page refuses to issue until it is named.
        refused = await owner.post(
            ORG_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
        assert refused.status_code == 409
        assert live_app.state.invitation_email_delivery.calls == []
        named = await owner.post(
            ORG_INVITATIONS_NAME_PATH,
            data={"csrf_token": csrf_from(page.text), ORGANIZATION_NAME_FIELD: CHOSEN_NAME},
        )
        assert named.status_code == 200
        issued = await owner.post(
            ORG_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(named.text), EMAIL_FIELD: INVITEE},
        )
    assert issued.status_code == 201
    assert live_app.state.invitation_email_delivery.calls[0]["organization_name"] == CHOSEN_NAME
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
        memberships = conn.execute(
            "SELECT user_id, org_id, role FROM collab_org_members ORDER BY created_at"
        ).fetchall()
        orgs = conn.execute("SELECT id FROM collab_orgs").fetchall()
        events = conn.execute("SELECT action, actor, org_id FROM collab_audit_events ORDER BY id").fetchall()
        leaked = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE detail::text LIKE %s",
            (f"%{token}%",),
        ).fetchone()["n"]

    assert [row["id"] for row in orgs] == [ORG_ID], "no second organization may exist"
    with live_db.connection() as conn:
        org_name = conn.execute("SELECT name FROM collab_orgs WHERE id = %s", (ORG_ID,)).fetchone()["name"]
        rename = conn.execute(
            "SELECT actor, target_type, target_id, target_label, org_id, detail"
            " FROM collab_audit_events WHERE action = 'org.rename'"
        ).fetchall()
    assert org_name == CHOSEN_NAME
    assert [dict(row) for row in rename] == [
        {
            "actor": OWNER_SUB,
            "target_type": "org",
            "target_id": ORG_ID,
            "target_label": CHOSEN_NAME,
            "org_id": ORG_ID,
            "detail": {"replaced_placeholder": True},
        }
    ]
    assert [row["action"] for row in events][:2] == ["org.rename", "invitation.send"]
    assert {(row["user_id"], row["org_id"], row["role"]) for row in memberships} == {
        (OWNER_SUB, ORG_ID, "owner"),
        (idp.sub, ORG_ID, "member"),
    }
    send_events = [row for row in events if row["action"] == "invitation.send"]
    assert [(row["actor"], row["org_id"]) for row in send_events] == [(OWNER_SUB, ORG_ID)]
    assert "org.create" not in {row["action"] for row in events}
    assert leaked == 0, "no audit row may quote the raw secret"


@live_postgres
async def test_live_an_owner_revokes_and_the_link_dies(live_app, idp, live_db):
    with live_db.connection() as conn:
        conn.execute("UPDATE collab_orgs SET name = %s WHERE id = %s", (ORG_NAME, ORG_ID))
    async with web_client(live_app) as owner:
        await sign_in(owner, idp, next_path=ORG_INVITATIONS_PATH)
        page = await owner.get(ORG_INVITATIONS_PATH)
        issued = await owner.post(
            ORG_INVITATIONS_PATH,
            data={"csrf_token": csrf_from(page.text), EMAIL_FIELD: INVITEE},
        )
        assert issued.status_code == 201
        with live_db.connection() as conn:
            invitation = conn.execute("SELECT id, org_id FROM collab_invitations").fetchone()
        assert invitation["org_id"] == ORG_ID
        listing = await owner.get(ORG_INVITATIONS_PATH)
        revoked = await owner.post(
            ORG_INVITATIONS_REVOKE_PATH,
            data={
                "csrf_token": csrf_from(listing.text),
                INVITATION_ID_FIELD: invitation["id"],
            },
        )
    assert revoked.status_code == 200

    with live_db.connection() as conn:
        status_row = conn.execute("SELECT status FROM collab_invitations").fetchone()
        actions = [
            row["action"] for row in conn.execute("SELECT action FROM collab_audit_events ORDER BY id").fetchall()
        ]
    assert status_row["status"] == "revoked"
    assert actions == ["invitation.send", "invitation.revoke"]
