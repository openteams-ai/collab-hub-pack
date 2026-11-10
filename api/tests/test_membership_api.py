"""The membership API for add-on services (issue #270).

Pinned here:

1. **Who may call.** Only a configured add-on's client-credentials token:
   the wrong service, a person's token (even one issued through an add-on's
   own client), an ID token, an unsigned or foreign-signed token, a browser
   cookie and no credential at all are each refused.
2. **What it says.** Exactly the documented fields, and nothing else about an
   organization.
3. **Freshness.** A removal, an ownership change and a join are in the very
   next answer: nothing is cached on the hub side.
4. **Off by default.** No client ids, no routes, and the existing surfaces
   are unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import time
from contextlib import asynccontextmanager

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from collab_hub_api.config import Config, FramesMembershipApiConfig
from collab_hub_api.core import _self_authenticating_paths, make_app
from collab_hub_api.frames import auth
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.org_source import DEFAULT_ORG_ENV, DEFAULT_WORKSPACE_ENV, ORG_SOURCE_ENV
from collab_hub_api.frames.orgs import (
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_REMOVED,
    ROLE_MEMBER,
    ROLE_OWNER,
    InMemoryOrgStore,
    OrgMember,
    OrgRoster,
    OrgSchemaMissingError,
    OrgsUnavailableError,
    PostgresOrgStore,
    UnavailableOrgStore,
)
from collab_hub_api.routers.membership import membership_api_path

psycopg = pytest.importorskip("psycopg")

ISSUER = "https://keycloak.example.test/realms/collab"
JWKS_URL = "https://keycloak.example.test/realms/collab/protocol/openid-connect/certs"
USAGE_CONTROL = "usage-control"
SUBSCRIPTIONS = "subscription-manager"

ALICE = "a1b2c3d4-1111-4111-8111-abcdefabcdef"
BOB = "22222222-2222-4222-8222-b0b0b0b0b0b0"
CAROL = "33333333-3333-4333-8333-cacacacacaca"
ORG_ONE = "org-1111"
ORG_TWO = "org-2222"
ORG_THREE = "org-3333"


def _key() -> tuple[bytes, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private_key.public_key()


REALM_PEM, REALM_PUBLIC = _key()
FOREIGN_PEM, _ = _key()


class _StaticJwksClient:
    """Stands in for the shared JWKS client: always the realm's one key."""

    class _SigningKey:
        key = REALM_PUBLIC

    def get_signing_key_from_jwt(self, token: str):
        return self._SigningKey()


def _signed(claims: dict, pem: bytes = REALM_PEM) -> str:
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "realm"})


def service_claims(client: str, **overrides) -> dict:
    """A Keycloak 26 client-credentials access token's claims, for *client*."""

    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": f"service-account-{client}",
        "azp": client,
        "client_id": client,
        "typ": "Bearer",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def service(client: str = USAGE_CONTROL, **overrides) -> dict[str, str]:
    return bearer(_signed(service_claims(client, **overrides)))


def _unsigned(payload: dict) -> str:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _config(tmp_path, *, client_ids=(USAGE_CONTROL, SUBSCRIPTIONS), audience: str = "", **overrides) -> Config:
    frames = {
        "active_state": {"backend": "memory"},
        "history": {"backend": "memory"},
        "groups": {"backend": "memory"},
        "usage": {"backend": "memory"},
        "orgs": {"backend": "memory"},
        "membership_api": {"client_ids": list(client_ids), "audience": audience},
        "mcp_session_manager_enabled": False,
    }
    payload = {"storage": {"frames_path": str(tmp_path / "frames")}, "frames": frames, "tasks": {"backend": "memory"}}
    payload.update(overrides)
    return Config.parse(payload)


