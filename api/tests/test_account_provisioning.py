"""The provisioning seam and the exact-email lookup (issue #172).

Two things get proven here, and neither needs a database or a Keycloak.

**The read lane can classify a retry.** `find_provisioned_account` asks for one
exact address and returns the correlation markers. The existing
`search_users` cannot do that job — it passes Keycloak's infix `search`
parameter and drops attributes — so a fuzzy hit without markers could not tell
our half-finished account from a stranger's, which is the ambiguity #172's
review named as blocking.

**The write lane refuses by default.** A deployment with no provisioning
credential gets `DisabledAccountProvisioner`, and that is the correct
behaviour rather than a placeholder: it is what "this deployment does not
pre-create accounts" looks like.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from collab_hub_api.frames.account_provisioning import (
    AccountProvisioningUnavailableError,
    DisabledAccountProvisioner,
    DisabledServiceAccessGranter,
    InvitationAccountProvisioner,
    ProvisionedAccount,
    ServiceAccessError,
    ServiceAccessGranter,
)
from collab_hub_api.frames.auth import DisplayIdentity
from collab_hub_api.frames.invitations import InvitationAcceptance
from collab_hub_api.routers.invitations import grant_service_access, groups_to_grant
from collab_hub_api.user_directory import (
    PROVISIONED_FOR_ATTRIBUTE,
    PROVISIONING_COMPLETE_ATTRIBUTE,
    KeycloakUserDirectoryClient,
    ProvisionedAccountRecord,
    UserDirectoryUnavailableError,
)

TOKEN_PATH = "/protocol/openid-connect/token"


def _client(handler) -> KeycloakUserDirectoryClient:
    return KeycloakUserDirectoryClient(
        token_url="https://keycloak.example/realms/hub/protocol/openid-connect/token",
        admin_api_base_url="https://keycloak.example/admin/realms/hub",
        client_id="nexus-user-directory",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )


def _token_response(request: httpx.Request) -> httpx.Response:
    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["client_credentials"]
    return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})


# ===========================================================================
# The exact-email lookup
# ===========================================================================


def test_the_lookup_asks_for_one_exact_folded_address_not_a_search():
    """`exact=true` with `email` is the whole point: it names this address or
    nothing. A fuzzy match is not evidence of identity.

    And the address is **folded** before the query (#179's review, minor 3).
    Measured afterwards on Keycloak 26.5: `exact=true` on `email` matches
    case-insensitively, so folding was not required to make a differently-cased
    retry find its account. It is pinned here anyway, because the reason to fold
    does not depend on that -- this is the form Gate B matches and the
    provisioning claim keys on (#157), and one address should have one spelling
    across every comparison in the invitation path rather than borrowing the
    provider's collation.
    """

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token_response(request)
        seen.append(request)
        return httpx.Response(200, json=[])

    client = _client(handler)
    assert client.find_provisioned_account("Alice@Example.com") is None
    client.close()

    assert len(seen) == 1
    params = seen[0].url.params
    assert params["email"] == "alice@example.com", "the address is folded before the query"
    assert params["exact"] == "true"
    assert "search" not in params, "an infix search cannot classify a retry"


def test_the_markers_are_read_off_the_account():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token_response(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "kc-user-9",
                    "email": "invitee@example.com",
                    "emailVerified": True,
                    "attributes": {
                        PROVISIONED_FOR_ATTRIBUTE: ["inv-42"],
                        PROVISIONING_COMPLETE_ATTRIBUTE: ["2026-08-21T00:00:00Z"],
                    },
                }
            ],
        )

    client = _client(handler)
    record = client.find_provisioned_account("invitee@example.com")
    client.close()

    assert record == ProvisionedAccountRecord(
        id="kc-user-9",
        email="invitee@example.com",
        email_verified=True,
        provisioned_for="inv-42",
        provisioning_complete="2026-08-21T00:00:00Z",
    )
    assert record.provisioning_complete, "complete means: classify as existing, attempt no write"


def test_an_account_without_our_marker_is_a_stranger():
    """The load-bearing None. It means *do not write to this account* — it
    belongs to somebody who arrived another way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token_response(request)
        return httpx.Response(
            200,
            json=[{"id": "kc-user-1", "email": "already@example.com", "emailVerified": True}],
        )

    client = _client(handler)
    record = client.find_provisioned_account("already@example.com")
    client.close()

    assert record is not None
    assert record.provisioned_for is None
    assert record.provisioning_complete is None


def test_an_attribute_present_but_empty_reads_as_absent():
    """Keycloak returns attributes as lists, and an empty one is not a value.
    Every decision made from these markers treats both the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token_response(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "kc-user-2",
                    "email": "empty@example.com",
                    "attributes": {PROVISIONED_FOR_ATTRIBUTE: [""], PROVISIONING_COMPLETE_ATTRIBUTE: []},
                }
            ],
        )

    client = _client(handler)
    record = client.find_provisioned_account("empty@example.com")
    client.close()

    assert record.provisioned_for is None
    assert record.provisioning_complete is None


def test_two_accounts_for_one_exact_address_refuses_rather_than_choosing():
    """A realm holding two accounts for one address is a state this code will
    not guess about: writing to the wrong one of a pair is worse than failing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(TOKEN_PATH):
            return _token_response(request)
        return httpx.Response(
            200,
            json=[
                {"id": "kc-a", "email": "twin@example.com"},
                {"id": "kc-b", "email": "twin@example.com"},
            ],
        )

    client = _client(handler)
    with pytest.raises(UserDirectoryUnavailableError, match="refusing to choose"):
        client.find_provisioned_account("twin@example.com")
    client.close()


def test_the_lookup_carries_no_name_group_or_role():
    """Each field this model does not carry is one a future caller cannot come
    to depend on. Reconciliation is not a directory browser."""

    fields = set(ProvisionedAccountRecord.model_fields)
    assert fields == {"id", "email", "email_verified", "provisioned_for", "provisioning_complete"}


# ===========================================================================
# The write seam
# ===========================================================================


def test_a_deployment_with_no_credential_refuses_every_write():
    provisioner = DisabledAccountProvisioner()
    assert provisioner.configured is False

    with pytest.raises(AccountProvisioningUnavailableError, match="no provisioning credential"):
        provisioner.create_account(email="someone@example.com", invitation_id="inv-1")
    with pytest.raises(AccountProvisioningUnavailableError):
        provisioner.send_setup(user_id="kc-user-1")
    with pytest.raises(AccountProvisioningUnavailableError):
        provisioner.mark_complete(user_id="kc-user-1")


def test_the_result_distinguishes_created_from_adopted():
    """A retry that finds its own marked account has succeeded. Recording which
    happened keeps a recovered run from being invisible in the logs."""

    assert ProvisionedAccount(user_id="kc-1").already_existed is False
    assert ProvisionedAccount(user_id="kc-1", already_existed=True).already_existed is True


def test_the_read_client_cannot_write_and_the_write_seam_cannot_read():
    """The two lanes cannot do each other's job, which is the mechanism rather
    than a convention.

    Checked over the classes' own surfaces: the directory client exposes no
    create or update, and the provisioner exposes no read. The credentials
    enforce this at runtime too — the read credential is refused every write,
    and the write credential cannot read users at all (both measured on #172)
    — but a shape that cannot express the wrong call is cheaper than a
    permission that refuses it.
    """

    read_surface = {name for name in dir(KeycloakUserDirectoryClient) if not name.startswith("_")}
    assert not {"create_account", "send_setup", "mark_complete"} & read_surface

    write_surface = {name for name in dir(DisabledAccountProvisioner) if not name.startswith("_")}
    assert not {"search_users", "search_groups", "find_provisioned_account"} & write_surface


# ===========================================================================
# Granting service access at acceptance (#180)
# ===========================================================================


class RecordingGranter:
    """A membership seam that records what it was asked to grant."""

    configured = True

    def __init__(self, fail: bool = False) -> None:
        self.grants: list[tuple[str, str]] = []
        self.fail = fail

    def grant(self, *, user_id: str, group_path: str) -> None:
        if self.fail:
            raise ServiceAccessError("the provider refused")
        self.grants.append((user_id, group_path))


def _acceptance(*, replay: bool = False) -> InvitationAcceptance:
    return InvitationAcceptance(
        invitation_id="inv-180",
        org_id="org-1",
        role="member",
        org_created=False,
        replay=replay,
    )


class RecordingService:
    """The recording half of the service, as the grant path sees it.

    Two writes, kept separate here because they have different jobs and
    different consequences when they fail: `settled` is the durable state a
    reconciler reads, `rows` is the audit trail a person reads.
    """

    def __init__(self, fail: bool = False, fail_settle: bool = False) -> None:
        self.rows: list[dict] = []
        self.settled: list[tuple[str, str, bool]] = []
        self.fail = fail
        self.fail_settle = fail_settle

    def settle_service_access_grant(self, *, user_id, group_path, granted) -> None:
        if self.fail_settle:
            raise RuntimeError("the database is having a moment")
        self.settled.append((user_id, group_path, granted))

    def record_service_access_grant(self, user_id, display, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("the database is having a moment")
        self.rows.append({"user_id": user_id, "display": display, **kwargs})


DISPLAY = DisplayIdentity(email="invitee@example.test", name="Invitee", email_verified=True)


def test_configured_groups_are_granted_to_someone_who_accepted():
    granter = RecordingGranter()
    grant_service_access(
        granter,
        ["/llm", "/services/next"],
        user_id="kc-user-1",
        acceptance=_acceptance(),
        record=RecordingService(),
        display=DISPLAY,
    )
    assert granter.grants == [("kc-user-1", "/llm"), ("kc-user-1", "/services/next")]


def test_no_configured_groups_grants_nothing():
    """The default, and the only safe one: the behaviour this replaced granted
    at account creation and so reached anyone who self-registered."""

    granter = RecordingGranter()
    grant_service_access(
        granter, [], user_id="kc-user-1", acceptance=_acceptance(), record=RecordingService(), display=DISPLAY
    )
    assert granter.grants == []


def test_an_unconfigured_granter_grants_nothing_and_does_not_raise():
    """A deployment without membership authority is a supported state, not an
    error mid-acceptance."""

    grant_service_access(
        DisabledServiceAccessGranter(),
        ["/llm"],
        user_id="kc-user-1",
        acceptance=_acceptance(),
        record=RecordingService(),
        display=DISPLAY,
    )
    grant_service_access(
        None, ["/llm"], user_id="kc-user-1", acceptance=_acceptance(), record=RecordingService(), display=DISPLAY
    )


def test_a_replayed_acceptance_grants_nothing():
    """A reloaded acceptance page created nothing, so it grants nothing --
    otherwise a page refresh becomes repeated writes to the identity provider."""

    granter = RecordingGranter()
    grant_service_access(
        granter,
        ["/llm"],
        user_id="kc-user-1",
        acceptance=_acceptance(replay=True),
        record=RecordingService(),
        display=DISPLAY,
    )
    assert granter.grants == []


def test_a_failed_grant_does_not_fail_the_acceptance():
    """The acceptance has committed and is correct: the invitee held a valid
    invitation and a verified address. Losing their membership because a group
    call failed would be the worse trade."""

    granter = RecordingGranter(fail=True)
    grant_service_access(
        granter, ["/llm"], user_id="kc-user-1", acceptance=_acceptance(), record=RecordingService(), display=DISPLAY
    )


def test_a_failed_grant_is_logged_with_the_invitation_not_the_address(caplog):
    """Recoverable, and findable. The address belongs in the audit row, not in
    a log line -- the same rule the rest of the invitation path follows."""

    import logging

    granter = RecordingGranter(fail=True)
    with caplog.at_level(logging.ERROR):
        grant_service_access(
            granter,
            ["/llm"],
            user_id="kc-user-1",
            acceptance=_acceptance(),
            record=RecordingService(),
            display=DISPLAY,
        )

    records = [r for r in caplog.records if "service_access_grant_failed" in r.getMessage()]
    assert len(records) == 1
    assert records[0].__dict__["invitation_id"] == "inv-180"
    assert records[0].__dict__["group_path"] == "/llm"
    assert "@" not in repr(records[0].__dict__), "no address in a log line"


# ---------------------------------------------------------------------------
# The durable record of every attempt (#180)
# ---------------------------------------------------------------------------


def test_a_successful_grant_is_recorded_as_granted():
    granter, service = RecordingGranter(), RecordingService()
    grant_service_access(
        granter, ["/llm"], user_id="kc-user-1", acceptance=_acceptance(), record=service, display=DISPLAY
    )
    assert [(row["group_path"], row["granted"]) for row in service.rows] == [("/llm", True)]
    assert service.rows[0]["invitation_id"] == "inv-180"
    assert service.rows[0]["org_id"] == "org-1"


def test_a_failed_grant_is_recorded_as_failed():
    """The half that makes a failure reconcilable rather than only reported."""

    granter, service = RecordingGranter(fail=True), RecordingService()
    grant_service_access(
        granter, ["/llm"], user_id="kc-user-1", acceptance=_acceptance(), record=service, display=DISPLAY
    )
    assert [(row["group_path"], row["granted"]) for row in service.rows] == [("/llm", False)]


def test_every_group_is_recorded_separately_including_a_partial_failure():
    """One row per attempt, not one per acceptance: a two-group acceptance
    where the second call fails must leave the first grant recorded as held."""

    class SecondFails(RecordingGranter):
        def grant(self, *, user_id: str, group_path: str) -> None:
            if group_path == "/services/next":
                raise ServiceAccessError("no")
            self.grants.append((user_id, group_path))

    service = RecordingService()
    grant_service_access(
        SecondFails(),
        ["/llm", "/services/next"],
        user_id="kc-user-1",
        acceptance=_acceptance(),
        record=service,
        display=DISPLAY,
    )
    assert [(row["group_path"], row["granted"]) for row in service.rows] == [
        ("/llm", True),
        ("/services/next", False),
    ]


def test_none_of_the_skip_paths_writes_a_row():
    """Nothing was attempted, so there is nothing to record. A row per
    non-event would bury real attempts under page reloads."""

    for granter, groups, acceptance in (
        (RecordingGranter(), [], _acceptance()),
        (RecordingGranter(), ["/llm"], _acceptance(replay=True)),
        (DisabledServiceAccessGranter(), ["/llm"], _acceptance()),
        (None, ["/llm"], _acceptance()),
    ):
        service = RecordingService()
        grant_service_access(
            granter, groups, user_id="kc-user-1", acceptance=acceptance, record=service, display=DISPLAY
        )
        assert service.rows == []


def test_a_recording_failure_does_not_undo_or_fail_the_grant(caplog):
    """Best-effort, and asymmetric on purpose: by this point the acceptance has
    committed and the group call has been made. A database blip must not turn a
    completed acceptance into a 500."""

    import logging

    granter, service = RecordingGranter(), RecordingService(fail=True)
    with caplog.at_level(logging.ERROR):
        grant_service_access(
            granter, ["/llm"], user_id="kc-user-1", acceptance=_acceptance(), record=service, display=DISPLAY
        )

    assert granter.grants == [("kc-user-1", "/llm")], "the grant stands"
    assert [r for r in caplog.records if "service_access_grant_unrecorded" in r.getMessage()]


def test_the_durable_row_is_settled_for_every_attempt():
    """The state a reconciler reads, per group, in either outcome."""

    class SecondFails(RecordingGranter):
        def grant(self, *, user_id: str, group_path: str) -> None:
            if group_path == "/services/next":
                raise ServiceAccessError("no")
            self.grants.append((user_id, group_path))

    service = RecordingService()
    grant_service_access(
        SecondFails(),
        ["/llm", "/services/next"],
        user_id="kc-user-1",
        acceptance=_acceptance(),
        record=service,
        display=DISPLAY,
    )
    assert service.settled == [("kc-user-1", "/llm", True), ("kc-user-1", "/services/next", False)]


def test_a_settle_failure_leaves_the_row_pending_rather_than_failing_the_acceptance(caplog):
    """The failure mode is a redundant retry, not a person with no access.

    This is the property the audit-only version could not offer: there, a lost
    write lost the only record that anything had been attempted. Here the row
    stays `pending` -- indistinguishable from a process that died earlier, and
    handled the same way.
    """

    import logging

    granter, service = RecordingGranter(), RecordingService(fail_settle=True)
    with caplog.at_level(logging.ERROR):
        grant_service_access(
            granter, ["/llm"], user_id="kc-user-1", acceptance=_acceptance(), record=service, display=DISPLAY
        )

    assert granter.grants == [("kc-user-1", "/llm")], "the grant stands"
    assert [r for r in caplog.records if "service_access_grant_unsettled" in r.getMessage()]
    # The audit trail is still written: one write failing must not suppress the
    # other, since they are independent records of the same event.
    assert [(row["group_path"], row["granted"]) for row in service.rows] == [("/llm", True)]


def test_the_owed_groups_are_decided_before_the_transaction_not_inside_it():
    """`groups_to_grant` is the single place the deployment conditions live.

    It has to be evaluated before the acceptance opens its transaction, because
    that transaction writes what is owed -- and promising a grant a deployment
    cannot attempt would put a permanent entry on the outstanding list for
    every invitee.
    """

    assert groups_to_grant(RecordingGranter(), ["/llm"]) == ("/llm",)
    assert groups_to_grant(RecordingGranter(), []) == ()
    assert groups_to_grant(None, ["/llm"]) == ()
    assert groups_to_grant(DisabledServiceAccessGranter(), ["/llm"]) == ()


def test_the_two_places_that_skip_a_grant_cannot_disagree():
    """Whatever `groups_to_grant` returns empty for, the grant path also skips.

    Two independently-written conditions would eventually drift, and the
    drifted state is the bad one: rows saying a grant is owed that nothing will
    ever attempt.
    """

    for granter in (None, DisabledServiceAccessGranter(), RecordingGranter()):
        for groups in ([], ["/llm"]):
            owed = groups_to_grant(granter, groups)
            service = RecordingService()
            grant_service_access(
                granter, owed, user_id="kc-user-1", acceptance=_acceptance(), record=service, display=DISPLAY
            )
            if not owed:
                assert service.settled == [] and service.rows == []
            else:
                assert service.settled and service.rows


def test_the_grant_path_is_the_only_caller_of_the_granter():
    """The structural form of "self-registration grants nothing, ever".

    The property whose absence caused an internal issue is
    not "the accept route checks something" -- it is that no other code path
    can reach a group write at all. Asserted against the source tree, because
    a behavioural test can only cover the paths someone thought to write.

    Since #365 there is exactly one other caller, deliberately:
    ``frames/self_service.py`` grants the same groups to a verified login the
    ``self_service`` org source has just given an organization, on a hub where
    sign-up is open because usage is paid for. It is reachable only under that
    org source, which is the operator's explicit choice. The list stays closed,
    so a third path still fails here.
    """

    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "collab_hub_api"
    callers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if ".grant(" in path.read_text() and path.name != "keycloak_service_access.py"
    )
    assert callers == ["frames/self_service.py", "routers/invitations.py"], callers


def test_the_granter_and_the_provisioner_are_separate_seams():
    """The disjointness rule from #172, expressed in the source.

    Creating an account needs `Groups/manage-members`, which over a member also
    permits password reset and deletion. Adding to a group needs
    `manage-membership`, which permits neither. Keeping them as separate
    protocols means a caller that wants to grant membership cannot reach a
    method that can create or delete.
    """

    granter_surface = {n for n in dir(DisabledServiceAccessGranter) if not n.startswith("_")}
    provisioner_surface = {n for n in dir(DisabledAccountProvisioner) if not n.startswith("_")}
    assert "grant" in granter_surface
    assert not {"create_account", "send_setup", "mark_complete"} & granter_surface
    assert "grant" not in provisioner_surface


def test_configured_is_part_of_the_provisioner_contract():
    """#179's review, minor 1: documented but not declared, so an
    implementation could omit it and leave every caller an AttributeError."""

    assert "configured" in InvitationAccountProvisioner.__annotations__
    assert "configured" in ServiceAccessGranter.__annotations__
