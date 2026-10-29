"""The admin JSON API, on the browser axis.

The panel is a browser application, so its API is authenticated the way the
rest of the browser surface is -- the session cookie, with the CSRF header on
mutations -- and not the way ``/v1`` is. That choice buys the path-based guard,
which authenticates before routing and cannot be defeated by a route that
forgets a dependency.

The one thing the guard does not do out of the box is answer a JSON client
correctly: its refusal for a page is a redirect to sign-in, which ``fetch``
follows, handing the caller an HTML document with status 200. These tests pin
the JSON shape of every refusal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("jwt")

from test_web_surface import (  # noqa: E402
    _StubIdp,
    make_web_app,
    sign_in,
    web_client,
)

from collab_hub_api.config import ConnectorsConfig  # noqa: E402
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV  # noqa: E402
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV  # noqa: E402
from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR  # noqa: E402


@pytest.fixture(autouse=True)
def membership_env(monkeypatch):
    """Membership mode: the only mode in which an operator role exists at all.

    Under claims-sourced auth the ``collab_`` tables are never read, so the
    platform-role axis is structurally ``None`` -- which is why ``make_app``
    mounts neither the operator page nor the admin API there.

    Identity is pinned to ``sub`` because roles are keyed on the principal the
    session carries, and the sync writes under that same principal. That is the
    configuration the runbook documents.
    """

    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")

ADMIN_SESSION_PATH = "/admin/api/session"
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


def build_app(tmp_path, idp):
    return make_web_app(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})


def grant_operator(app, user: str) -> None:
    """Seed the canonical source, not the test seam.

    ``resolve_platform_role`` reads ``OrgStore.resolve_principal`` whenever a
    store exists, and one does here because these tests run the lifespan. A
    ``platform_role_resolver`` set on app state would be ignored, which is
    correct -- the table outranks it -- and would make this test assert against
    a source the request path never consults.
    """

    app.state.org_store.set_platform_role(user)


@pytest.mark.asyncio
async def test_an_unauthenticated_json_call_is_refused_as_json(tmp_path, idp: _StubIdp):
    """A redirect here would reach `fetch` as a 200 and an HTML sign-in page."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        response = await client.get(ADMIN_SESSION_PATH)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]


@pytest.mark.asyncio
async def test_a_signed_in_non_operator_is_refused_as_json(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        await sign_in(client, idp, next_path="/web")
        response = await client.get(ADMIN_SESSION_PATH)

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_the_session_lists_the_feature_flags_that_are_on(tmp_path, idp: _StubIdp, monkeypatch):
    """The panel gates unfinished screens on this list; it has no other view of server settings."""

    from collab_hub_api import config as config_module
    from collab_hub_api.config import FeaturesConfig

    monkeypatch.setitem(config_module.FEATURE_FLAGS, "cogs_ui", "The Cogs screens (test flag).")
    monkeypatch.setitem(config_module.FEATURE_FLAGS, "other_ui", "Another screen (test flag).")
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        await sign_in(client, idp, next_path="/web")
        assert (await client.get(ADMIN_SESSION_PATH)).json()["features"] == []
        app.state.features = FeaturesConfig.model_validate({"cogs_ui": True, "other_ui": False})
        assert (await client.get(ADMIN_SESSION_PATH)).json()["features"] == ["cogs_ui"]


@pytest.mark.asyncio
async def test_an_operator_gets_their_identity_role_and_csrf_token(tmp_path, idp: _StubIdp):
    """The panel cannot read the CSRF secret itself: the cookie is HttpOnly."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        await sign_in(client, idp, next_path="/web")
        response = await client.get(ADMIN_SESSION_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["user"] == idp.sub
    assert body["email"] == "alice@example.com"
    assert body["role"] == PLATFORM_ROLE_OPERATOR
    assert body["csrf_token"]


@pytest.mark.asyncio
async def test_the_audit_endpoint_is_operator_only(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/audit")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_audit_endpoint_reports_an_unavailable_log_clearly(tmp_path, idp: _StubIdp):
    """A memory deployment records nothing; saying so beats an empty page."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/audit")

    assert response.status_code == 503
    assert response.json()["error"] == "audit_log_unavailable"