@pytest.fixture
def membership_env(monkeypatch):
    """Membership mode, verifying bearer tokens against the realm key."""

    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", ISSUER)
    # People's IdToken cookies stay unsigned so a test can present one easily;
    # bearer tokens -- the only kind this API reads -- are verified.
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.delenv("FRAMES_BEARER_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("DEV_AUTH_ENABLED", raising=False)
    monkeypatch.setattr(auth, "_get_jwks_client", lambda url: _StaticJwksClient())


@asynccontextmanager
async def running(config: Config):
    app = make_app(config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, app.state.org_store


def seed(store: InMemoryOrgStore) -> None:
    store.set_org(ORG_ONE, "Acme")
    store.set_membership(ALICE, ORG_ONE, ROLE_OWNER, email="alice@example.test", display_name="Alice A")
    store.set_membership(BOB, ORG_ONE, ROLE_MEMBER, email="bob@example.test", display_name="Bob B")
    # A member the hub holds no label for is still a member.
    store.set_membership(CAROL, ORG_TWO, ROLE_OWNER)


@pytest.fixture
async def api(tmp_path, membership_env):
    async with running(_config(tmp_path)) as (client, store):
        seed(store)
        yield client, store


# --------------------------------------------------------------------------
# Who may call
# --------------------------------------------------------------------------


async def test_each_configured_add_on_is_accepted(api):
    client, _ = api
    for name in (USAGE_CONTROL, SUBSCRIPTIONS):
        response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=service(name))
        assert response.status_code == 200, response.text


async def test_the_pre_keycloak_25_client_id_claim_is_accepted(api):
    client, _ = api
    headers = service(USAGE_CONTROL, client_id=None, clientId=USAGE_CONTROL)
    response = await client.get(f"/v1/membership/subjects/{ALICE}", headers=headers)
    assert response.status_code == 200, response.text


async def test_an_unauthenticated_call_is_refused(api):
    client, _ = api
    for path in (
        f"/v1/membership/subjects/{ALICE}",
        f"/v1/membership/organizations/{ORG_ONE}",
        "/v1/membership/organizations",
    ):
        response = await client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"


async def test_the_wrong_service_identity_is_refused(api):
    client, _ = api
    response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=service("some-other-service"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_a_users_token_is_refused_even_through_an_add_ons_own_client(api):
    """A person who signs in through the add-on's client gets its ``azp`` but no ``client_id``."""

    client, _ = api
    person = service_claims(USAGE_CONTROL, sub=ALICE, client_id=None, preferred_username="alice")
    response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=bearer(_signed(person)))
    assert response.status_code == 403


async def test_a_users_token_from_the_desktop_client_is_refused(api):
    client, _ = api
    person = service_claims("collab-desktop", sub=ALICE, client_id=None)
    response = await client.get(f"/v1/membership/subjects/{ALICE}", headers=bearer(_signed(person)))
    assert response.status_code == 403


async def test_azp_alone_is_not_enough(api):
    """``azp`` names an add-on, but the client-credentials claim names something else."""

    client, _ = api
    for mismatch in ({"client_id": "some-other-service"}, {"client_id": None, "clientId": "some-other-service"}):
        response = await client.get(f"/v1/membership/subjects/{ALICE}", headers=service(USAGE_CONTROL, **mismatch))
        assert response.status_code == 403, mismatch


async def test_an_id_token_is_refused(api):
    client, _ = api
    response = await client.get(f"/v1/membership/subjects/{ALICE}", headers=service(USAGE_CONTROL, typ="ID"))
    assert response.status_code == 401


async def test_a_token_without_an_expiry_is_refused(api):
    client, _ = api
    for exp in (None, True):
        response = await client.get(f"/v1/membership/subjects/{ALICE}", headers=service(USAGE_CONTROL, exp=exp))
        assert response.status_code == 401, exp


async def test_forged_expired_foreign_and_unsigned_tokens_are_refused(api):
    client, _ = api
    tokens = {
        "foreign key": _signed(service_claims(USAGE_CONTROL), pem=FOREIGN_PEM),
        "expired": _signed(service_claims(USAGE_CONTROL, exp=int(time.time()) - 60)),
        "other issuer": _signed(service_claims(USAGE_CONTROL, iss="https://elsewhere.example.test/realms/x")),
        "unsigned": _unsigned(service_claims(USAGE_CONTROL)),
        "garbage": "not-a-jwt",
    }
    for label, token in tokens.items():
        response = await client.get(f"/v1/membership/subjects/{ALICE}", headers=bearer(token))
        assert response.status_code == 401, label


async def test_a_browser_session_is_refused(api):
    """An owner's IdToken cookie is a valid people credential elsewhere, and worthless here."""

    client, _ = api
    owner_cookie = {"Cookie": f"IdToken-test={_unsigned({'sub': ALICE})}"}
    response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=owner_cookie)
    assert response.status_code == 403
    # The same owner can read their own identity through the people path, so
    # the refusal above is this API's policy, not a broken cookie.
    me = await client.get("/v1/me", headers=owner_cookie)
    assert me.status_code == 200 and me.json()["org_role"] == ROLE_OWNER


