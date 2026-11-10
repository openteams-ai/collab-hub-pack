"""The membership-writing granter (issue #180).

Everything here runs against `httpx.MockTransport`, the same way the user
directory's tests do — no Keycloak and no cluster. What is worth proving is not
that an HTTP call can be made, but the three things that make this credential
safe to hand out:

* it resolves configured group **paths** at startup, and a path that does not
  exist stops the deployment rather than failing per invitee;
* it grants by adding a membership and does **nothing else** — one `PUT`, no
  read-then-write, no user modification;
* a group nested under a parent is found, so the realm can organise service
  groups without this client constraining it.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from collab_hub_api.frames.account_provisioning import ServiceAccessError
from collab_hub_api.frames.keycloak_service_access import KeycloakServiceAccessGranter

TOKEN_PATH = "/protocol/openid-connect/token"


def _granter(handler, *, group_paths=("/llm",), group_ids=None) -> KeycloakServiceAccessGranter:
    return KeycloakServiceAccessGranter(
        token_url="https://keycloak.example/realms/hub/protocol/openid-connect/token",
        admin_api_base_url="https://keycloak.example/admin/realms/hub",
        client_id="collab-invitation-provisioning",
        client_secret="secret",
        group_paths=group_paths,
        group_ids=group_ids,
        transport=httpx.MockTransport(handler),
    )


def _token(request: httpx.Request) -> httpx.Response:
    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert body["client_id"] == ["collab-invitation-provisioning"]
    return httpx.Response(200, json={"access_token": "granter-token", "expires_in": 300})


def test_a_configured_group_resolves_to_its_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        assert request.url.path.endswith("/groups")
        assert request.url.params["search"] == "llm"
        return httpx.Response(200, json=[{"id": "group-llm", "name": "llm", "path": "/llm"}])

    granter = _granter(handler)
    assert granter.resolve_groups() == {"/llm": "group-llm"}
    granter.close()


def test_a_nested_group_is_found_so_the_realm_can_organise_them():
    """`/services/llm` must resolve, or the parent-group pattern this design
    recommends would be unusable by the code that has to grant into it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "group-services",
                    "name": "services",
                    "path": "/services",
                    "subGroups": [{"id": "group-llm", "name": "llm", "path": "/services/llm"}],
                }
            ],
        )

    granter = _granter(handler, group_paths=("/services/llm",))
    assert granter.resolve_groups() == {"/services/llm": "group-llm"}
    granter.close()


def test_a_same_named_group_at_the_wrong_path_does_not_satisfy_the_config():
    """Granting by path means the path is the identity. A group called `llm`
    somewhere else in the tree is a different group."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        return httpx.Response(200, json=[{"id": "group-elsewhere", "name": "llm", "path": "/somewhere/llm"}])

    granter = _granter(handler)
    with pytest.raises(ServiceAccessError, match="does not exist in the realm"):
        granter.resolve_groups()
    granter.close()


def test_a_missing_group_names_itself_in_the_error():
    """ "A group is missing" makes the reader compare two lists by hand."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        return httpx.Response(200, json=[])

    granter = _granter(handler, group_paths=("/llmm",))
    with pytest.raises(ServiceAccessError, match=r"'/llmm'"):
        granter.resolve_groups()
    granter.close()


def test_granting_is_one_put_and_touches_nothing_else():
    """The whole authority, exercised: add a membership. No read of the user, no
    read of the membership first, no modification of the account."""

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "group-llm", "name": "llm", "path": "/llm"}])
        return httpx.Response(204)

    granter = _granter(handler)
    granter.resolve_groups()
    granter.grant(user_id="kc-user-1", group_path="/llm")
    granter.close()

    assert seen == [
        ("GET", "/admin/realms/hub/groups"),
        ("PUT", "/admin/realms/hub/users/kc-user-1/groups/group-llm"),
    ]
    methods = {method for method, _ in seen}
    assert methods == {"GET", "PUT"}, "no POST, no DELETE, no user modification"


def test_granting_an_unresolved_path_is_a_wiring_error_not_a_lookup():
    """A path nobody configured must not be resolved on demand: that would let
    a typo become a per-acceptance failure again, which is what resolving at
    startup exists to prevent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        return httpx.Response(200, json=[{"id": "group-llm", "name": "llm", "path": "/llm"}])

    granter = _granter(handler)
    granter.resolve_groups()
    with pytest.raises(ServiceAccessError, match="was not resolved at startup"):
        granter.grant(user_id="kc-user-1", group_path="/never-configured")
    granter.close()


def test_a_refused_grant_raises_service_access_error():
    """Which is what the acceptance path catches and logs, rather than failing
    an acceptance that has already committed."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "group-llm", "name": "llm", "path": "/llm"}])
        return httpx.Response(403, json={"error": "insufficient_scope"})

    granter = _granter(handler)
    granter.resolve_groups()
    with pytest.raises(ServiceAccessError, match="HTTP 403"):
        granter.grant(user_id="kc-user-1", group_path="/llm")
    granter.close()