@pytest.mark.asyncio
async def test_hub_usage_is_operator_only_and_spans_organizations(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        await sign_in(client, idp, next_path="/web")
        refused = await client.get("/admin/api/usage")

        grant_operator(app, idp.sub)
        app.state.usage_store.record_user_seen("org-a", "default", "u-1", "alice@example.com")
        app.state.usage_store.record_user_seen("org-b", "default", "u-2", "bob@example.com")
        app.state.usage_store.record_event("org-a", "default", "u-1", "chat")
        allowed = await client.get("/admin/api/usage")

    assert refused.status_code == 403
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["users_total"] == 2
    assert body["events"] == [{"event": "chat", "count": 1}]
    assert {org["org_id"] for org in body["organizations"]} == {"org-a", "org-b"}


@pytest.mark.asyncio
async def test_a_usage_window_without_a_timezone_is_refused_not_a_500(tmp_path, idp: _StubIdp):
    """A naive time cannot be compared with stored aware times in memory, and
    Postgres would read it in its own session timezone. Neither is an answer."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.usage_store.record_event("org-a", "default", "u-1", "chat")
        await sign_in(client, idp, next_path="/web")
        naive = await client.get("/admin/api/usage?since=2026-09-01T00:00:00")
        aware = await client.get("/admin/api/usage?since=2026-09-01T00:00:00Z")

    assert naive.status_code == 422
    assert aware.status_code == 200


@pytest.mark.asyncio
async def test_an_organization_with_events_but_no_roster_row_is_still_listed(tmp_path, idp: _StubIdp):
    """Otherwise its events count in the total but appear under no organization."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.usage_store.record_event("org-c", "default", "u-9", "chat")
        await sign_in(client, idp, next_path="/web")
        body = (await client.get("/admin/api/usage")).json()

    assert body["events_total"] == 1
    assert {org["org_id"]: org["events"] for org in body["organizations"]} == {"org-c": 1}


@pytest.mark.asyncio
async def test_connectors_are_listed_without_any_secret_reaching_the_browser(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    secret = "slack-static-token-value"
    app.state.connectors_config = ConnectorsConfig.model_validate(
        {"slack": {"static_access_token": secret}}
    )

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        await sign_in(client, idp, next_path="/web")
        app.state.connectors_config = ConnectorsConfig.model_validate(
            {"slack": {"static_access_token": secret}}
        )
        response = await client.get("/admin/api/connectors")

    assert response.status_code == 200
    assert secret not in response.text
    listed = {row["key"]: row for row in response.json()["connectors"]}
    assert listed["slack"]["configured"] is True
    assert listed["github"]["configured"] is False


class StubDirectory:
    """Enough of the directory protocol for the users endpoint."""

    def __init__(self, users):
        self._users = users
        self.queries: list[str | None] = []

    def search_users(self, query=None, *, limit=50, first=0):
        self.queries.append(query)
        return self._users[first : first + limit]


@pytest.mark.asyncio
async def test_users_are_listed_with_the_role_the_hub_actually_holds(tmp_path, idp: _StubIdp):
    """The directory knows people; only this hub knows who is an operator."""

    from collab_hub_api.user_directory import UserDirectoryUser

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.user_directory_client = StubDirectory(
            [
                UserDirectoryUser(id=idp.sub, username="alice", email="alice@example.com"),
                UserDirectoryUser(id="u-2", username="bob", email="bob@example.com"),
            ]
        )
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/users")

    assert response.status_code == 200
    listed = {row["id"]: row for row in response.json()["users"]}
    assert listed[idp.sub]["role"] == PLATFORM_ROLE_OPERATOR
    assert listed["u-2"]["role"] is None
    assert listed["u-2"]["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_the_users_endpoint_refuses_a_non_operator(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/users")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_users_endpoint_says_so_when_there_is_no_directory(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/users")

    assert response.status_code == 503
    assert response.json()["error"] == "user_directory_unavailable"


class StubModelAccess:
    """The service seam, recording what the endpoint asked of it."""

    configured = True

    def __init__(self):
        self.changes: list[tuple[str, str, str]] = []

    def list_members(self, group_path):
        from collab_hub_api.frames.group_membership import GroupMember

        return [GroupMember(id="u-2", username="bob", email="bob@example.com")]

    def grant(self, actor, *, user_id, group_path, user_label=None):
        self.changes.append(("grant", user_id, group_path))

    def revoke(self, actor, *, user_id, group_path, user_label=None):
        self.changes.append(("revoke", user_id, group_path))


class StubCatalog:
    configured = True

    def list_models(self):
        from collab_hub_api.frames.model_catalog import CatalogModel

        return [CatalogModel(id="llama-3.1-8b", owned_by="hub")]


@pytest.mark.asyncio
async def test_models_are_listed_with_the_group_that_gates_each(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.model_catalog = StubCatalog()
        app.state.model_groups = {"llama-3.1-8b": "/llm"}
        app.state.model_access = StubModelAccess()
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/models")

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == [{"id": "llama-3.1-8b", "owned_by": "hub", "group_path": "/llm"}]
    assert body["manageable"] is True


@pytest.mark.asyncio
async def test_the_catalogue_degrades_without_taking_the_panel_with_it(tmp_path, idp: _StubIdp):
    from collab_hub_api.frames.model_catalog import ModelCatalogError

    class Broken:
        configured = True

        def list_models(self):
            raise ModelCatalogError("serving layer is down")

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.model_catalog = Broken()
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/models")

    assert response.status_code == 200
    assert response.json() == {"models": [], "catalog_error": "unavailable", "manageable": False}


@pytest.mark.asyncio
async def test_a_membership_change_without_a_csrf_token_is_refused(tmp_path, idp: _StubIdp):
    """The session cookie alone must not be enough to change access."""

    app = build_app(tmp_path, idp)
    access = StubModelAccess()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.model_access = access
        await sign_in(client, idp, next_path="/web")
        response = await client.post(
            "/admin/api/model-access",
            json={"user_id": "u-2", "group_path": "/llm", "action": "grant"},
        )

    assert response.status_code == 403
    assert access.changes == []


@pytest.mark.asyncio
async def test_a_membership_change_with_the_csrf_token_reaches_the_service(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    access = StubModelAccess()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.model_access = access
        await sign_in(client, idp, next_path="/web")
        token = (await client.get("/admin/api/session")).json()["csrf_token"]
        response = await client.post(
            "/admin/api/model-access",
            json={"user_id": "u-2", "group_path": "/llm", "action": "revoke"},
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code == 200
    assert access.changes == [("revoke", "u-2", "/llm")]


class StubInvitations:
    """The invitation service seam, as the panel's endpoints use it."""

    def __init__(self):
        self.created: list[str] = []
        self.revoked: list[str] = []

    def list_all(self, *, limit, offset):
        from datetime import datetime, timedelta, timezone

        from collab_hub_api.frames.invitations import Invitation

        now = datetime.now(tz=timezone.utc)
        from collab_hub_api.frames.invitations import InvitationPage

        return InvitationPage(
            invitations=[
                Invitation(
                    id="inv-1",
                    email="bob@example.com",
                    org_id=None,
                    status="pending",
                    created_at=now,
                    expires_at=now + timedelta(days=3),
                    accepted_at=None,
                    created_by="u-1",
                )
            ],
            has_more=False,
        )

    def server_now(self):
        from datetime import datetime, timezone

        return datetime.now(tz=timezone.utc)


@pytest.mark.asyncio
async def test_invitations_are_listed_for_the_panel(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = StubInvitations()
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/invitations")

    assert response.status_code == 200
    rows = response.json()["invitations"]
    assert [row["email"] for row in rows] == ["bob@example.com"]
    assert [row["status"] for row in rows] == ["pending"]
    # The one-time secret never leaves the service on a listing.
    assert "secret" not in response.text and "token" not in response.text


class ManyInvitations(StubInvitations):
    """Enough invitations to need more than one page, served by offset."""

    def __init__(self, count):
        super().__init__()
        self.count = count

    def list_all(self, *, limit, offset):
        from datetime import datetime, timedelta, timezone

        from collab_hub_api.frames.invitations import Invitation, InvitationPage

        now = datetime.now(tz=timezone.utc)
        ids = range(self.count - 1 - offset, max(self.count - 1 - offset - limit, -1), -1)
        return InvitationPage(
            invitations=[
                Invitation(
                    id=f"inv-{n}",
                    email=f"user{n}@example.com",
                    org_id=None,
                    status="pending",
                    created_at=now,
                    expires_at=now + timedelta(days=3),
                    accepted_at=None,
                    created_by="u-1",
                )
                for n in ids
            ],
            has_more=offset + limit < self.count,
        )


@pytest.mark.asyncio
async def test_every_invitation_can_be_reached_page_by_page(tmp_path, idp: _StubIdp):
    """``has_more`` alone left no way to fetch the rest past the first page."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = ManyInvitations(250)
        await sign_in(client, idp, next_path="/web")
        seen: list[str] = []
        path = "/admin/api/invitations"
        while path:
            body = (await client.get(path)).json()
            seen += [row["id"] for row in body["invitations"]]
            cursor = body["next_offset"]
            path = f"/admin/api/invitations?offset={cursor}" if cursor is not None else ""

    assert seen == [f"inv-{n}" for n in range(249, -1, -1)]


@pytest.mark.asyncio
async def test_the_panel_invitation_list_is_operator_only(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        app.state.invitation_service = StubInvitations()
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin/api/invitations")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_session_reports_the_running_version(tmp_path, idp: _StubIdp):
    """Which build is this? The panel shows it, so it has to come from here.

    Read from the installed package's own metadata rather than a constant that
    someone has to remember to bump: a version that lies is worse than no
    version, because it is quoted in incident reports.
    """

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        await sign_in(client, idp, next_path="/web")
        body = (await client.get(ADMIN_SESSION_PATH)).json()

    from importlib.metadata import version

    assert body["version"] == version("collab-hub-api")


class IssuingInvitations(StubInvitations):
    """Adds the issue and revoke halves of the service seam."""

    def __init__(self, *, live_exists: bool = False):
        super().__init__()
        self.live_exists = live_exists

    def create_unless_live(self, auth, *, email, org_id):
        from datetime import datetime, timedelta, timezone

        from collab_hub_api.frames.credentials import InvitationSecret
        from collab_hub_api.frames.invitations import (
            Invitation,
            IssuedInvitation,
            LiveInvitationExists,
        )

        now = datetime.now(tz=timezone.utc)
        row = Invitation(
            id="inv-new",
            email=email,
            org_id=None,
            status="pending",
            created_at=now,
            expires_at=now + timedelta(days=3),
            accepted_at=None,
            created_by="u-1",
        )
        if self.live_exists:
            return LiveInvitationExists(existing=row)
        self.created.append(email)
        return IssuedInvitation(invitation=row, raw_secret=InvitationSecret("s3cret-not-real"))

    def revoke(self, auth, invitation_id, **kwargs):
        self.revoked.append(invitation_id)
        return None


class RecordingDelivery:
    configured = True

    def __init__(self, status=None):
        from collab_hub_api.frames.invitation_email import DELIVERY_PROVIDER_ACCEPTED

        status = DELIVERY_PROVIDER_ACCEPTED if status is None else status
        self.status = status
        self.sent: list[str] = []

    def deliver(self, *, invitation_id, recipient, invitation_secret, organization_name, expires_at):
        from collab_hub_api.frames.invitation_email import DeliveryOutcome

        self.sent.append(recipient)
        return DeliveryOutcome(status=self.status, error_code=None)


async def issue(client, email, csrf):
    return await client.post(
        "/admin/api/invitations",
        json={"email": email},
        headers={"X-CSRF-Token": csrf},
    )


@pytest.mark.asyncio
async def test_the_panel_can_issue_an_invitation_and_never_returns_its_secret(tmp_path, idp: _StubIdp):
    """The one-time code leaves this process only through the mail adapter."""

    app = build_app(tmp_path, idp)
    service = IssuingInvitations()
    delivery = RecordingDelivery()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = service
        app.state.invitation_email_delivery = delivery
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        response = await issue(client, "bob@example.com", csrf)

    assert response.status_code == 201
    assert service.created == ["bob@example.com"]
    assert delivery.sent == ["bob@example.com"]
    assert response.json()["outcome"] == "sent"
    assert "s3cret-not-real" not in response.text


@pytest.mark.asyncio
async def test_issuing_without_a_csrf_token_is_refused(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    service = IssuingInvitations()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = service
        app.state.invitation_email_delivery = RecordingDelivery()
        await sign_in(client, idp, next_path="/web")
        response = await client.post("/admin/api/invitations", json={"email": "bob@example.com"})

    assert response.status_code == 403
    assert service.created == []


@pytest.mark.asyncio
async def test_a_second_live_invitation_is_refused_rather_than_minted(tmp_path, idp: _StubIdp):
    """One live token per address; issuing twice must not mint a second."""

    app = build_app(tmp_path, idp)
    service = IssuingInvitations(live_exists=True)
    delivery = RecordingDelivery()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = service
        app.state.invitation_email_delivery = delivery
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        response = await issue(client, "bob@example.com", csrf)

    assert response.status_code == 409
    assert response.json()["outcome"] == "already_live"
    assert delivery.sent == []


@pytest.mark.asyncio
async def test_a_committed_invitation_whose_email_failed_says_so(tmp_path, idp: _StubIdp):
    """The invitation exists either way; the wording is the only difference."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = IssuingInvitations()
        app.state.invitation_email_delivery = RecordingDelivery(status="failed")
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        response = await issue(client, "bob@example.com", csrf)

    assert response.status_code == 201
    assert response.json()["outcome"] == "send_failed"


@pytest.mark.asyncio
async def test_the_panel_can_revoke_an_invitation(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    service = IssuingInvitations()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.invitation_service = service
        app.state.invitation_email_delivery = RecordingDelivery()
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        response = await client.post(
            "/admin/api/invitations/inv-1/revoke", headers={"X-CSRF-Token": csrf}
        )

    assert response.status_code == 200
    assert service.revoked == ["inv-1"]


class RecordingRoleAdmin:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def grant(self, actor, *, user_id, user_label=None):
        self.calls.append(("grant", user_id))

    def revoke(self, actor, *, user_id, user_label=None):
        self.calls.append(("revoke", user_id))


@pytest.mark.asyncio
async def test_an_operator_can_grant_and_revoke_the_role(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    admin = RecordingRoleAdmin()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.platform_role_admin = admin
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        granted = await client.post(
            "/admin/api/users/u-2/role",
            json={"action": "grant", "user_label": "bob@example.com"},
            headers={"X-CSRF-Token": csrf},
        )
        revoked = await client.post(
            "/admin/api/users/u-2/role",
            json={"action": "revoke"},
            headers={"X-CSRF-Token": csrf},
        )

    assert granted.status_code == 200 and revoked.status_code == 200
    assert admin.calls == [("grant", "u-2"), ("revoke", "u-2")]


class RefusingRoleAdmin(RecordingRoleAdmin):
    def revoke(self, actor, *, user_id, user_label=None):
        from collab_hub_api.frames.platform_role_admin import PlatformRoleChangeRefused

        raise PlatformRoleChangeRefused("last_operator")


@pytest.mark.asyncio
async def test_a_revoke_that_would_strand_the_deployment_is_a_conflict(tmp_path, idp: _StubIdp):
    """The panel gets a reason it can show, not a 500."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        csrf = await operator_client(app, idp, client)
        app.state.platform_role_admin = RefusingRoleAdmin()
        response = await client.post(
            "/admin/api/users/u-2/role",
            json={"action": "revoke"},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 409
    assert response.json() == {"error": "last_operator"}


@pytest.mark.asyncio
async def test_a_role_change_without_a_csrf_token_is_refused(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    admin = RecordingRoleAdmin()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.platform_role_admin = admin
        await sign_in(client, idp, next_path="/web")
        response = await client.post("/admin/api/users/u-2/role", json={"action": "grant"})

    assert response.status_code == 403
    assert admin.calls == []


@pytest.mark.asyncio
async def test_the_users_listing_says_where_each_role_came_from(tmp_path, idp: _StubIdp):
    from collab_hub_api.user_directory import UserDirectoryUser

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.user_directory_client = StubDirectory(
            [UserDirectoryUser(id=idp.sub, username="alice", email="alice@example.com")]
        )
        await sign_in(client, idp, next_path="/web")
        body = (await client.get("/admin/api/users")).json()

    assert body["users"][0]["role_source"] == "manual"


@pytest.mark.asyncio
async def test_the_users_listing_reads_every_role_at_once(tmp_path, idp: _StubIdp):
    """One read for the page, not two per person: a 200-row page used to be up
    to 400 database round trips."""

    from collab_hub_api.user_directory import UserDirectoryUser

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        store = app.state.org_store
        grant_operator(app, idp.sub)
        store.set_platform_role("u-2")
        store.set_platform_role("u-3", "operator", "active", "idp")
        store.set_platform_role("u-4", "operator", "revoked", "idp")
        app.state.user_directory_client = StubDirectory(
            [
                UserDirectoryUser(id=user_id, username=user_id, email=None)
                for user_id in ("u-2", "u-3", "u-4", "u-5")
            ]
        )
        await sign_in(client, idp, next_path="/web")

        per_person: list[str] = []
        for name in ("resolve_principal", "get_platform_role_row"):
            original = getattr(store, name)

            def spy(user_id, _original=original):
                per_person.append(user_id)
                return _original(user_id)

            setattr(store, name, spy)
        body = (await client.get("/admin/api/users")).json()

    assert {row["id"]: (row["role"], row["role_source"]) for row in body["users"]} == {
        "u-2": ("operator", "manual"),
        "u-3": ("operator", "idp"),
        "u-4": (None, None),
        "u-5": (None, None),
    }
    assert not {"u-2", "u-3", "u-4", "u-5"} & set(per_person)


@pytest.mark.asyncio
async def test_everyone_in_the_directory_can_be_reached_page_by_page(tmp_path, idp: _StubIdp):
    from collab_hub_api.user_directory import UserDirectoryUser

    app = build_app(tmp_path, idp)
    people = [UserDirectoryUser(id=f"u-{n}", username=f"user{n}", email=None) for n in range(120)]

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.user_directory_client = StubDirectory(people)
        await sign_in(client, idp, next_path="/web")
        seen: list[str] = []
        path = "/admin/api/users?limit=50"
        while path:
            body = (await client.get(path)).json()
            seen += [row["id"] for row in body["users"]]
            cursor = body["next_first"]
            path = f"/admin/api/users?limit=50&first={cursor}" if cursor is not None else ""

    assert seen == [person.id for person in people]


class RecordingConnectorStore:
    def __init__(self, disabled=()):
        self._disabled = set(disabled)
        self.calls: list[tuple[str, bool]] = []

    def disabled(self):
        return set(self._disabled)

    def set_enabled(self, actor, *, connector, enabled):
        self.calls.append((connector, enabled))
        if enabled:
            self._disabled.discard(connector)
        else:
            self._disabled.add(connector)


@pytest.mark.asyncio
async def test_a_switched_off_connector_still_shows_as_configured_to_the_panel(tmp_path, idp: _StubIdp):
    """Every other caller should see it as unusable; this screen must not."""

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.connectors_config = ConnectorsConfig.model_validate(
            {"slack": {"static_access_token": "slack-not-a-real-token"}}
        )
        app.state.connector_store = RecordingConnectorStore(disabled={"slack"})
        await sign_in(client, idp, next_path="/web")
        body = (await client.get("/admin/api/connectors")).json()

    slack = {row["key"]: row for row in body["connectors"]}["slack"]
    assert slack["configured"] is True
    assert slack["enabled"] is False
    assert body["switchable"] is True


@pytest.mark.asyncio
async def test_an_operator_can_switch_a_connector_off_and_on(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    store = RecordingConnectorStore()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.connector_store = store
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        off = await client.post(
            "/admin/api/connectors/slack", json={"enabled": False}, headers={"X-CSRF-Token": csrf}
        )
        on = await client.post(
            "/admin/api/connectors/slack", json={"enabled": True}, headers={"X-CSRF-Token": csrf}
        )

    assert off.status_code == 200 and on.status_code == 200
    assert store.calls == [("slack", False), ("slack", True)]


@pytest.mark.asyncio
async def test_an_unknown_connector_cannot_be_switched(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    store = RecordingConnectorStore()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.connector_store = store
        await sign_in(client, idp, next_path="/web")
        csrf = (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]
        response = await client.post(
            "/admin/api/connectors/dropbox",
            json={"enabled": False},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 404
    assert store.calls == []


@pytest.mark.asyncio
async def test_switching_a_connector_without_a_csrf_token_is_refused(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)
    store = RecordingConnectorStore()

    async with app.router.lifespan_context(app), web_client(app) as client:
        grant_operator(app, idp.sub)
        app.state.connector_store = store
        await sign_in(client, idp, next_path="/web")
        response = await client.post("/admin/api/connectors/slack", json={"enabled": False})

    assert response.status_code == 403
    assert store.calls == []


# ---------------------------------------------------------------------------
# What each section answers when the thing behind it is absent. These are the
# states a half-configured deployment actually lands in, so each has to say
# which of "not set up" and "broken" it is.
# ---------------------------------------------------------------------------


async def operator_client(app, idp, client):
    grant_operator(app, idp.sub)
    await sign_in(client, idp, next_path="/web")
    return (await client.get(ADMIN_SESSION_PATH)).json()["csrf_token"]


@pytest.mark.asyncio
async def test_model_access_reports_itself_unavailable_without_a_credential(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        csrf = await operator_client(app, idp, client)
        app.state.model_access = None
        listing = await client.get("/admin/api/model-access?group_path=/llm")
        change = await client.post(
            "/admin/api/model-access",
            json={"user_id": "u-2", "group_path": "/llm", "action": "grant"},
            headers={"X-CSRF-Token": csrf},
        )

    assert listing.status_code == 503
    assert listing.json()["error"] == "model_access_unavailable"
    assert change.status_code == 503


@pytest.mark.asyncio
async def test_a_refusal_from_the_identity_provider_is_reported_as_upstream(tmp_path, idp: _StubIdp):
    """502, so the panel can say "another service said no" rather than "broken"."""

    from collab_hub_api.frames.group_membership import GroupMembershipError

    class Refusing:
        configured = True

        def list_members(self, group_path):
            raise GroupMembershipError("Keycloak refused list: 403")

        def grant(self, *a, **k):
            raise GroupMembershipError("Keycloak refused")

        def revoke(self, *a, **k):
            raise GroupMembershipError("Keycloak refused")

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        csrf = await operator_client(app, idp, client)
        app.state.model_access = Refusing()
        listing = await client.get("/admin/api/model-access?group_path=/llm")
        change = await client.post(
            "/admin/api/model-access",
            json={"user_id": "u-2", "group_path": "/llm", "action": "grant"},
            headers={"X-CSRF-Token": csrf},
        )

    assert listing.status_code == 502 and listing.json()["error"] == "group_unavailable"
    assert change.status_code == 502


@pytest.mark.asyncio
async def test_role_changes_are_refused_without_somewhere_to_record_them(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        csrf = await operator_client(app, idp, client)
        app.state.platform_role_admin = None
        response = await client.post(
            "/admin/api/users/u-2/role",
            json={"action": "grant"},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 503
    assert response.json()["error"] == "role_management_unavailable"


@pytest.mark.asyncio
async def test_connectors_cannot_be_switched_without_a_store(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        csrf = await operator_client(app, idp, client)
        app.state.connector_store = None
        listing = await client.get("/admin/api/connectors")
        change = await client.post(
            "/admin/api/connectors/slack",
            json={"enabled": False},
            headers={"X-CSRF-Token": csrf},
        )

    # Still listed, so the screen can show what exists; just not switchable.
    assert listing.status_code == 200 and listing.json()["switchable"] is False
    assert change.status_code == 503


@pytest.mark.asyncio
async def test_an_unconfigured_catalogue_reads_differently_from_a_broken_one(tmp_path, idp: _StubIdp):
    class NotConfigured:
        configured = False

        def list_models(self):
            raise AssertionError("an unconfigured catalogue is never asked")

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        await operator_client(app, idp, client)
        app.state.model_catalog = NotConfigured()
        response = await client.get("/admin/api/models")

    assert response.json()["catalog_error"] == "not_configured"


@pytest.mark.asyncio
async def test_invitations_report_an_unavailable_service(tmp_path, idp: _StubIdp):
    from collab_hub_api.frames.invitations import InvitationsUnavailableError

    class Unavailable:
        def list_all(self, **kwargs):
            raise InvitationsUnavailableError("no database")

        def create_unless_live(self, *a, **k):
            raise InvitationsUnavailableError("no database")

        def server_now(self):
            raise InvitationsUnavailableError("no database")

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        csrf = await operator_client(app, idp, client)
        app.state.invitation_service = Unavailable()
        app.state.invitation_email_delivery = RecordingDelivery()
        listing = await client.get("/admin/api/invitations")
        issued = await issue(client, "bob@example.com", csrf)

    assert listing.status_code == 503
    assert issued.status_code == 503 and issued.json()["outcome"] == "unavailable"