async def test_any_cookie_is_refused_even_beside_a_valid_service_token(api):
    client, _ = api
    response = await client.get(
        f"/v1/membership/organizations/{ORG_ONE}",
        headers={**service(), "Cookie": "__Host-collab_session=abc"},
    )
    assert response.status_code == 403


async def test_a_forwarded_user_header_grants_nothing(api):
    client, _ = api
    response = await client.get(
        f"/v1/membership/organizations/{ORG_ONE}",
        headers={"X-Forwarded-User": ALICE, "X-Auth-Request-User": ALICE, "X-Forwarded-Email": "alice@example.test"},
    )
    assert response.status_code == 401


async def test_an_audience_is_enforced_when_configured(tmp_path, membership_env):
    async with running(_config(tmp_path, audience="collab-hub-membership")) as (client, store):
        seed(store)
        path = f"/v1/membership/subjects/{ALICE}"
        assert (await client.get(path, headers=service())).status_code == 401
        assert (await client.get(path, headers=service(aud="account"))).status_code == 401
        assert (await client.get(path, headers=service(aud=["account", "collab-hub-membership"]))).status_code == 200


async def test_hardened_protection_map_does_not_put_a_people_check_in_front(tmp_path, membership_env):
    """With ``default_access: authenticated`` the routes still answer services, and still refuse people."""

    config = _config(tmp_path, security={"default_access": "authenticated"})
    async with running(config) as (client, store):
        seed(store)
        assert (await client.get(f"/v1/membership/subjects/{ALICE}", headers=service())).status_code == 200
        assert (await client.get(f"/v1/membership/subjects/{ALICE}")).status_code == 401
        # And the rest of the API is still behind the people check.
        assert (await client.get("/v1/me", headers=service())).status_code == 403


async def test_unsigned_service_tokens_work_only_through_the_dev_door(tmp_path, membership_env, monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_JWKS_URL")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    async with running(_config(tmp_path)) as (client, store):
        seed(store)
        token = _unsigned(service_claims(USAGE_CONTROL))
        assert (await client.get(f"/v1/membership/subjects/{ALICE}", headers=bearer(token))).status_code == 200
        person = _unsigned(service_claims(USAGE_CONTROL, client_id=None, sub=ALICE))
        assert (await client.get(f"/v1/membership/subjects/{ALICE}", headers=bearer(person))).status_code == 403


# --------------------------------------------------------------------------
# What it says, field for field
# --------------------------------------------------------------------------


async def test_subject_memberships_expose_exactly_org_and_role(api):
    client, _ = api
    body = (await client.get(f"/v1/membership/subjects/{ALICE}", headers=service())).json()
    assert set(body) == {"subject", "organizations", "observed_at"}
    assert body["subject"] == ALICE
    assert body["organizations"] == [{"org_id": ORG_ONE, "role": ROLE_OWNER}]


async def test_a_subject_with_no_membership_has_no_organizations(api):
    client, _ = api
    body = (await client.get("/v1/membership/subjects/nobody", headers=service())).json()
    assert body["organizations"] == []


async def test_an_organization_exposes_exactly_its_members_and_count(api):
    client, _ = api
    body = (await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=service())).json()
    assert set(body) == {"org_id", "name", "member_count", "members", "observed_at"}
    assert body["org_id"] == ORG_ONE and body["name"] == "Acme"
    assert body["member_count"] == 2
    # Ordered by subject.
    assert body["members"] == [
        {"subject": BOB, "role": ROLE_MEMBER, "display_name": "Bob B", "email": "bob@example.test"},
        {"subject": ALICE, "role": ROLE_OWNER, "display_name": "Alice A", "email": "alice@example.test"},
    ]


async def test_a_member_without_a_label_is_listed_not_dropped(api):
    client, _ = api
    body = (await client.get(f"/v1/membership/organizations/{ORG_TWO}", headers=service())).json()
    assert body["member_count"] == 1
    assert body["members"] == [{"subject": CAROL, "role": ROLE_OWNER, "display_name": None, "email": None}]
    assert body["name"] is None


async def test_an_unknown_organization_is_a_404_with_its_own_code(api):
    client, _ = api
    response = await client.get("/v1/membership/organizations/no-such-org", headers=service())
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "organization_not_found", "message": "No such organization"}}