def test_an_expired_token_is_retried_once():
    """A token can lapse between the check and the call. That is not a failure
    worth surfacing to an invitee's acceptance."""

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "group-llm", "name": "llm", "path": "/llm"}])
        calls.append(1)
        return httpx.Response(401) if len(calls) == 1 else httpx.Response(204)

    granter = _granter(handler)
    granter.resolve_groups()
    granter.grant(user_id="kc-user-1", group_path="/llm")
    granter.close()
    assert len(calls) == 2, "one retry on a fresh token"


def test_the_granter_reports_itself_configured():
    """The flag the acceptance path checks before doing anything."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    granter = _granter(handler)
    assert granter.configured is True
    granter.close()


# ===========================================================================
# The startup gate, at the builder
# ===========================================================================


def test_no_configured_groups_yields_the_disabled_granter():
    """The default. Nothing hands out service access because a deployment
    forgot to say otherwise -- that is the shape of the behaviour this
    replaced (an internal issue)."""

    from collab_hub_api.config import Config, build_service_access_granter
    from collab_hub_api.frames.account_provisioning import DisabledServiceAccessGranter

    granter = build_service_access_granter(Config.parse({}))
    assert isinstance(granter, DisabledServiceAccessGranter)
    assert granter.configured is False


def test_groups_without_a_credential_refuses_to_start():
    """A deployment that promises service access and cannot grant it strands
    every invitee it accepts. Better to not come up."""

    from collab_hub_api.config import Config, build_service_access_granter

    with pytest.raises(RuntimeError, match="frames.service_access.keycloak"):
        build_service_access_granter(Config.parse({"frames": {"service_access": {"grant_on_acceptance": ["/llm"]}}}))


def test_the_refusal_names_the_groups_and_the_missing_fields():
    """So the reader fixes it in one pass instead of discovering fields one
    restart at a time."""

    from collab_hub_api.config import Config, build_service_access_granter

    with pytest.raises(RuntimeError) as raised:
        build_service_access_granter(
            Config.parse(
                {
                    "frames": {
                        "service_access": {
                            "grant_on_acceptance": ["/llm"],
                            "keycloak": {"issuer_url": "https://keycloak.example/realms/hub"},
                        }
                    }
                }
            )
        )
    message = str(raised.value)
    assert "/llm" in message
    assert "client_id" in message and "client_secret" in message
    assert "admin_api_base_url" in message
    assert "issuer_url" not in message, "a field that is set must not be reported missing"


def test_the_builder_closes_the_granter_when_resolution_fails():
    """Refusing to start is intended; leaking a connection pool is not.

    A misconfigured deployment restarts in a loop, so a pool leaked per attempt
    compounds (raised in review of #183). The granter owns an `httpx.Client`
    from construction, before it is asked to resolve anything.
    """

    from collab_hub_api.config import Config, build_service_access_granter
    from collab_hub_api.frames import keycloak_service_access as module

    closed: list[bool] = []
    real_resolve = module.KeycloakServiceAccessGranter.resolve_groups
    real_close = module.KeycloakServiceAccessGranter.close

    def failing_resolve(self):
        raise ServiceAccessError("configured service group '/nope' does not exist in the realm")

    def recording_close(self):
        closed.append(True)
        real_close(self)

    module.KeycloakServiceAccessGranter.resolve_groups = failing_resolve
    module.KeycloakServiceAccessGranter.close = recording_close
    try:
        with pytest.raises(ServiceAccessError, match="does not exist"):
            build_service_access_granter(
                Config.parse(
                    {
                        "frames": {
                            "service_access": {
                                "grant_on_acceptance": ["/nope"],
                                "keycloak": {
                                    "issuer_url": "https://keycloak.example/realms/hub",
                                    "admin_api_base_url": "https://keycloak.example/admin/realms/hub",
                                    "client_id": "collab-invitation-provisioning",
                                    "client_secret": "secret",
                                },
                            }
                        }
                    }
                )
            )
    finally:
        module.KeycloakServiceAccessGranter.resolve_groups = real_resolve
        module.KeycloakServiceAccessGranter.close = real_close

    assert closed == [True], "the granter's connection pool must be closed before the raise"


def test_close_is_part_of_what_the_app_can_call_on_a_granter():
    """`_close_quietly` in core.py checks for the attribute rather than assuming
    it, because the disabled granter owns no connections. This pins that the
    real one does have it, so teardown actually closes something."""

    from collab_hub_api.frames.account_provisioning import DisabledServiceAccessGranter

    assert callable(getattr(KeycloakServiceAccessGranter, "close", None))
    assert not hasattr(DisabledServiceAccessGranter, "close")


# ---------------------------------------------------------------------------
# Configured group ids: the path a write-only credential can actually take
# ---------------------------------------------------------------------------


def test_a_configured_id_is_used_and_nothing_is_looked_up():
    """The measured reason this option exists.

    On collab-hub the membership credential is refused every read -- `GET
    /groups` included -- so a startup that resolved paths could not start at
    all. A configured id has to mean *no lookup*, not a lookup with a fallback,
    and the assertion that matters is the absence of the request.
    """

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        if request.method == "GET":
            # Stands in for the 403 the real credential returns on every read.
            # If resolution ever reaches here, the failure has production's
            # shape rather than a test-only one.
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(204)

    granter = _granter(handler, group_ids={"/llm": "group-llm-id"})
    try:
        assert granter.resolve_groups() == {"/llm": "group-llm-id"}
        assert requests == [], "startup must not call the identity provider at all"

        granter.grant(user_id="kc-user-1", group_path="/llm")
        assert requests == [
            f"POST /realms/hub{TOKEN_PATH}",
            "PUT /admin/realms/hub/users/kc-user-1/groups/group-llm-id",
        ], "one token fetch and one membership write -- no read at any point"
    finally:
        granter.close()


def test_a_write_only_credential_can_start_but_could_not_have_resolved():
    """Both halves in one test, because the pair is the point.

    The same handler refuses every read. With the id configured the granter
    starts and grants; without it, startup raises -- which is exactly what
    happened when this was deployed against the real realm.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        if request.method == "GET":
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(204)

    configured = _granter(handler, group_ids={"/llm": "group-llm-id"})
    try:
        configured.resolve_groups()
        configured.grant(user_id="kc-user-1", group_path="/llm")
    finally:
        configured.close()

    unconfigured = _granter(handler)
    try:
        with pytest.raises(ServiceAccessError):
            unconfigured.resolve_groups()
    finally:
        unconfigured.close()


def test_an_unmapped_path_is_still_looked_up_beside_a_mapped_one():
    """A mapping is per path, not a mode: a deployment may supply some ids.

    Asserted because the alternative reading -- "any configured id disables
    lookup entirely" -- would silently leave the unmapped group unresolved and
    fail at grant time instead of startup.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token(request)
        assert request.url.params["search"] == "next", "only the unmapped path is looked up"
        return httpx.Response(200, json=[{"id": "group-next-id", "path": "/next", "subGroups": []}])

    granter = _granter(handler, group_paths=("/llm", "/next"), group_ids={"/llm": "group-llm-id"})
    try:
        assert granter.resolve_groups() == {"/llm": "group-llm-id", "/next": "group-next-id"}
    finally:
        granter.close()


def _credential(**extra):
    keycloak = {
        "issuer_url": "https://keycloak.example/realms/hub",
        "admin_api_base_url": "https://keycloak.example/admin/realms/hub",
        "client_id": "collab-invitation-provisioning",
        "client_secret": "secret",
    }
    keycloak.update(extra)
    return {"frames": {"service_access": {"grant_on_acceptance": ["/llm"], "keycloak": keycloak}}}


def test_the_builder_starts_without_touching_keycloak_when_ids_are_configured():
    """The whole point, at the level a deployment experiences it.

    No transport is injected, so any attempt to reach the identity provider
    would fail against an unreachable host -- which is what this asserts by
    completing.
    """

    from collab_hub_api.config import Config, build_service_access_granter

    granter = build_service_access_granter(
        Config.parse(_credential(group_ids={"/llm": "fcd9e0f8-d05e-4e13-ae25-97846cfade17"}))
    )
    try:
        assert granter.configured is True
    finally:
        granter.close()


def test_an_id_for_a_group_nothing_grants_refuses_to_start():
    """The harmless-looking reading of this typo is the dangerous one.

    An unused mapping entry sounds like it costs nothing -- but the reason a
    path is unmapped is usually that the *other* list spells it differently,
    and then the real path silently falls back to a lookup the credential
    cannot perform.
    """

    from collab_hub_api.config import Config, build_service_access_granter

    with pytest.raises(RuntimeError) as raised:
        build_service_access_granter(Config.parse(_credential(group_ids={"/llmm": "some-id"})))
    assert "/llmm" in str(raised.value)


def test_an_id_that_is_really_a_path_refuses_to_start():
    """Checked for emptiness and for looking like a path, and no stricter.

    Keycloak owns the id's format, so a UUID rule here would be this codebase
    inventing a contract it does not control. Pasting the path in twice is the
    mistake worth catching, and it is catchable without one.
    """

    from collab_hub_api.config import Config, build_service_access_granter

    for bad in ("/llm", "   ", ""):
        with pytest.raises(RuntimeError) as raised:
            build_service_access_granter(Config.parse(_credential(group_ids={"/llm": bad})))
        assert "/llm" in str(raised.value)