async def test_the_listing_pages_every_organization_with_the_same_fields(api):
    client, store = api
    store.set_org(ORG_THREE, "Empty")
    first = (await client.get("/v1/membership/organizations?limit=2", headers=service())).json()
    assert set(first) == {"organizations", "next_cursor", "observed_at"}
    assert [o["org_id"] for o in first["organizations"]] == [ORG_ONE, ORG_TWO]
    assert all(set(o) == {"org_id", "name", "member_count", "members"} for o in first["organizations"])
    assert first["next_cursor"] == ORG_TWO
    second = (
        await client.get(f"/v1/membership/organizations?limit=2&cursor={first['next_cursor']}", headers=service())
    ).json()
    assert second["organizations"] == [{"org_id": ORG_THREE, "name": "Empty", "member_count": 0, "members": []}]
    assert second["next_cursor"] is None
    everything = (await client.get("/v1/membership/organizations", headers=service())).json()
    assert len(everything["organizations"]) == 3 and everything["next_cursor"] is None


async def test_the_listing_refuses_out_of_range_pages(api):
    client, _ = api
    for query in ("limit=0", "limit=501", "cursor="):
        response = await client.get(f"/v1/membership/organizations?{query}", headers=service())
        assert response.status_code == 422, query


async def test_an_unavailable_store_is_a_503_not_an_empty_roster(api, monkeypatch):
    client, store = api

    def unavailable(*args, **kwargs):
        raise OrgsUnavailableError("down")

    monkeypatch.setattr(store, "get_roster", unavailable)
    response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=service())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "organizations_unavailable"


# --------------------------------------------------------------------------
# Freshness: the next read sees every change
# --------------------------------------------------------------------------


async def test_removal_ownership_change_and_join_are_visible_on_the_next_read(api):
    client, store = api
    path = f"/v1/membership/organizations/{ORG_ONE}"
    before = (await client.get(path, headers=service())).json()
    assert before["member_count"] == 2

    # Ownership moves from Alice to Bob, then Alice is removed and Carol-less
    # newcomer Dan joins: each read reflects every change committed before it.
    store.set_membership(BOB, ORG_ONE, ROLE_OWNER, email="bob@example.test", display_name="Bob B")
    store.set_membership(ALICE, ORG_ONE, ROLE_MEMBER, email="alice@example.test", display_name="Alice A")
    after_transfer = (await client.get(path, headers=service())).json()
    assert {m["subject"]: m["role"] for m in after_transfer["members"]} == {ALICE: ROLE_MEMBER, BOB: ROLE_OWNER}

    store.set_membership(ALICE, ORG_ONE, ROLE_MEMBER, status=MEMBERSHIP_REMOVED)
    after_removal = (await client.get(path, headers=service())).json()
    assert [m["subject"] for m in after_removal["members"]] == [BOB]
    assert after_removal["member_count"] == 1
    subject = (await client.get(f"/v1/membership/subjects/{ALICE}", headers=service())).json()
    assert subject["organizations"] == []

    store.set_membership("dan", ORG_ONE, ROLE_MEMBER)
    after_join = (await client.get(path, headers=service())).json()
    assert after_join["member_count"] == 2
    listing = (await client.get("/v1/membership/organizations", headers=service())).json()
    assert next(o for o in listing["organizations"] if o["org_id"] == ORG_ONE)["member_count"] == 2


async def test_an_organization_whose_members_all_left_still_answers(api):
    client, store = api
    store.set_membership(CAROL, ORG_TWO, ROLE_OWNER, status=MEMBERSHIP_REMOVED)
    body = (await client.get(f"/v1/membership/organizations/{ORG_TWO}", headers=service())).json()
    assert body["member_count"] == 0 and body["members"] == []


# --------------------------------------------------------------------------
# Off by default, and the preconditions when on
# --------------------------------------------------------------------------


async def test_without_client_ids_nothing_is_mounted(tmp_path, membership_env):
    async with running(_config(tmp_path, client_ids=())) as (client, store):
        seed(store)
        for headers in (service(), {"Cookie": f"IdToken-test={_unsigned({'sub': ALICE})}"}):
            response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=headers)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
    # Under a hardened map the people check runs first, as for any unknown
    # path, and a service token -- no membership of its own -- gets no further.
    async with running(_config(tmp_path, client_ids=(), security={"default_access": "authenticated"})) as (client, _):
        response = await client.get(f"/v1/membership/organizations/{ORG_ONE}", headers=service())
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "no_organization"


def test_default_config_has_no_add_ons():
    assert Config.parse({}).frames.membership_api == FramesMembershipApiConfig()
    assert FramesMembershipApiConfig().client_ids == []


def test_client_ids_require_membership_resolution(tmp_path, membership_env, monkeypatch):
    monkeypatch.setenv(ORG_SOURCE_ENV, "claims")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "")
    with pytest.raises(RuntimeError, match="not resolved from membership"):
        make_app(_config(tmp_path))


def test_client_ids_require_a_way_to_verify_tokens(tmp_path, membership_env, monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_JWKS_URL")
    with pytest.raises(RuntimeError, match="no bearer JWKS URL"):
        make_app(_config(tmp_path))


@pytest.mark.parametrize(
    "settings",
    [
        {"client_ids": [""]},
        {"client_ids": [" usage-control"]},
        {"client_ids": ["usage control"]},
        {"client_ids": ["usage-control", "usage-control"]},
        {"audience": " collab "},
    ],
)
def test_malformed_settings_are_refused(settings):
    with pytest.raises(ValidationError):
        FramesMembershipApiConfig(**settings)


def test_settings_parse_from_the_environment_as_the_chart_renders_them(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__MEMBERSHIP_API__CLIENT_IDS", '["usage-control","subscription-manager"]')
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__MEMBERSHIP_API__AUDIENCE", "collab-hub-membership")
    settings = Config().frames.membership_api
    assert settings.client_ids == [USAGE_CONTROL, SUBSCRIPTIONS]
    assert settings.audience == "collab-hub-membership"


def test_path_predicates_are_segment_aware():
    assert membership_api_path("/v1/membership")
    assert membership_api_path("/v1/membership/organizations")
    assert not membership_api_path("/v1/membershipx")
    assert _self_authenticating_paths(registry_serving=False, membership_api=False) is None
    both = _self_authenticating_paths(registry_serving=True, membership_api=True)
    assert both("/v2/") and both("/v1/membership/organizations") and not both("/v1/frames")


# --------------------------------------------------------------------------
# The stores
# --------------------------------------------------------------------------


def test_the_unavailable_store_raises_rather_than_answering_empty():
    store = UnavailableOrgStore()
    with pytest.raises(OrgsUnavailableError):
        store.get_roster(ORG_ONE)
    with pytest.raises(OrgsUnavailableError):
        store.list_rosters(after=None, limit=10)


def test_in_memory_admission_records_the_contact_columns_and_the_org_name():
    store = InMemoryOrgStore()
    store.provision_member(ALICE, ORG_ONE, "Declared", email="alice@example.test", display_name="Alice")
    store.provision_member(BOB, ORG_ONE, "Renamed?")
    roster = store.get_roster(ORG_ONE)
    assert roster == OrgRoster(
        org_id=ORG_ONE,
        name="Declared",
        members=(
            OrgMember(user_id=BOB, role=ROLE_MEMBER, email=None, display_name=None),
            OrgMember(user_id=ALICE, role=ROLE_MEMBER, email="alice@example.test", display_name="Alice"),
        ),
    )


class _RecordingConnection:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.statements: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.error is not None:
            raise self.error
        self.statements.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


class _StubDatabase:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return self._connection


def _row(org_id, name, user_id=None, role=None, email=None, display_name=None):
    return {
        "org_id": org_id,
        "name": name,
        "user_id": user_id,
        "role": role,
        "email": email,
        "display_name": display_name,
    }


def test_postgres_roster_groups_rows_and_filters_status_in_the_join():
    connection = _RecordingConnection(
        [_row(ORG_ONE, "Acme", ALICE, ROLE_OWNER, "a@example.test", "Alice"), _row(ORG_ONE, "Acme", BOB, ROLE_MEMBER)]
    )
    roster = PostgresOrgStore(_StubDatabase(connection)).get_roster(ORG_ONE)
    assert roster.org_id == ORG_ONE and [m.user_id for m in roster.members] == [ALICE, BOB]
    sql, params = connection.statements[0]
    assert "LEFT JOIN collab_org_members m ON m.org_id = o.id AND m.status = %s" in sql
    assert params == (MEMBERSHIP_ACTIVE, ORG_ONE)


def test_postgres_roster_of_an_unknown_org_is_none_and_an_empty_one_is_empty():
    assert PostgresOrgStore(_StubDatabase(_RecordingConnection([]))).get_roster("nope") is None
    empty = PostgresOrgStore(_StubDatabase(_RecordingConnection([_row(ORG_TWO, None)]))).get_roster(ORG_TWO)
    assert empty == OrgRoster(org_id=ORG_TWO, name=None, members=())


def test_postgres_listing_groups_each_page():
    connection = _RecordingConnection(
        [_row(ORG_ONE, "Acme", ALICE, ROLE_OWNER), _row(ORG_TWO, None), _row(ORG_THREE, "C", CAROL, ROLE_OWNER)]
    )
    rosters = PostgresOrgStore(_StubDatabase(connection)).list_rosters(after=ORG_ONE, limit=3)
    assert [(r.org_id, len(r.members)) for r in rosters] == [(ORG_ONE, 1), (ORG_TWO, 0), (ORG_THREE, 1)]
    assert connection.statements[0][1] == (ORG_ONE, ORG_ONE, 3, MEMBERSHIP_ACTIVE)


def test_postgres_roster_reads_map_a_missing_schema_onto_the_503_class():
    for read in (lambda s: s.get_roster(ORG_ONE), lambda s: s.list_rosters(after=None, limit=5)):
        store = PostgresOrgStore(_StubDatabase(_RecordingConnection(error=psycopg.errors.UndefinedTable())))
        with pytest.raises(OrgSchemaMissingError):
            read(store)


# --------------------------------------------------------------------------
# Against a real database (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# --------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live membership API tests",
)


@pytest.fixture
def migrated_database():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=5, timeout_seconds=10.0)

    def drop_all() -> None:
        with database.connection() as conn:
            tables = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename LIKE 'collab\\_%'"
            ).fetchall()
            for row in tables:
                conn.execute(f'DROP TABLE IF EXISTS "{row["tablename"]}" CASCADE')

    try:
        drop_all()
        run_collab_schema_migrations(database)
        yield database
        drop_all()
    finally:
        database.close()


@live_postgres
def test_live_rosters_reflect_removal_and_ownership_on_the_next_read(migrated_database):
    store = PostgresOrgStore(migrated_database)
    with migrated_database.connection() as conn:
        for org_id, name in ((ORG_ONE, "Acme"), (ORG_TWO, None), (ORG_THREE, "Empty")):
            conn.execute("INSERT INTO collab_orgs (id, name, created_by) VALUES (%s, %s, 'test')", (org_id, name))
        for user_id, org_id, role, email, display in (
            (ALICE, ORG_ONE, ROLE_OWNER, "alice@example.test", "Alice"),
            (BOB, ORG_ONE, ROLE_MEMBER, None, None),
            (CAROL, ORG_TWO, ROLE_OWNER, None, "Carol"),
        ):
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role, email, display_name)"
                " VALUES (%s, %s, %s, %s, %s)",
                (user_id, org_id, role, email, display),
            )

    assert store.get_roster(ORG_ONE) == OrgRoster(
        org_id=ORG_ONE,
        name="Acme",
        members=(
            OrgMember(user_id=BOB, role=ROLE_MEMBER, email=None, display_name=None),
            OrgMember(user_id=ALICE, role=ROLE_OWNER, email="alice@example.test", display_name="Alice"),
        ),
    )
    assert store.get_roster("no-such-org") is None
    # The name column is nullable, and a null name is reported as such.
    assert store.get_roster(ORG_TWO).name is None

    first = store.list_rosters(after=None, limit=2)
    assert [r.org_id for r in first] == [ORG_ONE, ORG_TWO]
    rest = store.list_rosters(after=ORG_TWO, limit=2)
    assert rest == [OrgRoster(org_id=ORG_THREE, name="Empty", members=())]

    with migrated_database.connection() as conn:
        conn.execute("UPDATE collab_org_members SET role = 'owner' WHERE user_id = %s", (BOB,))
        conn.execute("UPDATE collab_org_members SET status = 'removed' WHERE user_id = %s", (ALICE,))
    roster = store.get_roster(ORG_ONE)
    assert roster.members == (OrgMember(user_id=BOB, role=ROLE_OWNER, email=None, display_name=None),)

    with migrated_database.connection() as conn:
        conn.execute("UPDATE collab_org_members SET status = 'removed' WHERE user_id = %s", (CAROL,))
    assert store.get_roster(ORG_TWO).members == ()
    assert [r.org_id for r in store.list_rosters(after=None, limit=10)] == [ORG_ONE, ORG_TWO, ORG_THREE]
