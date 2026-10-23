"""The invitation service (issue #89): create, list, revoke, body-based accept.

Coverage maps onto the R3/R11 acceptance list, and the live-Postgres tests are
where the load-bearing claims are actually proven rather than inspected:

1. **Concurrent acceptance of one token yields one membership and one org.**
   Proven twice over — once with a deterministic, forced interleaving (a held
   ``FOR UPDATE`` lock, and a held conflicting membership insert, so the exact
   contended code path runs on demand rather than when the scheduler feels
   like it), and once with real threads racing through the whole service.
2. **Every terminal state is explicit**: expired, revoked, replayed,
   unverified, mismatched, already-in-an-organization — each with its own
   registered wire code, and none of them consuming the token.
3. **Exact-match email** (Gate B, 2026-08-03), including what happens when
   the verified claim changes between issuance and acceptance, in both
   directions.
4. **The two authority axes** produce identical ``invitation.send`` rows
   differing only in the actor, and an owner cannot name another
   organization.
5. **Atomicity**: a failed issuance leaves neither invitation nor event row,
   and a failed acceptance leaves neither organization nor membership nor
   event row — and the token still pending.
6. **The raw token never appears** in a response body, a log record, or an
   audit row, and a near-miss token is not echoed back by a 422.

Live-Postgres tests opt in with ``COLLAB_HUB_TEST_POSTGRES_URL``, exactly as
``test_operator_foundation.py``; everything else runs everywhere.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import inspect
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

psycopg = pytest.importorskip("psycopg")

from collab_hub_api.config import Config  # noqa: E402
from collab_hub_api.core import make_app  # noqa: E402
from collab_hub_api.frames import error_codes  # noqa: E402
from collab_hub_api.frames import invitations as invitations_module  # noqa: E402
from collab_hub_api.frames.audit import AUDIT_ACTIONS  # noqa: E402
from collab_hub_api.frames.auth import (  # noqa: E402
    WORKSPACE_DEFAULT,
    AuthContext,
    CallerIdentity,
    DisplayIdentity,
    display_identity_from_claims,
)
from collab_hub_api.frames.authorization import verify_protected_routes  # noqa: E402
from collab_hub_api.frames.collab_schema import (  # noqa: E402
    COLLAB_SCHEMA_MIGRATIONS,
    LATEST_COLLAB_SCHEMA_VERSION,
    NEUTRAL_ORG_NAME,
    run_collab_schema_migrations,
)
from collab_hub_api.frames.credentials import InvitationSecret  # noqa: E402
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV  # noqa: E402
from collab_hub_api.frames.invitation_email import (  # noqa: E402
    DELIVERY_PROVIDER_ACCEPTED,
    DeliveryOutcome,
    InvitationEmailMessage,
    render_invitation_email,
)
from collab_hub_api.frames.invitations import (  # noqa: E402
    INVITATION_STORED_STATUSES,
    INVITATION_TTL,
    STATUS_ACCEPTED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REVOKED,
    AlreadyInOrganizationError,
    EmailNotVerifiedError,
    Invitation,
    InvitationAlreadyUsedError,
    InvitationEmailMismatchError,
    InvitationExpiredError,
    InvitationNotFoundError,
    InvitationRevokedError,
    InvitationsUnavailableError,
    OrganizationAlreadyNamedError,
    PostgresInvitationService,
    UnavailableInvitationService,
    effective_status,
    emails_match,
    hash_invitation_secret,
    is_placeholder_organization_name,
    mint_invitation_secret,
    validate_invited_email,
    validate_organization_name,
    verified_claim_email,
)
from collab_hub_api.frames.org_source import (  # noqa: E402
    DEFAULT_ORG_ENV,
    DEFAULT_WORKSPACE_ENV,
    ORG_SOURCE_ENV,
)
from collab_hub_api.frames.orgs import (  # noqa: E402
    MEMBERSHIP_REMOVED,
    PLATFORM_ROLE_OPERATOR,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from collab_hub_api.routers import invitations as invitations_router  # noqa: E402

OPERATOR = "0perator-1111-4111-8111-abcdefabcdef"
OWNER = "owner000-2222-4222-8222-abcdefabcdef"
MEMBER = "member00-3333-4333-8333-abcdefabcdef"
INVITEE = "invitee0-4444-4444-8444-abcdefabcdef"
OTHER_INVITEE = "other000-5555-4555-8555-abcdefabcdef"

ORG = "org-aaaa"
OTHER_ORG = "org-bbbb"

INVITED_EMAIL = "Invitee@Example.com"
"""Deliberately mixed case: issuance lowers it (#157), so this fixture is what
an operator *types* and :data:`STORED_EMAIL` is what the system holds."""

STORED_EMAIL = INVITED_EMAIL.lower()
"""The address as stored, emailed, audited and compared, after the ASCII fold
the Gate B amendment (#157) applies at issuance. Keycloak asserts this
spelling whatever was typed, which is the whole reason the amendment exists."""


def display(email: str | None = None, *, verified: bool = True, name: str | None = None) -> DisplayIdentity:
    return DisplayIdentity(name=name, email=email, email_verified=verified)


def ctx(user: str, *, org_id: str | None = ORG, org_role: str | None = None, platform_role: str | None = None):
    return AuthContext(
        user=user,
        home_org_id=org_id,
        workspace_id=WORKSPACE_DEFAULT,
        display=display(f"{user[:6]}@example.com"),
        org_role=org_role,
        platform_role=platform_role,
    )


OPERATOR_CTX = ctx(OPERATOR, org_id=None, platform_role=PLATFORM_ROLE_OPERATOR)
OWNER_CTX = ctx(OWNER, org_id=ORG, org_role=ROLE_OWNER)


# ===========================================================================
# Tokens: minting, hashing, and the properties the design rests on
# ===========================================================================


def test_minted_secrets_are_unique_urlsafe_and_never_equal_their_hash():
    secrets_seen = set()
    for _ in range(64):
        minted = mint_invitation_secret()
        raw = minted.raw.reveal()
        assert isinstance(minted.raw, InvitationSecret)  # never a bare str
        assert raw not in secrets_seen
        secrets_seen.add(raw)
        assert set(raw) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert len(raw) >= 40
        assert minted.token_hash == hash_invitation_secret(raw)
        assert minted.token_hash != raw
        assert len(minted.token_hash) == 64


@pytest.mark.parametrize(
    "value",
    ["", "not-a-real-token", "ünïcödé", "\ud800", "x" * 10_000, "tab\there", "\x00"],
)
def test_hashing_is_defined_over_every_str(value):
    """A garbage accept body must miss, never raise.

    An encode error here would answer 500 for input that a near-miss token
    answers 404 for — a distinction worth nothing to a legitimate invitee and
    everything to someone probing the endpoint.
    """

    digest = hash_invitation_secret(value)
    assert len(digest) == 64
    assert digest == hash_invitation_secret(value)


def test_the_invitation_resource_has_no_field_that_could_carry_a_token():
    """S4: the desktop settings UI builds on these responses.

    Asserted against the model rather than one response, so a field added
    later has to be added here too.
    """

    assert set(invitations_router.InvitationResource.model_fields) == {
        "id",
        "org_id",
        "email",
        "status",
        "created_at",
        "expires_at",
    }
    assert set(invitations_router.InvitationCreateResponse.model_fields) == {
        "id",
        "org_id",
        "email",
        "status",
        "created_at",
        "expires_at",
        "delivery_status",
        "delivery_error_code",
    }
    assert set(invitations_router.InvitationAcceptResponse.model_fields) == {
        "org_id",
        "role",
        "org_created",
    }


def _invitation(status: str = STATUS_PENDING, *, expires_in: timedelta = timedelta(days=7), **kwargs) -> Invitation:
    # An arbitrary fixture expiry, deliberately NOT ``INVITATION_TTL``: the
    # state machine is TTL-agnostic, and the tests built on this helper must
    # not move when the default does. The default's own pin is
    # ``test_the_default_lifetime_is_forty_eight_hours``.
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    defaults = dict(
        id="inv-1",
        org_id=None,
        email=INVITED_EMAIL,
        status=status,
        created_at=now,
        created_by=OPERATOR,
        expires_at=now + expires_in,
    )
    defaults.update(kwargs)
    return Invitation(**defaults)


SENTINEL_SECRET = "S3cr3tTokenValueThatMustNeverBePrinted"
"""Deliberately matches the accept token pattern so it is a *valid* token —
a redaction that only worked for values validation rejects would prove
nothing."""


def _carriers_of_the_secret() -> dict[str, object]:
    """Every object in the system that holds a live invitation secret.

    Enumerated once so every probe below runs against the same list, and so
    a carrier added without protection is a failing test rather than a silent
    regression — see the type-keyed detector further down, which is what
    keeps this list honest.
    """

    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    return {
        "MintedSecret": invitations_module.MintedSecret(
            raw=InvitationSecret(SENTINEL_SECRET), token_hash=hash_invitation_secret(SENTINEL_SECRET)
        ),
        "IssuedInvitation": invitations_module.IssuedInvitation(
            invitation=_invitation(), raw_secret=InvitationSecret(SENTINEL_SECRET)
        ),
        "InvitationAcceptRequest": invitations_router.InvitationAcceptRequest(token=SENTINEL_SECRET),
        # The already-merged SES adapter's carrier, included because this
        # module hands it the secret and inherits its exposure.
        "InvitationEmailMessage": InvitationEmailMessage(
            recipient=INVITED_EMAIL, subject="s", text_body=InvitationSecret(SENTINEL_SECRET)
        ),
        "render_invitation_email": render_invitation_email(
            recipient=INVITED_EMAIL,
            setup_url=f"https://example.com/invite#token={SENTINEL_SECRET}",
            organization_name=None,
            expires_at=now + timedelta(days=7),
            # Required since #93: the sent copy tells the invitee how to get the
            # app, and the renderer refuses to leave a placeholder in a message
            # nobody will proof-read.
            app_instructions="Download from https://example.test/download",
            require_verified_email=True,
        ),
        # The wrapper itself: everything above inherits its behaviour, so it
        # is the one that actually has to hold.
        "InvitationSecret": InvitationSecret(SENTINEL_SECRET),
    }


@pytest.mark.parametrize("name", sorted(_carriers_of_the_secret()))
def test_no_type_that_carries_the_secret_will_print_it(name):
    """Every rendering path a log line or an error reporter might take.

    ``repr`` is the obvious one but not the only one: ``str`` and f-string
    interpolation go through ``__format__``/``__str__``, and a type that only
    suppressed ``repr`` would still print in an f-string if it defined
    ``__str__``. All four are checked for every carrier.
    """

    carrier = _carriers_of_the_secret()[name]
    assert SENTINEL_SECRET not in repr(carrier)
    assert SENTINEL_SECRET not in str(carrier)
    assert SENTINEL_SECRET not in f"{carrier}"
    assert SENTINEL_SECRET not in format(carrier)


@pytest.mark.parametrize("name", sorted(_carriers_of_the_secret()))
def test_no_route_that_reads_the_field_value_can_reach_the_secret(name):
    """Every probe the review gate used, run against every carrier.

    The lesson of rounds two and three: suppressing ``__repr__`` and patching
    ``model_dump`` and ``__getstate__`` closes the exits someone thought of.
    These are the ones that read the *field value* instead, which is why the
    fix had to be the value's type rather than another exit:

    - ``dataclasses.asdict`` / ``astuple`` — walk the fields and deep-copy;
    - ``vars`` / ``__dict__`` / ``json.dumps(..., default=vars)`` — flatten
      the instance;
    - ``copy`` / ``deepcopy`` — duplicate the credential;
    - pickle at protocols 2-5 — the default ``__reduce_ex__``;
    - **FastAPI's own response encoder**, which is the one that mattered: it
      walks dataclass fields, and a probe endpoint returning a carrier
      answered 200 with the live secret in the body.

    A carrier may refuse or redact; both are fine, leaking is not, so the
    assertion is on the outcome rather than on the mechanism.
    """

    import copy
    import pickle

    from fastapi.encoders import jsonable_encoder

    carrier = _carriers_of_the_secret()[name]

    def rendered(value) -> str:
        if isinstance(value, bytes):
            return value.decode("latin-1")
        return value if isinstance(value, str) else repr(value)

    probes = {
        "asdict": lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else None,
        "astuple": lambda o: dataclasses.astuple(o) if dataclasses.is_dataclass(o) else None,
        "vars": vars,
        "__dict__": lambda o: o.__dict__,
        "json.dumps(default=vars)": lambda o: json.dumps(o, default=vars),
        "jsonable_encoder": jsonable_encoder,
        "copy": copy.copy,
        "deepcopy": copy.deepcopy,
    }
    for protocol in (2, 3, 4, 5):
        probes[f"pickle-p{protocol}"] = lambda o, p=protocol: pickle.dumps(o, protocol=p)

    for probe_name, probe in probes.items():
        try:
            outcome = rendered(probe(carrier))
        except Exception as exc:  # noqa: BLE001 - a refusal is a valid outcome
            outcome = f"{type(exc).__name__}: {exc}"
        assert SENTINEL_SECRET not in outcome, f"{name} leaked the secret via {probe_name}"


async def test_a_response_returning_a_carrier_cannot_put_the_secret_on_the_wire(tmp_path, monkeypatch):
    """The decisive probe, as an actual HTTP response.

    FastAPI's encoder walks dataclass fields, so before the wrapper a probe
    endpoint returning any carrier answered 200 with a live credential in the
    body. This mounts exactly that endpoint for each carrier and asserts the
    secret is not in the response — whatever the status code turns out to be.
    """

    from fastapi import FastAPI

    probe = FastAPI()
    carriers = _carriers_of_the_secret()
    for index, name in enumerate(sorted(carriers)):
        probe.add_api_route(
            f"/probe/{index}",
            (lambda captured=carriers[name]: (lambda: captured))(),
            methods=["GET"],
        )

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        for index, name in enumerate(sorted(carriers)):
            response = await client.get(f"/probe/{index}")
            assert SENTINEL_SECRET not in response.text, (
                f"{name} was serialized into a {response.status_code} response body"
            )


@pytest.mark.parametrize("name", sorted(_carriers_of_the_secret()))
def test_no_type_that_carries_the_secret_will_serialize_it(name):
    """The other half, and the one suppressing ``repr`` does nothing about.

    ``repr=False`` governs *rendering*. Serialization ignores it completely:

    - pydantic's ``model_dump``/``model_dump_json`` read the fields, not the
      repr, and dumping a request model is the single most routine thing
      anyone does when they add request logging. Both modes are asserted
      separately because ``model_dump_json`` does not route through
      ``model_dump``, so a serializer registered for one mode leaves the
      other wide open.
    - ``__reduce_ex__`` walks instance state directly, and pickling is how an
      object crosses into a queue, a cache, or a crash dump — where the
      credential is then at rest rather than in flight.

    A carrier may either refuse to serialize or emit a redaction; both are
    acceptable, leaking is not, so the assertion is on the outcome.
    """

    import pickle

    from pydantic import BaseModel

    carrier = _carriers_of_the_secret()[name]

    if isinstance(carrier, BaseModel):
        assert SENTINEL_SECRET not in json.dumps(carrier.model_dump())
        assert SENTINEL_SECRET not in carrier.model_dump_json()
        # And the value is still usable in-process — redaction on the way out
        # only, never a model that has lost its own field.
        assert carrier.token.reveal() == SENTINEL_SECRET

    try:
        pickled = pickle.dumps(carrier)
    except (TypeError, pickle.PicklingError):
        pass  # refused outright, which is the stronger answer
    else:
        assert SENTINEL_SECRET.encode() not in pickled
        assert SENTINEL_SECRET not in repr(pickle.loads(pickled))


@pytest.mark.parametrize("name", sorted(_carriers_of_the_secret()))
def test_a_traceback_that_captures_locals_does_not_reveal_the_secret(name):
    """The realistic worst case: an error reporter that walks frame locals.

    Ordinary tracebacks do not render locals, but Sentry-style reporters and
    ``traceback.TracebackException(capture_locals=True)`` do — and what they
    print for each local is its ``repr``. This raises with the carrier bound
    to a local and renders the traceback the way such a reporter would.
    """

    import traceback

    carrier = _carriers_of_the_secret()[name]

    def failing_frame(held):  # noqa: ARG001 - `held` exists to be captured
        raise RuntimeError("boom")

    try:
        failing_frame(carrier)
    except RuntimeError as exc:
        rendered = "".join(
            traceback.TracebackException.from_exception(exc, capture_locals=True).format()
        )

    assert "held" in rendered  # the local really was captured
    assert SENTINEL_SECRET not in rendered


def _credential_fields(module) -> list[tuple[type, str]]:
    """Every field in *module* whose declared type is the credential wrapper.

    Keyed on **type**, not on name. The previous version of this detector
    matched field names against a guessed vocabulary (`raw_secret`, `token`,
    …) and was therefore bypassed by any carrier whose author called the
    field `value` or `material` — which makes it a naming convention dressed
    up as a mechanism. A type is not guessable-around: a field either holds
    the wrapper or it does not.
    """

    import dataclasses

    from pydantic import BaseModel

    found: list[tuple[type, str]] = []
    for attribute in vars(module).values():
        if not isinstance(attribute, type) or attribute.__module__ != module.__name__:
            continue
        if dataclasses.is_dataclass(attribute):
            annotated = [(field.name, field.type) for field in dataclasses.fields(attribute)]
        elif issubclass(attribute, BaseModel):
            annotated = [(name, field.annotation) for name, field in attribute.model_fields.items()]
        else:
            continue
        for name, annotation in annotated:
            # `from __future__ import annotations` leaves dataclass field
            # types as strings, so compare on the spelling as well as the
            # object; both identify the type, neither identifies a name.
            if annotation is InvitationSecret or annotation == "InvitationSecret":
                found.append((attribute, name))
    return found


def _walk_package_module_names() -> list[str]:
    """The module names ``pkgutil`` finds on disk under the package.

    Separated from :func:`_all_package_modules` so a test can assert on the
    **walk's own output**. That distinction is the whole point: the previous
    "did it scan enough?" guard counted the modules in ``sys.modules``, which
    are already there from ordinary imports, so forcing the walk to return
    nothing still satisfied it. A guard that a broken walk passes is not a
    guard.

    ``__main__`` modules are skipped: importing one runs a program entry
    point, and no carrier belongs there.
    """

    import pkgutil

    import collab_hub_api

    return [
        info.name
        for info in pkgutil.walk_packages(collab_hub_api.__path__, prefix="collab_hub_api.")
        if not info.name.endswith("__main__")
    ]


def _all_package_modules():
    """Every module in the application package, discovered rather than listed.

    An earlier version named four modules, which made it exactly as good as
    somebody's memory: a carrier added in a *new* module was invisible and
    the test passed, defeating the whole purpose of catching what someone
    else adds later.

    Two sources, because neither alone is complete. The ``pkgutil`` walk
    imports modules that nothing has imported yet, so a brand-new file is
    covered the moment it exists on disk; ``sys.modules`` then supplies
    everything under the package, which also lets a test inject a module and
    have it genuinely discovered.
    """

    import importlib
    import sys

    import collab_hub_api

    for name in _walk_package_module_names():
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - a module that cannot import
            raise AssertionError(
                f"{name} could not be imported, so the credential-carrier scan cannot see it"
            ) from exc

    root = collab_hub_api.__name__
    return [
        module
        for name, module in sorted(sys.modules.items())
        if module is not None and (name == root or name.startswith(root + "."))
    ]


def _assert_every_carrier_is_covered(modules) -> None:
    """The guard's rule, extracted so its own proof can invoke it.

    Any field in *modules* whose declared type is the credential wrapper must
    belong to a type on the carrier list, where the probes actually run.
    """

    covered = {type(carrier) for carrier in _carriers_of_the_secret().values()}
    for module in modules:
        for owner, field_name in _credential_fields(module):
            assert owner in covered, (
                f"{owner.__module__}.{owner.__name__}.{field_name} holds an InvitationSecret but is "
                "not in _carriers_of_the_secret(), so no probe asserts it cannot leak"
            )


def test_every_type_holding_the_wrapper_is_on_the_carrier_list():
    """A future carrier must fail this, not ship.

    Type-keyed, so it cannot be evaded by choosing an innocuous field name;
    package-wide, so it cannot be evaded by putting the carrier in a module
    this test does not happen to name. Both evasions are proven closed by the
    two tests below.
    """

    # Assert on the WALK's output, not on the module list: `sys.modules` is
    # already full of this package from ordinary imports, so a count over it
    # is satisfied even when the walk finds nothing. Naming modules that only
    # a filesystem walk can produce is what makes "the walk ran" checkable.
    walked = set(_walk_package_module_names())
    for required in (
        "collab_hub_api.frames.credentials",
        "collab_hub_api.frames.invitations",
        "collab_hub_api.routers.invitations",
    ):
        assert required in walked, f"the package walk did not reach {required}: {sorted(walked)[:5]}"
    # And it reaches beyond the modules this test imports, which is the case
    # the walk exists for — a carrier in a file nothing here references.
    assert len(walked) > 20, sorted(walked)

    _assert_every_carrier_is_covered(_all_package_modules())


def _render_traceback_holding(obj) -> str:
    """A locals-capturing traceback whose raising frame holds only *obj*.

    Scoped deliberately: a probe that let the sentinel sit in an enclosing
    frame would report a leak from its own test code, which is how a lazy
    version of this check produces false positives and gets deleted.
    """

    import traceback

    def raising_frame(held):  # noqa: ARG001 - `held` exists to be captured
        raise RuntimeError("boom")

    try:
        raising_frame(obj)
    except RuntimeError as exc:
        return "".join(traceback.TracebackException.from_exception(exc, capture_locals=True).format())
    raise AssertionError("unreachable")  # pragma: no cover


def _accept_request_builders():
    """Every ordinary way to get an ``InvitationAcceptRequest``.

    ``model_construct`` and ``model_copy(update=...)`` **skip validation by
    design**, so the field's after-validator never runs for them. They are not
    exotic — ``model_construct`` is exactly what someone reaches for to skip
    validation on a hot path — so the invariant has to hold for them too, and
    each is boxed explicitly on the model.
    """

    model = invitations_router.InvitationAcceptRequest
    return {
        "validate": lambda: model(token=SENTINEL_SECRET),
        "model_construct": lambda: model.model_construct(token=SENTINEL_SECRET),
        "model_copy": lambda: model(token=SENTINEL_SECRET).model_copy(),
        "model_copy_update": lambda: model(token=SENTINEL_SECRET).model_copy(
            update={"token": SENTINEL_SECRET}
        ),
    }


@pytest.mark.parametrize("how", sorted(_accept_request_builders()))
def test_the_token_is_boxed_however_the_model_is_built(how):
    """The wrapper invariant must not depend on which constructor ran.

    Before this, ``model_construct`` and ``model_copy(update=...)`` left a
    bare ``str`` in the model's ``__dict__`` — invisible to ``model_dump``
    (the serializer still redacted) but printed in full by any traceback that
    captures locals. Every protection on the model assumes the field holds
    the wrapper, so the boxing has to happen on every path that can set it.
    """

    payload = _accept_request_builders()[how]()

    assert isinstance(payload.token, InvitationSecret), f"{how} left a raw value in the field"
    assert payload.token.reveal() == SENTINEL_SECRET  # still usable in-process
    assert SENTINEL_SECRET not in repr(vars(payload))
    assert SENTINEL_SECRET not in repr(payload)
    assert SENTINEL_SECRET not in json.dumps(payload.model_dump())
    assert SENTINEL_SECRET not in payload.model_dump_json()
    assert SENTINEL_SECRET not in _render_traceback_holding(payload)


# Every public entry point on ``BaseModel``, classified. The point of writing
# the whole surface down — including the methods that only read — is that the
# completeness test below compares this to the installed class, so a pydantic
# upgrade that adds a public method fails loudly instead of leaving it
# unaudited. That is the fix for the *method*, not just for the one hole it
# let through: the previous audit reasoned backwards from the ``__dict__``
# write sites it could find, and missed the deprecated ``copy()``, which
# reaches ``__dict__`` by an entirely different internal route
# (``pydantic/deprecated/copy_internals.py``) and does not go through
# ``model_copy`` at all.
BUILDS = "builds or mutates an instance"
READS = "reads an instance or the class"

REVIEWED_BASEMODEL_API = {
    "__init__": BUILDS,
    "__replace__": BUILDS,
    "__copy__": BUILDS,
    "__deepcopy__": BUILDS,
    "__setattr__": BUILDS,
    "__setstate__": BUILDS,
    "model_construct": BUILDS,
    "model_copy": BUILDS,
    "model_validate": BUILDS,
    "model_validate_json": BUILDS,
    "model_validate_strings": BUILDS,
    "construct": BUILDS,
    "copy": BUILDS,
    "parse_obj": BUILDS,
    "parse_raw": BUILDS,
    "parse_file": BUILDS,
    "validate": BUILDS,
    "from_orm": BUILDS,
    "__iter__": READS,
    "model_dump": READS,
    "model_dump_json": READS,
    "model_json_schema": READS,
    "model_parametrized_name": READS,
    "model_post_init": READS,
    "model_rebuild": READS,
    "dict": READS,
    "json": READS,
    "schema": READS,
    "schema_json": READS,
    "update_forward_refs": READS,
}


def _public_basemodel_api() -> set[str]:
    """Every public callable on the installed ``BaseModel``, plus the dunders
    that construct or mutate."""

    import inspect

    from pydantic import BaseModel

    mutating_dunders = {
        "__init__",
        "__replace__",
        "__copy__",
        "__deepcopy__",
        "__setattr__",
        "__setstate__",
        "__iter__",
    }
    found = set()
    for name in dir(BaseModel):
        if name.startswith("_") and name not in mutating_dunders:
            continue
        attribute = inspect.getattr_static(BaseModel, name, None)
        if callable(attribute) or isinstance(attribute, (classmethod, staticmethod)):
            found.add(name)
    return found


MUST_BUILD = frozenset(
    {"__init__", "__setattr__", "model_validate", "model_construct", "model_copy", "copy"}
)
"""Entry points that are constructors or mutators as a matter of fact.

Named explicitly because the classification above is data a person edits, and
a check whose rule is "the classification agrees with itself" is satisfied by
relabelling everything ``READS`` and returning no builders — which is exactly
what someone quietly weakening this would do. Pinning a floor that cannot
honestly be reclassified is what makes the rest of the check mean something.
"""


def test_the_classification_is_not_vacuous():
    """Guard the guard: the classification cannot be defined into passing.

    Relabelling every entry point as ``READS`` and emptying the call table
    used to satisfy both checks below. It no longer can: these entry points
    build or mutate instances as a matter of fact, not of labelling, and the
    table must actually cover them.
    """

    builds = {name for name, kind in REVIEWED_BASEMODEL_API.items() if kind == BUILDS}
    missing = MUST_BUILD - builds
    assert not missing, f"these build or mutate an instance and cannot be classified otherwise: {sorted(missing)}"

    exercised = set(BUILD_CALLS)
    assert exercised, "no entry point is exercised, so the audit proves nothing"
    assert MUST_BUILD <= exercised, f"not exercised: {sorted(MUST_BUILD - exercised)}"


def test_the_basemodel_surface_has_not_changed_under_us():
    """A pydantic upgrade that adds a public method must fail this test.

    The audit is only worth its conclusion if it is complete, and "complete"
    has to be checked against the installed library rather than against
    memory — which is exactly what went wrong the first time.
    """

    actual = _public_basemodel_api()
    reviewed = set(REVIEWED_BASEMODEL_API)
    assert actual == reviewed, (
        f"pydantic's public BaseModel API changed. Added: {sorted(actual - reviewed)}; "
        f"removed: {sorted(reviewed - actual)}. Classify each addition in "
        "REVIEWED_BASEMODEL_API and, if it builds or mutates, add its arguments to BUILD_CALLS."
    )


# How each BUILDS entry point is invoked, as **data**. There are deliberately
# no per-entry helper functions here: a table of callables is a table of
# things that can each be replaced by `lambda: prebuilt_model`, which is how a
# previous version of this harness passed every guard without invoking a
# single entry point. Here the name in the table *is* the call — the harness
# resolves it off the installed class with `getattr` — so the only thing an
# entry can carry is its arguments.
CLASS_CALL = "classmethod on the model class"
INSTANCE_CALL = "bound method on a fresh instance"
CONSTRUCTOR_CALL = "the class itself, i.e. __init__"

TEMP_JSON_FILE = "<a temp file holding the JSON body>"
ORM_OBJECT = "<an object with a .token attribute>"
STATE_DICT = {
    "__dict__": {"token": SENTINEL_SECRET},
    "__pydantic_fields_set__": {"token"},
    "__pydantic_extra__": None,
    "__pydantic_private__": None,
}

BUILD_CALLS: dict[str, tuple[str, tuple, dict]] = {
    "__init__": (CONSTRUCTOR_CALL, (), {"token": SENTINEL_SECRET}),
    "__replace__": (INSTANCE_CALL, (), {"token": SENTINEL_SECRET}),
    "__copy__": (INSTANCE_CALL, (), {}),
    "__deepcopy__": (INSTANCE_CALL, (), {}),
    "__setattr__": (INSTANCE_CALL, ("token", SENTINEL_SECRET), {}),
    "__setstate__": (INSTANCE_CALL, (STATE_DICT,), {}),
    "model_construct": (CLASS_CALL, (), {"token": SENTINEL_SECRET}),
    "model_copy": (INSTANCE_CALL, (), {"update": {"token": SENTINEL_SECRET}}),
    "model_validate": (CLASS_CALL, ({"token": SENTINEL_SECRET},), {}),
    "model_validate_json": (CLASS_CALL, ('{"token": "%s"}' % SENTINEL_SECRET,), {}),
    "model_validate_strings": (CLASS_CALL, ({"token": SENTINEL_SECRET},), {}),
    "construct": (CLASS_CALL, (), {"token": SENTINEL_SECRET}),
    "copy": (INSTANCE_CALL, (), {"update": {"token": SENTINEL_SECRET}}),
    "parse_obj": (CLASS_CALL, ({"token": SENTINEL_SECRET},), {}),
    "parse_raw": (CLASS_CALL, ('{"token": "%s"}' % SENTINEL_SECRET,), {}),
    "parse_file": (CLASS_CALL, (TEMP_JSON_FILE,), {}),
    "validate": (CLASS_CALL, ({"token": SENTINEL_SECRET},), {}),
    "from_orm": (CLASS_CALL, (ORM_OBJECT,), {}),
}


def _invoke_entry_point(name: str, tmp_path):
    """Call *name* on the accept model, resolved by name off the class.

    Returns ``(produced, refusal)``: the object the call produced — for the
    in-place mutators, the instance they were called on — or the exception
    that refused it. Nothing here knows anything entry-specific beyond the
    arguments in :data:`BUILD_CALLS`, which is the point: there is no seam
    where a name could be quietly wired to something other than itself.
    """

    import json as json_module
    import warnings

    model = invitations_router.InvitationAcceptRequest
    target, args, kwargs = BUILD_CALLS[name]

    resolved_args = []
    for argument in args:
        if argument is TEMP_JSON_FILE:
            path = tmp_path / f"{name}.json"
            path.write_text(json_module.dumps({"token": SENTINEL_SECRET}))
            resolved_args.append(path)
        elif argument is ORM_OBJECT:
            resolved_args.append(type("Row", (), {"token": SENTINEL_SECRET})())
        else:
            resolved_args.append(argument)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the deprecated entry points warn
        try:
            if target is CONSTRUCTOR_CALL:
                return model(*resolved_args, **kwargs), None
            if target is CLASS_CALL:
                return getattr(model, name)(*resolved_args, **kwargs), None
            instance = model(token=SENTINEL_SECRET)
            returned = getattr(instance, name)(*resolved_args, **kwargs)
            # The in-place mutators return None; what they produced is the
            # instance they were called on.
            return (instance if returned is None else returned), None
        except Exception as exc:  # noqa: BLE001 - a refusal is a valid outcome
            return None, exc


def test_every_building_entry_point_has_an_audit_case():
    """Classification and coverage cannot drift apart."""

    builds = {name for name, kind in REVIEWED_BASEMODEL_API.items() if kind == BUILDS}
    assert set(BUILD_CALLS) == builds


def test_each_entry_point_really_runs_and_produces_its_own_object(tmp_path):
    """Provenance: the entries cannot all be one prebuilt model.

    The bypass this closes: replacing every invocation with something that
    returns a single valid instance satisfied every other guard here without
    any entry point being called. Distinct object identity makes that fail on
    the second entry — and the objects are all held alive while they are
    compared, because CPython reuses the id of a collected object and a
    version of this check that let them die would pass by accident.
    """

    produced = []
    refused = {}
    for name in sorted(BUILD_CALLS):
        instance, refusal = _invoke_entry_point(name, tmp_path)
        if refusal is not None:
            refused[name] = type(refusal).__name__
        else:
            produced.append((name, instance))

    assert len(produced) >= 2, produced
    identities = {id(instance) for _name, instance in produced}
    assert len(identities) == len(produced), (
        "two entry points produced the same object, so at least one did not really run: "
        f"{[name for name, _ in produced]}"
    )
    # The refusals are the expected ones, not a way of avoiding the check.
    assert set(refused) <= {"__deepcopy__", "__setattr__", "from_orm"}, refused


@pytest.mark.parametrize("entry_point", sorted(BUILD_CALLS))
def test_the_named_entry_point_is_the_one_that_actually_runs(entry_point, tmp_path):
    """Provenance, at the strongest point available: did *that* method run?

    Distinct object identity (above) catches the harness returning one shared
    prebuilt model. It does not catch a harness that builds a *fresh* valid
    model per entry while calling none of them — the identities differ and
    every other assertion is satisfied. This closes that too: the named
    attribute is replaced by a call-through spy for the duration of the
    invocation, so if the entry point was not reached, the spy did not fire
    and the test fails.

    ``side_effect`` delegates to the real implementation, so this observes the
    call rather than replacing it, and the entry point behaves exactly as it
    does in the test above.
    """

    from unittest import mock

    model = invitations_router.InvitationAcceptRequest
    original = getattr(model, entry_point)
    with mock.patch.object(model, entry_point, autospec=True, side_effect=original) as spy:
        _invoke_entry_point(entry_point, tmp_path)

    assert spy.called, (
        f"{entry_point} was never invoked — the audit for it proves nothing about "
        f"{model.__name__}.{entry_point}"
    )


@pytest.mark.parametrize("entry_point", sorted(BUILD_CALLS))
def test_no_public_entry_point_leaves_the_token_unboxed(entry_point, tmp_path):
    """Every documented way to build or mutate this model, boxed or refused.

    Enumerated **forward from the public surface** rather than backward from
    the ``__dict__`` write sites. That direction is the whole lesson: the
    backward audit found the two write sites pydantic has and concluded the
    field was safe, while ``.copy(update=...)`` — a documented public method
    a ``DeprecationWarning`` does not stop anyone calling — reached
    ``__dict__`` by a third route and left a bare ``str`` in the model.
    """

    instance, refusal = _invoke_entry_point(entry_point, tmp_path)
    if refusal is not None:
        # A refusal is a valid outcome (deep copy, assignment on a frozen
        # model, from_orm without from_attributes). What is not valid is
        # succeeding with a bare string.
        return

    assert isinstance(instance.token, InvitationSecret), f"{entry_point} left a raw value in the field"
    assert instance.token.reveal() == SENTINEL_SECRET
    assert SENTINEL_SECRET not in repr(vars(instance))
    assert SENTINEL_SECRET not in repr(instance)
    assert SENTINEL_SECRET not in json.dumps(instance.model_dump())
    assert SENTINEL_SECRET not in instance.model_dump_json()
    assert SENTINEL_SECRET not in _render_traceback_holding(instance)


def test_a_subtype_instance_cannot_smuggle_a_raw_token_through_validation():
    """The gap an entry-point audit cannot see: what a *subtype* does.

    Auditing every public constructor proves each one boxes what it is given.
    It says nothing about a value that arrives *already inside a model* —
    pydantic's default ``revalidate_instances='never'`` returns such an
    instance untouched, so a subtype redeclaring ``token: str`` produced a
    model whose field was a bare string, and ``model_validate`` and
    ``TypeAdapter`` both handed it straight back.

    ``revalidate_instances="always"`` is the one-line close: any instance,
    subtype included, is validated again, so the boxing validator fires. It
    also means an already-boxed value gets revalidated, which is why the
    field chain unwraps before the constraints — without that, the safety
    setting would break ordinary ``model_validate`` of a valid instance.
    """

    from pydantic import TypeAdapter, create_model

    model = invitations_router.InvitationAcceptRequest
    subtype = create_model("SmugglingSubtype", __base__=model, token=(str, ...))
    smuggled = subtype(token=SENTINEL_SECRET)

    # The subtype itself is outside our control — it redeclared the field.
    assert isinstance(smuggled.token, str)

    for label, revalidated in (
        ("model_validate", model.model_validate(smuggled)),
        ("TypeAdapter", TypeAdapter(model).validate_python(smuggled)),
    ):
        assert isinstance(revalidated.token, InvitationSecret), label
        assert revalidated.token.reveal() == SENTINEL_SECRET, label
        assert SENTINEL_SECRET not in repr(vars(revalidated)), label
        assert SENTINEL_SECRET not in _render_traceback_holding(revalidated), label

    # And revalidation is idempotent: an already-boxed instance survives it.
    already = model(token=SENTINEL_SECRET)
    assert model.model_validate(already).token.reveal() == SENTINEL_SECRET
    assert model.model_validate({"token": SENTINEL_SECRET}).token.reveal() == SENTINEL_SECRET


async def test_no_framework_path_writes_the_field_without_boxing():
    """The direct-write sites, audited against the installed library.

    The companion to the forward enumeration above: that one proves every
    public entry point boxes, this one accounts for the places pydantic
    writes ``model.__dict__`` directly, so the two meet in the middle.

    - ``_model_field_setattr_handler`` (``pydantic/main.py``) does
      ``model.__dict__[name] = val``, reachable only via
      ``BaseModel.__setattr__``, which calls ``_check_frozen`` first. Our
      ``frozen=True`` raises before the write, and since the handler is never
      returned it is never memoized either — so no cached bypass exists for a
      later assignment.
    - ``model_copy(update=...)`` and the deprecated ``copy(update=...)``
      (``deprecated/copy_internals.py``) both install the update mapping
      straight into ``__dict__``. Both are overridden to box first.

    The remaining handlers write private attributes, extra fields, and cached
    properties — none of which this model has, with ``extra`` at its default.
    FastAPI contains no ``object.__setattr__`` and no ``__dict__[...]`` write
    anywhere, and never calls ``model_construct``: its body models go through
    ordinary validation, which this test exercises over real HTTP.
    """

    from fastapi import FastAPI

    model = invitations_router.InvitationAcceptRequest

    assert model.model_config.get("frozen") is True
    with pytest.raises(Exception):  # noqa: B017 - pydantic's frozen error type is not contract
        model(token=SENTINEL_SECRET).token = SENTINEL_SECRET
    assert "token" not in getattr(model, "__pydantic_setattr_handlers__", {})

    probe = FastAPI()

    @probe.post("/probe")
    def probe_route(payload: invitations_router.InvitationAcceptRequest) -> dict:
        return {
            "type": type(payload.token).__name__,
            "revealed_matches": payload.token.reveal() == SENTINEL_SECRET,
            "vars_leaks": SENTINEL_SECRET in repr(vars(payload)),
        }

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        response = await client.post("/probe", json={"token": SENTINEL_SECRET})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "type": "InvitationSecret",
        "revealed_matches": True,
        "vars_leaks": False,
    }


def test_the_accept_model_refuses_assignment_and_deep_copies():
    """The remaining mutation routes, closed and stated.

    Assignment does not validate by default, so ``frozen=True`` is what stops
    a bare string being put back. ``model_copy(deep=True)`` raises rather than
    duplicating the credential — a refusal, not a leak, and the same answer
    ``copy.deepcopy`` gives.
    """

    payload = invitations_router.InvitationAcceptRequest(token=SENTINEL_SECRET)

    with pytest.raises(Exception):  # noqa: B017 - pydantic's frozen error type is not contract
        payload.token = SENTINEL_SECRET
    with pytest.raises(TypeError):
        payload.model_copy(deep=True)
    assert payload.token.reveal() == SENTINEL_SECRET


def test_the_wrapper_is_immutable_and_final():
    """A frozen carrier is only as frozen as the value it holds.

    Rebinding a wrapper's slot would swap the credential inside a dataclass
    that advertises it cannot change; and a subclass could override
    ``__repr__`` to undo every redaction while still satisfying "this field
    holds an InvitationSecret" for the carrier detector. Both cost one method
    to close.
    """

    secret = InvitationSecret(SENTINEL_SECRET)

    with pytest.raises(TypeError, match="modified"):
        secret._value = "swapped"
    with pytest.raises(TypeError, match="modified"):
        del secret._value
    assert secret.reveal() == SENTINEL_SECRET

    with pytest.raises(TypeError, match="final"):

        class Leaky(InvitationSecret):
            def __repr__(self) -> str:  # pragma: no cover - never constructed
                return self.reveal()


def test_the_wrapper_compares_by_identity_on_purpose():
    """A decision, recorded: secrets are handles, not values.

    Nothing in this system compares or hashes a secret — redemption is a
    digest lookup in Postgres and the address match is ``compare_digest`` on
    strings — so a value-comparing ``__eq__`` would add a second supported
    reader of the value for no consumer. Two independently minted secrets are
    genuinely different secrets. This is pinned rather than left implicit so
    the consequence for the carriers below is visible.
    """

    same_value = (InvitationSecret(SENTINEL_SECRET), InvitationSecret(SENTINEL_SECRET))
    assert same_value[0] != same_value[1]
    assert hash(same_value[0]) != hash(same_value[1])
    assert same_value[0] == same_value[0]
    assert len({same_value[0], same_value[0]}) == 1

    # And the consequence, stated: carriers inherit identity semantics.
    minted = [
        invitations_module.MintedSecret(raw=secret, token_hash="deadbeef") for secret in same_value
    ]
    assert minted[0] != minted[1]
    assert minted[0] == minted[0]


def test_the_known_carriers_still_declare_the_wrapper():
    """The detector's other direction: carriers must not quietly revert.

    ``test_every_type_holding_the_wrapper_is_on_the_carrier_list`` asks
    "is everything that holds one covered?", which is vacuously satisfied if
    a field is changed back to a raw ``str``. This asks the opposite question
    and names the carriers, so downgrading one is a failure rather than a
    detector that finds nothing.
    """

    declared = {
        (owner.__name__, field)
        for module in _all_package_modules()
        for owner, field in _credential_fields(module)
    }
    assert declared == {
        ("MintedSecret", "raw"),
        ("IssuedInvitation", "raw_secret"),
        ("InvitationEmailMessage", "text_body"),
    }

    # The accept model is checked on the instance instead, because its
    # annotation is legitimately `str`: the length and alphabet constraints
    # have to apply to the value as it arrives off the wire, *before* it is
    # boxed, so the wrapper is applied by an AfterValidator that no static
    # inspection can see. What matters is the same either way — what the
    # field ends up holding.
    payload = invitations_router.InvitationAcceptRequest(token=SENTINEL_SECRET)
    assert isinstance(payload.token, InvitationSecret)


def test_the_detector_catches_an_innocuously_named_carrier():
    """First evasion closed: an innocent-looking field name.

    A field called ``material`` says nothing about being a credential, and
    under the original name-matching rule this carrier sailed through. Here
    the *guard itself* runs against a module containing it and must fail.
    """

    import sys
    import types

    module = types.ModuleType("innocuous_carrier_module")

    @dataclasses.dataclass
    class Sneaky:
        material: InvitationSecret

    Sneaky.__module__ = module.__name__
    module.Sneaky = Sneaky
    sys.modules[module.__name__] = module
    try:
        assert _credential_fields(module) == [(Sneaky, "material")]
        with pytest.raises(AssertionError, match="material"):
            _assert_every_carrier_is_covered([module])
    finally:
        del sys.modules[module.__name__]


def test_the_detector_finds_a_carrier_in_a_module_nobody_listed():
    """Second evasion closed: a carrier in a brand-new module.

    The guard used to scan four named modules, so a carrier in a fifth was
    invisible and the test passed — which is precisely the case it exists to
    catch, since "someone adds one later" usually means "in a new file". The
    scan now discovers modules, so this injects a carrier into a package
    module no list mentions and asserts the *whole* guard fails, not just the
    per-module helper.
    """

    import sys
    import types

    name = "collab_hub_api.frames.zz_probe_new_carrier_module"
    module = types.ModuleType(name)

    @dataclasses.dataclass
    class LatecomerCarrier:
        payload: InvitationSecret

    LatecomerCarrier.__module__ = name
    module.LatecomerCarrier = LatecomerCarrier
    sys.modules[name] = module
    try:
        discovered = _all_package_modules()
        assert module in discovered, "a package module in sys.modules was not discovered"
        with pytest.raises(AssertionError, match="LatecomerCarrier"):
            _assert_every_carrier_is_covered(discovered)
    finally:
        del sys.modules[name]

    # And with it gone, the real guard is clean again.
    _assert_every_carrier_is_covered(_all_package_modules())


def test_no_carrier_stores_a_raw_string_credential():
    """The other half of the rule: a raw ``str`` credential is itself a failure.

    Every carrier on the list must hold the wrapper somewhere. A carrier that
    kept a plain string would pass the probes only by accident — via a
    hand-written ``__repr__`` — and would still be flattened by
    ``asdict``/``vars``/FastAPI's encoder, which read the value.
    """

    for name, carrier in _carriers_of_the_secret().items():
        if isinstance(carrier, InvitationSecret):
            continue
        values = (
            list(vars(carrier).values())
            if hasattr(carrier, "__dict__")
            else [getattr(carrier, slot) for slot in getattr(type(carrier), "__slots__", ())]
        )
        assert any(isinstance(value, InvitationSecret) for value in values), (
            f"{name} carries the secret as a raw value rather than an InvitationSecret"
        )


def test_the_service_is_never_handed_a_raw_accept_token():
    """The accept path hashes at the edge, so no failing frame holds a secret.

    ``hash_invitation_secret`` is defined over every ``str`` (tested above),
    and pydantic has already validated ``token`` as an ASCII ``str`` before
    the call, so on this route it cannot raise and can never be the frame in
    a traceback. Hashing in the handler therefore means the only name bound
    to a live accept token anywhere is ``payload``, which neither renders nor
    serializes it.
    """

    assert "token_hash" in inspect.signature(PostgresInvitationService.accept).parameters
    assert "raw_secret" not in inspect.signature(PostgresInvitationService.accept).parameters
    router_source = _executable_source(invitations_router)
    assert "hash_invitation_secret ( payload . token . reveal ( ) )" in router_source


# ===========================================================================
# Gate B: exact match, and nothing that looks like canonicalization
# ===========================================================================


def test_issuance_stores_the_address_ascii_lowercased():
    """Amended on #157: stored lowered, because the IdP asserts lowered.

    The previous rule stored the address exactly as typed. Keycloak lowercases
    every account's email, so a stored capital was an invitation whose claim
    could never equal it — and the invitee was told it had been sent to a
    different address while looking at their own.

    Everything except case is still preserved: the dot and the plus-tag below
    are part of the address, not decoration to be canonicalized away.
    """

    assert validate_invited_email(INVITED_EMAIL) == INVITED_EMAIL.lower()
    assert validate_invited_email("a.b+tag@Sub.Example.COM") == "a.b+tag@sub.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "no-at-sign",
        "two@at@signs.com",
        "Display Name <someone@example.com>",
        "one@example.com, two@example.com",
        "trailing space@example.com",
        " leading@example.com",
        "unicode@exämple.com",
        "@example.com",
        "local@",
    ],
)
def test_issuance_refuses_anything_that_is_not_one_exact_mailbox(value):
    with pytest.raises(ValueError):
        validate_invited_email(value)


def test_matching_folds_ascii_case_and_nothing_else():
    """The amended rule (#157), stated so it cannot drift either way.

    Case folds, because Keycloak has already decided case does not
    distinguish accounts. Nothing else folds, because Gate B rejected an
    address-equivalence ruleset and that half stands.
    """

    assert emails_match(INVITED_EMAIL, INVITED_EMAIL)
    assert emails_match(INVITED_EMAIL, INVITED_EMAIL.lower())
    assert emails_match(INVITED_EMAIL, INVITED_EMAIL.upper())
    # The reported incident, as a test: issued with a capital, claimed lowered.
    assert emails_match("Alice@example.com", "alice@example.com")
    # Still refused: the canonicalization Gate B kept out.
    assert not emails_match("a+tag@example.com", "a@example.com")
    assert not emails_match("a.b@gmail.com", "ab@gmail.com")


def test_folding_is_bounded_to_ascii_so_no_unicode_widens_equality():
    """Why the fold is a byte translation rather than ``str.lower()``.

    Each of these would collapse onto an ASCII address under a Unicode-aware
    fold, which would let a claim that is *not* the invited address satisfy the
    gate:

    * U+212A KELVIN SIGN lowers to ASCII ``k`` under ``str.lower()``;
    * ``ẛ`` (U+1E9B) casefolds to ``ṡ``, and ``ß`` casefolds to ``ss``;
    * Turkish ``İ`` lowers to ``i`` plus a combining dot.

    Translating only 0x41–0x5A on UTF-8 bytes cannot touch any of them, since
    every byte of a multi-byte sequence is ≥ 0x80.
    """

    assert not emails_match("k@example.com", "\u212a@example.com")
    assert not emails_match("ss@example.com", "\u00df@example.com")
    assert not emails_match("i@example.com", "\u0130@example.com")


def test_matching_a_non_ascii_or_surrogate_claim_is_false_not_an_exception():
    assert not emails_match(INVITED_EMAIL, "ünïcödé@example.com")
    assert not emails_match(INVITED_EMAIL, "\ud800@example.com")


def test_the_only_normalization_is_the_bounded_ascii_fold():
    """Gate B removed the equivalence ruleset; #157 added back exactly one rule.

    The guard is kept and re-aimed rather than deleted. What it now forbids is
    any *widening* normalization — a provider-specific canonicalizer, or a
    Unicode fold that maps distinct addresses together — while permitting the
    single bounded fold the amendment ratified.

    ``ascii_folded_bytes`` is asserted present because it is meant to be the
    one definition of address equality: a second one appearing elsewhere is
    precisely the drift this test exists to catch.
    """

    source = Path(invitations_module.__file__).read_text()
    tree = ast.parse(source)

    # Inspect *code*, not text. The previous version grepped for the literal
    # ".casefold()", which this module now mentions in a docstring explaining
    # why it must not be used — a guard that a correct explanation can break is
    # a guard people delete.
    called = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "casefold" not in called, "casefold widens address equality (ß → ss)"
    assert "lower" not in called, "str.lower() maps U+212A onto ASCII k; fold on bytes instead"

    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not any("canonicalize" in name for name in defined)
    # The one definition of address equality, which #157 ratified.
    assert "ascii_folded_bytes" in defined


@pytest.mark.parametrize(
    ("email", "verified"),
    [
        ("someone@example.com", False),
        ("someone@example.com", None),
        ("someone@example.com", "true"),
        ("someone@example.com", 1),
        (None, True),
        ("", True),
        (12345, True),
    ],
)
def test_a_claim_without_a_usable_verified_email_is_one_explicit_state(email, verified):
    with pytest.raises(EmailNotVerifiedError):
        verified_claim_email(email, verified)


def test_a_boolean_true_claim_with_an_address_is_accepted():
    assert verified_claim_email("someone@example.com", True) == "someone@example.com"


# ---------------------------------------------------------------------------
# The token as the proof of mailbox control (frames.invitations.requireVerifiedEmail)
# ---------------------------------------------------------------------------


def test_the_strict_check_is_the_default_everywhere_it_is_spelled():
    """Fail closed. A caller that forgets the setting must get the strict rule,
    and an upgrade must not weaken a deployment that never asked."""

    from collab_hub_api.config import FramesInvitationsConfig

    assert FramesInvitationsConfig().require_verified_email is True
    with pytest.raises(EmailNotVerifiedError):
        verified_claim_email("someone@example.com", False)  # no keyword passed


@pytest.mark.parametrize("verified", [False, None, "true", 1, 0])
def test_relaxed_accepts_an_unverified_address_whatever_the_claim_says(verified):
    """The token stood in for the proof, so the flag stops being consulted --
    including the string and integer shapes the strict path refuses."""

    assert (
        verified_claim_email("someone@example.com", verified, require_verified=False)
        == "someone@example.com"
    )


@pytest.mark.parametrize("email", [None, "", 12345, [], {}])
def test_relaxed_still_needs_an_actual_address(email):
    """Dropping the verification requirement does not make the address optional.
    Gate B's match has to compare something."""

    with pytest.raises(EmailNotVerifiedError):
        verified_claim_email(email, True, require_verified=False)


def test_relaxing_verification_does_not_relax_the_address_match():
    """**The property that matters.** Without this, the relaxation would turn an
    invitation into a bearer token redeemable under any identity.

    Asserted against `_evaluate_acceptance` rather than the helper, because the
    helper cannot see the invitation -- the match is the caller's half, and the
    question is whether the two halves are independently relaxable. They must
    not be.
    """

    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    invitation = _invitation()

    # Unverified AND the wrong address: still refused, and refused as a
    # mismatch rather than as a verification failure -- the reader is told the
    # true reason.
    with pytest.raises(invitations_module.InvitationEmailMismatchError):
        invitations_module._evaluate_acceptance(
            invitation, now, INVITEE, "someone-else@example.com", False, require_verified=False
        )

    # Unverified and the right address: accepted, which is the whole point.
    assert (
        invitations_module._evaluate_acceptance(
            invitation, now, INVITEE, INVITED_EMAIL, False, require_verified=False
        )
        is None
    )

    # And with the strict setting the same right-address claim is refused, so
    # the setting is what decides rather than the address.
    with pytest.raises(EmailNotVerifiedError):
        invitations_module._evaluate_acceptance(
            invitation, now, INVITEE, INVITED_EMAIL, False, require_verified=True
        )


def test_a_dead_token_is_still_dead_however_verification_is_configured():
    """Order is load-bearing: the token's own state is decided before anything
    about the caller, so relaxing the identity check cannot resurrect a revoked,
    used, or expired link."""

    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    cases = [
        (_invitation(STATUS_REVOKED), invitations_module.InvitationRevokedError),
        (_invitation(STATUS_ACCEPTED), invitations_module.InvitationAlreadyUsedError),
        (_invitation(expires_in=-timedelta(days=1)), invitations_module.InvitationExpiredError),
    ]
    for invitation, expected in cases:
        with pytest.raises(expected):
            invitations_module._evaluate_acceptance(
                invitation, now, INVITEE, INVITED_EMAIL, False, require_verified=False
            )


def test_accept_threads_the_setting_to_both_decisions_without_a_database(monkeypatch):
    """Both threads, and it has to be both.

    `_accept_once` consults the setting twice -- once through
    `_evaluate_acceptance` and again through `verified_claim_email`, whose result
    becomes the persisted membership address. An earlier version of this test
    replaced the first with a spy that raised immediately, so execution never
    reached the second: deleting *that* keyword left it green, and on a relaxed
    deployment it would refuse every acceptance permanently, since the provider
    has stopped verifying and no account can satisfy the strict check. The live
    tests that would catch it are the ones CI skips, which is the gap this test
    exists to close.

    So the first spy returns a passing verdict and the second records and stops.
    `monkeypatch` rather than hand-restoring: a raise before a `try` would
    otherwise leak a spy into the next test.
    """

    import contextlib

    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    row = {
        "id": "inv-1",
        "org_id": ORG,
        "email": INVITED_EMAIL,
        "status": STATUS_PENDING,
        "created_at": now,
        "created_by": OPERATOR,
        "expires_at": now + timedelta(days=7),
        "accepted_at": None,
        "accepted_by": None,
        "accepted_org_id": None,
        "revoked_at": None,
        "revoked_by": None,
        "server_now": now,
    }

    class FakeConn:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchone(self):
            return row

    class FakeDb:
        @contextlib.contextmanager
        def connection(self):
            yield FakeConn()

    class Stop(Exception):
        pass

    for flag in (True, False):
        evaluated: list[bool] = []
        claimed: list[bool] = []

        def evaluate(*_args, require_verified: bool, **_kwargs):
            evaluated.append(require_verified)
            return None  # a passing verdict, so execution continues

        def claim(email, _verified, *, require_verified: bool = True):
            claimed.append(require_verified)
            raise Stop

        monkeypatch.setattr(invitations_module, "_evaluate_acceptance", evaluate)
        monkeypatch.setattr(invitations_module, "verified_claim_email", claim)

        service = invitations_module.PostgresInvitationService(
            FakeDb(), require_verified_email=flag
        )
        with pytest.raises(Stop):
            service.accept(
                user_id=INVITEE,
                display=display(INVITED_EMAIL, verified=False),
                token_hash="h" * 64,
                claim_email=INVITED_EMAIL,
                email_verified=False,
            )

        assert evaluated == [flag], f"the evaluation thread lost the setting for {flag}"
        assert claimed == [flag], f"the claim thread lost the setting for {flag}"


def test_the_service_builder_passes_the_setting_through():
    """Review finding: the seam moved up a level rather than closing.

    The live tests construct `PostgresInvitationService(..., require_verified_email=False)`
    directly, so deleting the keyword in `build_invitation_service` left the suite
    green -- and the failure is the total one: a deployment sets the flag, gets
    the relaxed copy, and every acceptance is still refused. The sibling email
    builder had this test; the service builder was the asymmetry.
    """

    from collab_hub_api.config import Config, build_invitation_service
    from collab_hub_api.frames.db import PostgresPools

    def service_for(flag: bool):
        config = Config.parse(
            {
                "frames": {
                    "invitations": {"require_verified_email": flag},
                    "postgres": {"url": "postgresql://user:pw@127.0.0.1:1/db"},
                }
            }
        )
        # Pools are lazy: `database()` does not connect, so this needs no server.
        return build_invitation_service(config, PostgresPools())

    assert service_for(True)._require_verified_email is True
    assert service_for(False)._require_verified_email is False


def test_the_service_carries_the_setting_and_defaults_it_closed():
    from collab_hub_api.frames.invitations import PostgresInvitationService

    assert PostgresInvitationService(object())._require_verified_email is True
    assert PostgresInvitationService(object(), require_verified_email=False)._require_verified_email is False


def test_email_verified_reaches_the_display_identity_only_as_a_boolean():
    assert display_identity_from_claims({"email": "a@b.com", "email_verified": True}).email_verified is True
    assert display_identity_from_claims({"email": "a@b.com", "email_verified": "true"}).email_verified is False
    assert display_identity_from_claims({"email": "a@b.com"}).email_verified is False
    # And the default is unverified, so a hand-built context is never trusted.
    assert DisplayIdentity().email_verified is False


def test_single_issuer_assumption_is_recorded_where_the_subs_are_used():
    """R12: every principal here is a bare ``sub``.

    The enforcement lives in ``identity.enforce_single_issuer_for_pin`` and
    runs at startup; what this asserts is that the module which *depends* on
    it says so, so a future multi-issuer change cannot miss this file.
    """

    doc = invitations_module.__doc__
    assert "single-issuer" in doc.lower() or "single issuer" in doc.lower()
    assert "enforce_single_issuer_for_pin" in doc


# ===========================================================================
# The state machine
# ===========================================================================


def test_expired_is_derived_and_terminal_states_win_over_the_clock():
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    later = now + timedelta(days=8)

    assert effective_status(_invitation(), now) == STATUS_PENDING
    assert effective_status(_invitation(), later) == STATUS_EXPIRED
    # A row that reached a stored terminal state keeps it forever: what
    # happened to the invitation is more informative than the clock passing.
    assert effective_status(_invitation(STATUS_ACCEPTED), later) == STATUS_ACCEPTED
    assert effective_status(_invitation(STATUS_REVOKED), later) == STATUS_REVOKED


def test_expiry_is_inclusive_at_the_boundary():
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    assert effective_status(_invitation(expires_in=timedelta(0)), now) == STATUS_EXPIRED


def test_the_default_lifetime_is_forty_eight_hours():
    """Shortened from 7 days by #131, the compensating control for the
    operator page rendering the redemption link (#91)."""
    assert INVITATION_TTL == timedelta(hours=48)


def test_an_invitation_is_refused_once_the_default_lifetime_elapses():
    """#131's acceptance: an invitation issued with the default TTL presents
    as expired — and is therefore refused everywhere — once 48 hours pass.

    ``effective_status`` is the module's single derivation of ``expired``;
    the live suite proves redemption honours it against the database clock
    (``test_live_an_expired_token_is_expired_and_stays_unconsumed``), so what
    this pins is that the *default* window really is the 48-hour one.
    """

    issued_at = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    invitation = _invitation(expires_in=INVITATION_TTL)

    assert effective_status(invitation, issued_at + timedelta(hours=47)) == STATUS_PENDING
    # Expiry is inclusive at the boundary (pinned separately above), so the
    # 48th hour itself is already refused.
    assert effective_status(invitation, issued_at + timedelta(hours=48)) == STATUS_EXPIRED
    assert effective_status(invitation, issued_at + timedelta(days=7)) == STATUS_EXPIRED


def test_the_granted_role_follows_from_whether_an_org_is_named():
    assert _invitation(org_id=None).granted_role == ROLE_OWNER
    assert _invitation(org_id=ORG).granted_role == ROLE_MEMBER


# ===========================================================================
# Schema: migrations are appended, and pinned to the Python vocabularies
# ===========================================================================


def test_migrations_are_appended_and_shipped_versions_are_untouched():
    """Append-only, checked in both directions.

    The version list grows and the *content* of already-released versions does
    not. Bumping the expected list when a migration is added is the intended
    edit; changing what an earlier version contains is the failure this guards,
    because a deployed database has already applied that text and will never
    apply it again.
    """

    versions = [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    assert LATEST_COLLAB_SCHEMA_VERSION == 13

    version_two = dict(COLLAB_SCHEMA_MIGRATIONS)[2]
    # v2 has shipped, so its statements are frozen text. If this ever fails,
    # someone amended a released version instead of appending one.
    assert any("collab_platform_roles" in statement for statement in version_two)
    assert any("collab_audit_events" in statement for statement in version_two)
    assert not any("collab_invitations" in statement for statement in version_two)

    version_three = dict(COLLAB_SCHEMA_MIGRATIONS)[3]
    assert any("collab_invitations" in statement for statement in version_three)
    assert not any("collab_provisioned_accounts" in statement for statement in version_three), (
        "v3 has shipped; the provisioning table belongs to v4"
    )

    # v4 (#172): the provisioning claim, keyed on the folded address.
    version_four = dict(COLLAB_SCHEMA_MIGRATIONS)[4]
    assert any("collab_provisioned_accounts" in statement for statement in version_four)
    assert any("email_folded     text PRIMARY KEY" in statement for statement in version_four), (
        "the primary key IS the serialization contract -- it is what makes a "
        "second concurrent claim conflict rather than create a second account"
    )

    # v5 (#180): the audit vocabulary gains `service_access.grant`. It is a
    # *replacement* of v2's constraint and not an edit of v2, which is the rule
    # this test exists to enforce, applied to a constraint instead of a table.
    version_five = dict(COLLAB_SCHEMA_MIGRATIONS)[5]
    assert all("collab_audit_events" in statement for statement in version_five)
    assert any("service_access.grant" in statement for statement in version_five)
    assert not any("service_access.grant" in statement for statement in dict(COLLAB_SCHEMA_MIGRATIONS)[2]), (
        "v2 has shipped; the widened vocabulary belongs to v5"
    )

    # v6 (#180): the durable record of what an acceptance owes.
    version_six = dict(COLLAB_SCHEMA_MIGRATIONS)[6]
    assert any("collab_service_access_grants" in statement for statement in version_six)
    assert any("PRIMARY KEY (user_id, group_path)" in statement for statement in version_six), (
        "the primary key IS the idempotence contract -- one row per person and "
        "group is what makes a second invitation not owe a second grant"
    )


def test_the_stored_status_vocabulary_is_pinned_to_the_check_constraint():
    ddl = "\n".join(dict(COLLAB_SCHEMA_MIGRATIONS)[3])
    constrained = ddl.split("status IN (")[1].split(")")[0]
    spelled = {value.strip().strip("'") for value in constrained.split(",")}
    assert spelled == set(INVITATION_STORED_STATUSES)
    # `expired` is derived and must never be storable.
    assert STATUS_EXPIRED not in spelled


def test_the_neutral_placeholder_name_matches_the_column_default():
    version_one = "\n".join(dict(COLLAB_SCHEMA_MIGRATIONS)[1])
    assert f"DEFAULT '{NEUTRAL_ORG_NAME}'" in version_one


def test_org_id_immutability_is_enforced_by_the_schema_not_by_convention():
    ddl = "\n".join(dict(COLLAB_SCHEMA_MIGRATIONS)[3])
    assert "collab_invitations_org_id_immutable" in ddl
    assert "BEFORE UPDATE ON collab_invitations" in ddl
    # DROP-then-CREATE, because CREATE OR REPLACE TRIGGER is PostgreSQL 14+
    # and the runner's idempotence rule must not raise the minimum server
    # version by a side door.
    assert "DROP TRIGGER IF EXISTS collab_invitations_org_id_immutable" in ddl
    assert "CREATE OR REPLACE TRIGGER" not in ddl


def test_the_secret_is_never_a_column():
    ddl = "\n".join(dict(COLLAB_SCHEMA_MIGRATIONS)[3])
    assert "token_hash" in ddl
    assert "token " not in ddl.replace("token_hash", "")
    assert "secret" not in ddl


def test_the_actions_this_issue_records_are_all_in_the_ratified_vocabulary():
    assert {"invitation.send", "invitation.revoke", "invitation.redeem", "org.create"} <= AUDIT_ACTIONS


# ===========================================================================
# Composition: this issue writes no audit rows and makes no auth decision
# ===========================================================================


def _executable_source(module) -> str:
    """A module's source with comments and docstrings removed.

    These assertions are about what the code *does*, and the prose in this
    package discusses the very tables and fields it must not touch. Tokenizing
    is what keeps "explains the rule" from reading as "breaks the rule".
    """

    import io
    import tokenize

    source = Path(module.__file__).read_text()
    kept: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
            tokenize.NL,
        ):
            continue  # a docstring or a bare attribute-documentation string
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            kept.append(token.string)
        previous = token.type
    return " ".join(kept)


def test_nothing_here_writes_an_audit_row_or_decides_authorization():
    """#87 owns both halves; this issue composes them.

    Asserted against the source because there is no runtime signal for "did
    not reimplement": an INSERT into ``collab_audit_events`` from here would
    be a second writer of a log that is only trustworthy because it has one.
    """

    for module in (invitations_module, invitations_router):
        source = _executable_source(module)
        for table in ("collab_audit_events", "collab_platform_roles"):
            assert table not in source, f"{module.__name__} touches {table} directly"
        # Role *reads* are the wrappers' job; a hand-rolled comparison here
        # would be an authorization decision this issue is not allowed to make.
        # (Naming the decorators is fine — applying one is the whole point.)
        assert ". platform_role" not in source
        assert ". org_role" not in source


def test_the_acceptance_transaction_body_touches_no_other_connection():
    """#87's one un-mechanized contract, asserted the only way available.

    ``audited()`` cannot detect a second ``db.connection()`` checkout inside
    its body — that connection commits on its own and the atomicity guarantee
    is silently gone. The body is one method, and this pins it to using only
    the guarded handle it was passed.
    """

    body = inspect.getsource(PostgresInvitationService._redeem)
    assert "self._db" not in body
    assert "connection()" not in body


def test_every_management_route_carries_a_guard_and_accept_deliberately_does_not():
    guarded = {}
    for route in invitations_router.router.routes:
        guarded[(route.path, tuple(sorted(route.methods)))] = hasattr(
            route.endpoint, "_authorization_guard_of"
        )

    assert guarded == {
        ("/operator/invitations", ("POST",)): True,
        ("/operator/invitations", ("GET",)): True,
        ("/operator/invitations/{invitation_id}/revoke", ("POST",)): True,
        ("/orgs/{org_id}/invitations", ("POST",)): True,
        ("/orgs/{org_id}/invitations", ("GET",)): True,
        ("/orgs/{org_id}/invitations/{invitation_id}/revoke", ("POST",)): True,
        # No role exists to require: the accepter's authority is the secret
        # plus the verified mailbox, both checked inside the transaction.
        ("/invitations/accept", ("POST",)): False,
    }


def test_the_shipped_router_passes_the_misordering_verifier():
    verify_protected_routes(invitations_router.router)


def test_the_verifier_would_catch_a_guard_written_above_its_route():
    """The trap #87 built ``verify_protected_routes`` for, reproduced here.

    Decorators apply bottom-up, so this order registers the *unguarded*
    function and throws the guard away. Without the verifier it is an open
    privileged route that looks protected in review.
    """

    from fastapi import APIRouter

    from collab_hub_api.frames.authorization import requires_platform_role

    bad = APIRouter()

    @requires_platform_role(PLATFORM_ROLE_OPERATOR)
    @bad.post("/operator/invitations")
    def misordered(auth: AuthContext):  # pragma: no cover - never called
        return auth

    with pytest.raises(RuntimeError, match="ORPHANED"):
        verify_protected_routes(bad)


# ===========================================================================
# Deployments with no Postgres behind the service
# ===========================================================================


def test_the_unavailable_service_refuses_every_operation():
    service = UnavailableInvitationService()
    for call in (
        lambda: service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None),
        lambda: service.list_all(limit=50),
        lambda: service.list_for_org(ORG, limit=50),
        lambda: service.get("inv-1"),
        lambda: service.revoke(OPERATOR_CTX, "inv-1"),
        lambda: service.accept(
            user_id=INVITEE,
            display=display(INVITED_EMAIL),
            token_hash="0" * 64,
            claim_email=None,
            email_verified=False,
        ),
        lambda: service.organization_name(ORG),
        lambda: service.name_organization(OPERATOR_CTX, org_id=ORG, name="Acme Widgets"),
        lambda: service.server_now(),
    ):
        with pytest.raises(InvitationsUnavailableError):
            call()


def test_the_accept_path_is_the_only_identity_level_path():
    assert invitations_router.identity_only_path("/v1/invitations/accept")
    assert not invitations_router.identity_only_path("/v1/invitations")
    assert not invitations_router.identity_only_path("/v1/operator/invitations")
    assert not invitations_router.identity_only_path("/v1/invitations/accept/extra")


def test_only_the_accept_path_redacts_its_validation_details():
    assert invitations_router.redact_validation_details("/v1/invitations/accept")
    # A deployment served under a root path still redacts.
    assert invitations_router.redact_validation_details("/nexus/v1/invitations/accept")
    assert not invitations_router.redact_validation_details("/v1/operator/invitations")


def test_a_caller_identity_carries_no_organization_at_all():
    """Nothing org-scoped can be reached with one, by construction."""

    identity = CallerIdentity(user=INVITEE, display=display(INVITED_EMAIL))
    assert not hasattr(identity, "org_id")
    assert not hasattr(identity, "home_org_id")
    assert not hasattr(identity, "org_role")
    assert not hasattr(identity, "platform_role")


# ===========================================================================
# HTTP surface without a database: reachability and redaction
# ===========================================================================


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def cookies_for(sub: str, *, email: str | None = None, email_verified: bool | str | None = True) -> dict[str, str]:
    claims: dict[str, object] = {"sub": sub, "preferred_username": f"name-{sub[:4]}"}
    if email is not None:
        claims["email"] = email
    if email_verified is not None:
        claims["email_verified"] = email_verified
    return {"IdToken-test": _jwt(claims)}


def _membership_env(monkeypatch) -> None:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)


def _config(tmp_path, **frames_overrides) -> Config:
    frames = {
        "active_state": {"backend": "memory"},
        "history": {"backend": "memory"},
        "groups": {"backend": "memory"},
        "usage": {"backend": "memory"},
        "orgs": {"backend": "memory"},
        "mcp_session_manager_enabled": False,
    }
    frames.update(frames_overrides)
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": frames,
            "tasks": {"backend": "memory"},
            # The hardened permutation: the credential check runs in the
            # path-protection middleware, *before* routing, which is where an
            # org-less invitee would otherwise be turned away.
            "security": {
                "paths": [{"path": "/health", "match": "exact", "access": "public"}],
                "default_access": "authenticated",
            },
        }
    )


@pytest_asyncio.fixture
async def dbless_client(tmp_path, monkeypatch):
    """A membership-mode app with no Postgres: the router is mounted, the
    service is the unavailable one."""

    _membership_env(monkeypatch)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_an_org_less_invitee_reaches_the_accept_route_on_a_hardened_deployment(dbless_client):
    """The bug this endpoint's auth level exists to avoid.

    Under ``default_access: authenticated`` the middleware authenticates
    before routing. If it used the membership choke point, every invitee —
    who by definition has no membership — would get ``no_organization`` and
    never reach the one route built for them. 503 here means the request got
    all the way to the handler and failed only for want of a database.
    """

    response = await dbless_client.post(
        "/v1/invitations/accept",
        json={"token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        cookies=cookies_for(INVITEE, email=INVITED_EMAIL),
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == error_codes.INVITATIONS_UNAVAILABLE


async def test_the_accept_route_still_requires_authentication(dbless_client):
    response = await dbless_client.post("/v1/invitations/accept", json={"token": "abc"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == error_codes.UNAUTHORIZED


async def test_a_near_miss_token_is_not_echoed_back_by_the_validation_error(dbless_client):
    """R3: pydantic v2 puts the rejected value in ``errors()[n].input``."""

    near_miss = "almost-a-real-token!!!"
    response = await dbless_client.post(
        "/v1/invitations/accept",
        json={"token": near_miss},
        cookies=cookies_for(INVITEE, email=INVITED_EMAIL),
    )
    assert response.status_code == 422
    assert near_miss not in response.text
    assert "details" not in response.json()["error"]


async def test_a_near_miss_token_is_not_echoed_under_a_root_path_either(tmp_path, monkeypatch):
    """The proxy-prefix case, which the app's own API-path test does not match.

    Behind a proxy that does not strip the prefix, ``/nexus/v1/invitations/
    accept`` is not recognised as an API path — so if redaction were a
    refinement of the enveloped branch, this request would fall through to
    FastAPI's default handler and echo the rejected token in its ``detail``.
    """

    _membership_env(monkeypatch)
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "server": {"root_path": "/nexus"},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "groups": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "orgs": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )
    app = make_app(config)
    near_miss = "almost-a-real-token!!!"
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, root_path="/nexus")
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/nexus/v1/invitations/accept",
                json={"token": near_miss},
                cookies=cookies_for(INVITEE, email=INVITED_EMAIL),
            )
    assert response.status_code == 422, response.text
    assert near_miss not in response.text


async def test_other_routes_keep_their_validation_details(dbless_client):
    """The redaction is surgical: only the route that carries a secret."""

    response = await dbless_client.post(
        "/v1/operator/invitations",
        json={"not_email": 1},
        cookies=cookies_for(OPERATOR),
    )
    # 403 (not an operator) or 422 — either way the point is that nothing
    # else lost its details; the operator path is exercised live below.
    assert response.status_code in (403, 422)


async def test_the_invitation_surface_is_absent_on_a_claims_sourced_deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.delenv(ORG_SOURCE_ENV, raising=False)
    app = make_app(_config(tmp_path))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any("invitation" in path for path in paths)


# ===========================================================================
# Live-Postgres coverage (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# ===========================================================================

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live invitation tests",
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


def _database(max_size: int = 12):
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=max_size, timeout_seconds=15.0)


def _drop_all(database) -> None:
    with database.connection() as conn:
        for table in COLLAB_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture
def live_db():
    """A migrated database with the standard cast seeded the runbook's way."""

    database = _database()
    try:
        _drop_all(database)
        run_collab_schema_migrations(database)
        with database.connection() as conn:
            for org in (ORG, OTHER_ORG):
                conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (org, OPERATOR))
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (OWNER, ORG, ROLE_OWNER),
            )
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (MEMBER, ORG, ROLE_MEMBER),
            )
            conn.execute(
                "INSERT INTO collab_platform_roles (user_id, role, granted_by) VALUES (%s, %s, %s)",
                (OPERATOR, PLATFORM_ROLE_OPERATOR, "bootstrap"),
            )
        yield database
        _drop_all(database)
    finally:
        database.close()


@pytest.fixture
def service(live_db):
    return PostgresInvitationService(live_db)


def _rows(database, sql: str, params=()) -> list[dict]:
    with database.connection() as conn:
        return conn.execute(sql, params).fetchall()


def _audit_rows(database) -> list[dict]:
    return _rows(
        database,
        "SELECT actor, actor_label, action, target_type, target_id, target_label, org_id, detail"
        " FROM collab_audit_events ORDER BY id",
    )


def _invitation_row(database, invitation_id: str) -> dict:
    return _rows(database, "SELECT * FROM collab_invitations WHERE id = %s", (invitation_id,))[0]


def _expire(database, invitation_id: str) -> None:
    with database.connection() as conn:
        conn.execute(
            "UPDATE collab_invitations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (invitation_id,),
        )


def _wait_until_blocked(database, expected: int = 1, timeout: float = 20.0) -> None:
    """Block until *expected* backends are waiting on a lock.

    Polling the server's own view rather than sleeping: the whole point of
    these tests is a forced interleaving, and a sleep would make them either
    slow or flaky depending on the machine.
    """

    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        while not deadline.is_set():
            with database.connection() as conn:
                waiting = conn.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity"
                    " WHERE wait_event_type = 'Lock' AND datname = current_database()"
                ).fetchone()["n"]
            if waiting >= expected:
                return
        raise AssertionError(f"no backend blocked on a lock within {timeout}s")
    finally:
        timer.cancel()


def _accept(
    service, *, user_id=INVITEE, secret: str, email=INVITED_EMAIL, verified=True, name=None, service_groups=()
):
    return service.accept(
        user_id=user_id,
        display=display(email, verified=verified, name=name),
        token_hash=hash_invitation_secret(secret),
        claim_email=email,
        email_verified=verified,
        service_groups=service_groups,
    )


def _owed_rows(live_db, user_id=INVITEE):
    return _rows(
        live_db,
        "SELECT user_id, group_path, state, invitation FROM collab_service_access_grants "
        "WHERE user_id = %s ORDER BY group_path",
        (user_id,),
    )


# --- Issuance ---------------------------------------------------------------


@live_postgres
def test_live_issuing_writes_the_invitation_and_its_event_together(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    row = _invitation_row(live_db, issued.invitation.id)
    assert row["email"] == STORED_EMAIL  # ASCII-lowered at issuance (#157)
    assert row["status"] == STATUS_PENDING
    assert row["org_id"] is None
    assert row["created_by"] == OPERATOR
    assert row["token_hash"] == hash_invitation_secret(issued.raw_secret.reveal())
    assert row["token_hash"] != issued.raw_secret.reveal()
    # Forty-eight hours, give or take the round trip.
    assert timedelta(hours=47, minutes=59) < row["expires_at"] - row["created_at"] < timedelta(hours=48, minutes=1)

    (event,) = _audit_rows(live_db)
    assert event["action"] == "invitation.send"
    assert event["actor"] == OPERATOR
    assert event["target_type"] == "invitation"
    assert event["target_id"] == issued.invitation.id
    assert event["target_label"] == STORED_EMAIL
    assert event["org_id"] is None
    assert event["detail"] == {"creates_organization": True, "ttl_hours": 48}
    # The secret and its hash are nowhere in the log.
    assert issued.raw_secret.reveal() not in json.dumps(event, default=str)
    assert row["token_hash"] not in json.dumps(event, default=str)


@live_postgres
def test_live_both_authority_axes_produce_the_same_row_apart_from_the_actor(service, live_db):
    """The whole point of composing two wrappers over one primitive."""

    service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=ORG)
    service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)

    operator_row, owner_row = _audit_rows(live_db)
    varying = {"actor", "actor_label", "target_id"}
    assert {k: v for k, v in operator_row.items() if k not in varying} == {
        k: v for k, v in owner_row.items() if k not in varying
    }
    assert operator_row["actor"] == OPERATOR
    assert owner_row["actor"] == OWNER
    assert operator_row["action"] == owner_row["action"] == "invitation.send"


@live_postgres
def test_live_issuing_into_a_nonexistent_org_leaves_no_invitation_and_no_event(service, live_db):
    """Atomicity on the issue path: the foreign key fails the whole action."""

    with pytest.raises(invitations_module.OrgNotFoundError):
        service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id="org-does-not-exist")

    assert _rows(live_db, "SELECT id FROM collab_invitations") == []
    assert _audit_rows(live_db) == []


@live_postgres
def test_live_issuing_creates_no_membership(service, live_db):
    """R11: signup — and invitation — alone grants nothing."""

    before = _rows(live_db, "SELECT user_id FROM collab_org_members")
    service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=ORG)
    assert _rows(live_db, "SELECT user_id FROM collab_org_members") == before


# --- Listing and revocation --------------------------------------------------


@live_postgres
def test_live_listing_is_scoped_by_construction(service):
    hub = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None).invitation
    mine = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG).invitation
    theirs = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=OTHER_ORG).invitation

    assert {inv.id for inv in service.list_all(limit=50).invitations} == {hub.id, mine.id, theirs.id}
    # An org's list holds its own invitations and never the org-creating ones.
    assert {inv.id for inv in service.list_for_org(ORG, limit=50).invitations} == {mine.id}
    assert {inv.id for inv in service.list_for_org(OTHER_ORG, limit=50).invitations} == {theirs.id}


@live_postgres
def test_live_listings_are_bounded_and_page_without_overlap_or_gaps(service, live_db):
    """Both list endpoints were unbounded, returning every row and every email.

    Paged here across a set large enough to need several requests, checking
    the property that actually matters: walking the pages visits every
    invitation exactly once. That holds only because the sort is a *total*
    order — ``created_at`` alone ties for rows issued in the same transaction,
    and tied rows may come back in any order between queries, which is how a
    LIMIT/OFFSET walk silently duplicates one row and drops another.
    """

    issued = {service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=ORG).invitation.id for _ in range(12)}

    seen: list[str] = []
    offset = 0
    while True:
        page = service.list_for_org(ORG, limit=5, offset=offset)
        assert len(page.invitations) <= 5
        seen.extend(invitation.id for invitation in page.invitations)
        if not page.has_more:
            break
        offset += 5
        assert offset < 100, "pagination did not terminate"

    assert len(seen) == len(issued), seen
    assert set(seen) == issued
    assert len(set(seen)) == len(seen), "a page overlapped another"

    # The operator view is bounded the same way.
    everything = service.list_all(limit=5)
    assert len(everything.invitations) == 5
    assert everything.has_more is True

    # A page past the end is empty rather than an error.
    beyond = service.list_for_org(ORG, limit=5, offset=500)
    assert beyond.invitations == [] and beyond.has_more is False

    # And the bound is enforced in the service, not only by the HTTP layer.
    for bad_limit, bad_offset in ((0, 0), (-1, 0), (5, -1)):
        with pytest.raises(ValueError):
            service.list_for_org(ORG, limit=bad_limit, offset=bad_offset)


@live_postgres
async def test_live_http_listings_are_bounded_by_default_and_capped(live_client):
    """The wire contract: a caller who passes nothing still gets a bounded page."""

    client, _database, delivery = live_client
    for _ in range(3):
        created = await client.post(
            f"/v1/orgs/{ORG}/invitations", json={"email": INVITED_EMAIL}, cookies=cookies_for(OWNER)
        )
        assert created.status_code == 201, created.text
    assert delivery.calls

    default = await client.get(f"/v1/orgs/{ORG}/invitations", cookies=cookies_for(OWNER))
    assert default.status_code == 200, default.text
    body = default.json()
    assert body["limit"] == invitations_router.DEFAULT_PAGE_SIZE
    assert body["offset"] == 0
    assert body["has_more"] is False
    assert len(body["invitations"]) == 3

    paged = await client.get(f"/v1/orgs/{ORG}/invitations?limit=2", cookies=cookies_for(OWNER))
    assert len(paged.json()["invitations"]) == 2
    assert paged.json()["has_more"] is True

    # Over the cap and below the floor are refused, not silently clamped: a
    # caller who asked for 10000 rows should learn the answer is no.
    for query in (f"limit={invitations_router.MAX_PAGE_SIZE + 1}", "limit=0", "offset=-1"):
        refused = await client.get(f"/v1/orgs/{ORG}/invitations?{query}", cookies=cookies_for(OWNER))
        assert refused.status_code == 422, (query, refused.text)


@live_postgres
def test_live_a_lapsed_invitation_presents_as_expired_without_a_sweeper(service, live_db):
    invitation = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG).invitation
    _expire(live_db, invitation.id)

    (listed,) = service.list_for_org(ORG, limit=50).invitations
    assert listed.status == STATUS_PENDING  # the stored column is untouched
    assert effective_status(listed, service.server_now()) == STATUS_EXPIRED


@live_postgres
def test_live_revoke_records_one_event_and_is_idempotent(service, live_db):
    invitation = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG).invitation

    revoked = service.revoke(OWNER_CTX, invitation.id, expect_org_id=ORG)
    assert revoked.status == STATUS_REVOKED
    assert revoked.revoked_by == OWNER
    assert _invitation_row(live_db, invitation.id)["revoked_at"] is not None

    again = service.revoke(OWNER_CTX, invitation.id, expect_org_id=ORG)
    assert again.status == STATUS_REVOKED

    actions = [row["action"] for row in _audit_rows(live_db)]
    # One send, one revoke — the second revoke changed nothing and must not
    # have written a row claiming it did.
    assert actions == ["invitation.send", "invitation.revoke"]


@live_postgres
def test_live_an_expired_invitation_can_still_be_revoked(service, live_db):
    invitation = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG).invitation
    _expire(live_db, invitation.id)
    assert service.revoke(OWNER_CTX, invitation.id, expect_org_id=ORG).status == STATUS_REVOKED


@live_postgres
def test_live_an_owner_cannot_reach_another_orgs_or_a_hub_invitation(service):
    theirs = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=OTHER_ORG).invitation
    hub = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None).invitation

    for invitation_id in (theirs.id, hub.id, "no-such-invitation"):
        with pytest.raises(InvitationNotFoundError):
            service.revoke(OWNER_CTX, invitation_id, expect_org_id=ORG)


@live_postgres
def test_live_an_accepted_invitation_cannot_be_revoked(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=ORG)
    _accept(service, secret=issued.raw_secret.reveal())

    with pytest.raises(InvitationAlreadyUsedError):
        service.revoke(OWNER_CTX, issued.invitation.id, expect_org_id=ORG)

    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_ACCEPTED
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send", "invitation.redeem"]


# --- Acceptance: the happy paths ---------------------------------------------


@live_postgres
def test_live_accepting_an_org_creating_invitation_makes_exactly_one_neutral_org(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    outcome = _accept(service, secret=issued.raw_secret.reveal(), name="Ada Lovelace")

    assert outcome.org_created is True
    assert outcome.role == ROLE_OWNER
    orgs = _rows(live_db, "SELECT id, name, created_by FROM collab_orgs WHERE id = %s", (outcome.org_id,))
    assert len(orgs) == 1
    # Ratified 2026-08-04: neutral placeholder, NOT derived from login info.
    assert orgs[0]["name"] == NEUTRAL_ORG_NAME
    assert INVITEE not in orgs[0]["name"]
    assert "Ada" not in orgs[0]["name"]
    assert "Invitee" not in orgs[0]["name"]
    assert orgs[0]["created_by"] == INVITEE

    (member,) = _rows(live_db, "SELECT * FROM collab_org_members WHERE user_id = %s", (INVITEE,))
    assert member["org_id"] == outcome.org_id
    assert member["role"] == ROLE_OWNER
    # The membership records the accepter's claim, which is not necessarily the
    # spelling the invitation was stored under: this `_accept` asserts the
    # mixed-case address, and only issuance applies the #157 fold.
    assert member["email"] == INVITED_EMAIL

    row = _invitation_row(live_db, issued.invitation.id)
    assert row["status"] == STATUS_ACCEPTED
    assert row["accepted_by"] == INVITEE
    assert row["accepted_org_id"] == outcome.org_id

    send, created = _audit_rows(live_db)
    assert send["action"] == "invitation.send"
    assert created["action"] == "org.create"
    assert created["actor"] == INVITEE  # the accepter, not the issuer
    assert created["target_type"] == "org"
    assert created["target_id"] == outcome.org_id
    assert created["org_id"] == outcome.org_id
    assert created["detail"] == {
        "invitation_id": issued.invitation.id,
        "role": ROLE_OWNER,
        "org_created": True,
        # #190: which acceptances took the relaxed path has to be answerable
        # after the fact. The membership row cannot say -- it stores the matched
        # address either way.
        "verified_email_required": True,
    }


@live_postgres
def test_live_an_acceptance_owes_its_service_groups_in_its_own_transaction(service, live_db):
    """#180: the membership, the audit row, and what is owed all commit together.

    The ordering this protects: the grant itself happens *after* this
    transaction, because a group write cannot be rolled back. That leaves a
    window, and the row written here is what makes the window survivable --
    every way the process can stop afterwards leaves a `pending` row a
    reconciler can act on. If the row were written after the commit instead,
    a crash in between would lose the fact that a grant was ever due.
    """

    issued = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)

    outcome = _accept(service, secret=issued.raw_secret.reveal(), service_groups=["/llm", "/services/next"])

    assert [(row["group_path"], row["state"], row["invitation"]) for row in _owed_rows(live_db)] == [
        ("/llm", "pending", outcome.invitation_id),
        ("/services/next", "pending", outcome.invitation_id),
    ]
    # Same transaction, so the membership this acceptance created is there too.
    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))


@live_postgres
def test_live_the_owed_row_is_written_on_the_audited_transaction_handle(service, live_db, monkeypatch):
    """The placement, not just the outcome.

    A row written a moment *after* the acceptance commits looks identical from
    the outside to one written inside it -- both are simply there afterwards --
    so the previous test cannot tell the two apart, and the difference is the
    entire point of #180's fix. What distinguishes them is which connection the
    write got: ``audited()`` hands its body a ``GuardedConnection`` that cannot
    commit or roll back on its own, and only code inside that transaction ever
    holds one. Asserting the type is asserting the placement.
    """

    from collab_hub_api.frames.audit import GuardedConnection

    seen: list[object] = []
    real = invitations_module.claim_pending

    def spy(conn, **kwargs):
        seen.append(conn)
        return real(conn, **kwargs)

    monkeypatch.setattr(invitations_module, "claim_pending", spy)

    issued = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)
    _accept(service, secret=issued.raw_secret.reveal(), service_groups=["/llm"])

    assert len(seen) == 1
    assert isinstance(seen[0], GuardedConnection), (
        "a pool checkout here would commit independently of the acceptance, "
        "which is the bug this test exists to prevent reintroducing"
    )
    assert [row["state"] for row in _owed_rows(live_db)] == ["pending"]


@live_postgres
def test_live_an_acceptance_that_owes_nothing_writes_no_rows(service, live_db):
    """The default. A deployment granting nothing accumulates no queue."""

    issued = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)
    _accept(service, secret=issued.raw_secret.reveal())
    assert _owed_rows(live_db) == []


@live_postgres
def test_live_a_replayed_acceptance_does_not_owe_a_second_time(service, live_db):
    """A reloaded acceptance page created nothing, so it owes nothing new.

    The replay path raises before the consume, so the transaction that would
    have written a second row rolls back -- which is also why the first row's
    invitation stays the recorded one.
    """

    issued = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)
    first = _accept(service, secret=issued.raw_secret.reveal(), service_groups=["/llm"])
    assert first.replay is False

    second = _accept(service, secret=issued.raw_secret.reveal(), service_groups=["/llm"])
    assert second.replay is True
    assert [(row["group_path"], row["state"]) for row in _owed_rows(live_db)] == [("/llm", "pending")]


@live_postgres
def test_live_a_refused_acceptance_owes_nothing(service, live_db):
    """Rolled back together with everything else the acceptance would have done.

    Asserted through a real refusal rather than a forced exception: a revoked
    invitation fails inside the same transaction the owed row is written in, so
    if that row were written outside it, this is the test that would find it.
    """

    issued = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)
    service.revoke(OWNER_CTX, issued.invitation.id, expect_org_id=ORG)

    with pytest.raises(invitations_module.InvitationRevokedError):
        _accept(service, secret=issued.raw_secret.reveal(), service_groups=["/llm"])

    assert _owed_rows(live_db) == []


@live_postgres
def test_live_a_relaxed_service_accepts_an_unverified_matching_account(live_db):
    """The production chain, which the first version of this change never tested.

    Review found that deleting **both** `require_verified` threads in
    `_accept_once` left the suite green: the helper was tested, the private
    attribute was read, and the path between them was not exercised at all.
    The failure that hides behind that is total -- a deployment sets
    `requireVerifiedEmail: false`, invitees get copy saying no verification is
    needed, and every acceptance is still refused. Because the operator has
    also flipped the realm by then, no account can ever verify, so every
    invitation becomes unredeemable.

    So this goes through the real service, built the way configuration builds
    it, and asserts the membership landed.
    """

    relaxed = invitations_module.PostgresInvitationService(live_db, require_verified_email=False)
    issued = relaxed.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)

    outcome = relaxed.accept(
        user_id=INVITEE,
        display=display(INVITED_EMAIL, verified=False),
        token_hash=hash_invitation_secret(issued.raw_secret.reveal()),
        claim_email=INVITED_EMAIL,
        email_verified=False,
    )

    assert outcome.org_id == ORG
    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))


@live_postgres
def test_live_a_strict_service_refuses_the_same_unverified_account(live_db):
    """The other half of the pair: same account, same token, strict service.

    Without this, the test above would pass on a build that ignored the setting
    entirely -- which is precisely the build review produced by deleting the
    threads.
    """

    strict = invitations_module.PostgresInvitationService(live_db)
    issued = strict.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)

    with pytest.raises(EmailNotVerifiedError):
        strict.accept(
            user_id=INVITEE,
            display=display(INVITED_EMAIL, verified=False),
            token_hash=hash_invitation_secret(issued.raw_secret.reveal()),
            claim_email=INVITED_EMAIL,
            email_verified=False,
        )

    assert not _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))


@live_postgres
def test_live_a_relaxed_service_still_refuses_a_mismatched_address(live_db):
    """Relaxing verification must not relax the match, asserted through the
    service rather than only through the helper."""

    relaxed = invitations_module.PostgresInvitationService(live_db, require_verified_email=False)
    issued = relaxed.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)

    with pytest.raises(invitations_module.InvitationEmailMismatchError):
        relaxed.accept(
            user_id=INVITEE,
            display=display("someone-else@example.com", verified=False),
            token_hash=hash_invitation_secret(issued.raw_secret.reveal()),
            claim_email="someone-else@example.com",
            email_verified=False,
        )

    assert not _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))


@live_postgres
def test_live_accepting_an_org_scoped_invitation_creates_nothing_but_a_membership(service, live_db):
    issued = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG)
    orgs_before = {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")}

    outcome = _accept(service, secret=issued.raw_secret.reveal())

    assert outcome == invitations_module.InvitationAcceptance(
        invitation_id=issued.invitation.id, org_id=ORG, role=ROLE_MEMBER, org_created=False
    )
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == orgs_before

    (member,) = _rows(live_db, "SELECT * FROM collab_org_members WHERE user_id = %s", (INVITEE,))
    assert (member["org_id"], member["role"]) == (ORG, ROLE_MEMBER)

    redeem = _audit_rows(live_db)[-1]
    assert redeem["action"] == "invitation.redeem"
    assert redeem["actor"] == INVITEE
    assert redeem["org_id"] == ORG
    assert redeem["target_id"] == issued.invitation.id


@live_postgres
def test_live_the_same_login_replaying_its_own_token_is_an_idempotent_success(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    first = _accept(service, secret=issued.raw_secret.reveal())
    audit_after_first = _audit_rows(live_db)

    second = _accept(service, secret=issued.raw_secret.reveal())

    assert second.replay is True
    assert second.org_id == first.org_id
    assert second.org_created is True
    # Nothing created and nothing recorded the second time round.
    assert _audit_rows(live_db) == audit_after_first
    assert len(_rows(live_db, "SELECT id FROM collab_orgs WHERE id = %s", (first.org_id,))) == 1


# --- Acceptance: every terminal state, and none of them consuming the token ---


@live_postgres
def test_live_an_unknown_token_is_not_found(service):
    with pytest.raises(InvitationNotFoundError):
        _accept(service, secret=mint_invitation_secret().raw.reveal())


@live_postgres
def test_live_an_expired_token_is_expired_and_stays_unconsumed(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    _expire(live_db, issued.invitation.id)

    with pytest.raises(InvitationExpiredError):
        _accept(service, secret=issued.raw_secret.reveal())

    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING
    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,)) == []


@live_postgres
def test_live_a_revoked_token_is_revoked(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    service.revoke(OPERATOR_CTX, issued.invitation.id)

    with pytest.raises(InvitationRevokedError):
        _accept(service, secret=issued.raw_secret.reveal())

    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,)) == []


@live_postgres
def test_live_a_second_login_replaying_a_used_token_is_refused(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    _accept(service, secret=issued.raw_secret.reveal())

    with pytest.raises(InvitationAlreadyUsedError):
        _accept(service, user_id=OTHER_INVITEE, secret=issued.raw_secret.reveal())

    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (OTHER_INVITEE,)) == []
    assert len(_rows(live_db, "SELECT id FROM collab_orgs")) == 3  # ORG, OTHER_ORG, and the one created


@live_postgres
@pytest.mark.parametrize("verified", [False, None, "true"])
def test_live_an_unverified_claim_is_refused_without_consuming_the_token(service, live_db, verified):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    with pytest.raises(EmailNotVerifiedError):
        service.accept(
            user_id=INVITEE,
            display=display(INVITED_EMAIL, verified=False),
            token_hash=hash_invitation_secret(issued.raw_secret.reveal()),
            claim_email=INVITED_EMAIL,
            email_verified=verified,
        )

    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING


@live_postgres
def test_live_a_missing_email_claim_is_the_same_state(service, live_db):
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    with pytest.raises(EmailNotVerifiedError):
        service.accept(
            user_id=INVITEE,
            display=display(None),
            token_hash=hash_invitation_secret(issued.raw_secret.reveal()),
            claim_email=None,
            email_verified=True,
        )

    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING


@live_postgres
def test_live_a_case_different_address_redeems_the_invitation(service, live_db):
    """The reported incident, end to end (#157).

    An operator types a capitalised address; Keycloak asserts the lowered one,
    because it lowercases every account's email. Under the original exact-match
    rule this raised ``InvitationEmailMismatchError`` and the invitation was
    unredeemable by anyone — the invitee was told it had been sent to a
    different address while looking at their own.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    assert _invitation_row(live_db, issued.invitation.id)["email"] == STORED_EMAIL

    outcome = _accept(service, secret=issued.raw_secret.reveal(), email=STORED_EMAIL)
    assert outcome.org_created is True


@live_postgres
def test_live_a_verified_email_that_changed_after_issuance(service, live_db):
    """The claim is matched at accept time; there is no issuance snapshot.

    Changing away from the invited address stops acceptance; changing *to*
    it starts working, with no reissue and no state to migrate.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    with pytest.raises(InvitationEmailMismatchError):
        _accept(service, secret=issued.raw_secret.reveal(), email="moved-on@example.com")
    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING

    outcome = _accept(service, secret=issued.raw_secret.reveal(), email=INVITED_EMAIL)
    assert outcome.org_created is True


@live_postgres
@pytest.mark.parametrize("status", ["active", MEMBERSHIP_REMOVED])
def test_live_a_login_that_already_has_a_membership_is_refused(service, live_db, status):
    """Any row blocks — including a removed one, and including one in the
    invitation's own target organization."""

    with live_db.connection() as conn:
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role, status) VALUES (%s, %s, %s, %s)",
            (INVITEE, OTHER_ORG, ROLE_MEMBER, status),
        )
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=ORG)

    with pytest.raises(AlreadyInOrganizationError):
        _accept(service, secret=issued.raw_secret.reveal())

    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING
    (member,) = _rows(live_db, "SELECT org_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))
    assert member["org_id"] == OTHER_ORG


@live_postgres
def test_live_a_refused_acceptance_leaves_no_org_and_no_event(service, live_db):
    """The org-creating path's rollback: nothing survives a failed accept."""

    with live_db.connection() as conn:
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
            (INVITEE, OTHER_ORG, ROLE_MEMBER),
        )
    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    orgs_before = {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")}

    with pytest.raises(AlreadyInOrganizationError):
        _accept(service, secret=issued.raw_secret.reveal())

    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == orgs_before
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send"]


# --- Concurrency: forced interleavings, then a real race ---------------------


@live_postgres
def test_live_a_second_accept_of_one_token_loses_deterministically(service, live_db):
    """The ``FOR UPDATE`` serialization, forced rather than hoped for.

    A held row lock puts the second acceptance exactly where the race matters
    — past its pre-read, blocked inside its transaction — and the winner then
    commits underneath it. Read Committed re-evaluates the locked row, so the
    loser sees ``accepted`` and rolls its own organization back with it.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    winner_org = "org-winner"
    failures: list[BaseException] = []

    def loser():
        try:
            _accept(service, user_id=OTHER_INVITEE, secret=issued.raw_secret.reveal())
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
            failures.append(exc)

    holder = _database(max_size=2)
    try:
        with holder.connection() as conn:
            conn.execute(
                "SELECT id FROM collab_invitations WHERE id = %s FOR UPDATE",
                (issued.invitation.id,),
            )
            thread = threading.Thread(target=loser)
            thread.start()
            _wait_until_blocked(live_db)
            # The winner's work, committed when this block exits.
            conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (winner_org, INVITEE))
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (INVITEE, winner_org, ROLE_OWNER),
            )
            conn.execute(
                "UPDATE collab_invitations SET status = 'accepted', accepted_at = now(),"
                " accepted_by = %s, accepted_org_id = %s WHERE id = %s",
                (INVITEE, winner_org, issued.invitation.id),
            )
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        holder.close()

    assert len(failures) == 1
    assert isinstance(failures[0], InvitationAlreadyUsedError)
    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (OTHER_INVITEE,)) == []
    assert _rows(live_db, "SELECT accepted_org_id FROM collab_invitations")[0]["accepted_org_id"] == winner_org
    # The loser's speculatively-minted organization was never committed.
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {ORG, OTHER_ORG, winner_org}


def _accept_while_locked(service, live_db, hold, *, user_id=INVITEE):
    """Run one acceptance against a row another transaction holds locked.

    Returns ``(outcomes, errors)``. *hold* is called with the holding
    connection once the acceptance is provably blocked, so the test controls
    exactly what the winner did and when it committed.
    """

    outcomes: list[object] = []
    errors: list[BaseException] = []

    def run():
        try:
            outcomes.append(_accept(service, user_id=user_id, secret=hold.secret))
        except BaseException as exc:  # noqa: BLE001 - returned to the caller
            errors.append(exc)

    holder = _database(max_size=2)
    try:
        with holder.connection() as conn:
            conn.execute(
                "SELECT id FROM collab_invitations WHERE id = %s FOR UPDATE",
                (hold.invitation_id,),
            )
            thread = threading.Thread(target=run)
            thread.start()
            _wait_until_blocked(live_db)
            hold.act(conn)
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        holder.close()
    return outcomes, errors


class _Hold:
    def __init__(self, invitation_id: str, secret: str, act):
        self.invitation_id = invitation_id
        self.secret = secret
        self.act = act


@live_postgres
def test_live_the_database_refuses_to_change_an_invitations_org_id(service, live_db):
    """The foundation of the pre-read/locked-read safety argument, enforced.

    Acceptance decides its audit action and scope from a read taken *outside*
    the transaction, and re-checks only ``status`` inside it. If ``org_id``
    could move in between, an acceptance could commit a join to an existing
    organization while its audit row claimed ``org.create`` for an
    organization that never existed. No application path updates the column —
    but the beta's correction path is a person in psql, so the guarantee is
    in the database.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        with live_db.connection() as conn:
            conn.execute(
                "UPDATE collab_invitations SET org_id = %s WHERE id = %s",
                (ORG, issued.invitation.id),
            )

    # Including the other direction, and including a no-op-looking rewrite.
    scoped = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=ORG).invitation
    for new_org in (None, OTHER_ORG):
        with pytest.raises(psycopg.errors.CheckViolation):
            with live_db.connection() as conn:
                conn.execute(
                    "UPDATE collab_invitations SET org_id = %s WHERE id = %s", (new_org, scoped.id)
                )

    # And every legitimate update still works: the trigger fires on the
    # column, not on the statement.
    assert service.revoke(OPERATOR_CTX, scoped.id).status == STATUS_REVOKED
    assert _accept(service, secret=issued.raw_secret.reveal()).org_created is True
    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_ACCEPTED


# --- Transient conflicts under stricter isolation levels ---------------------


def test_a_transient_conflict_is_retried_and_a_real_error_is_not():
    """The retry is bounded, and narrow: only 40001 and 40P01."""

    attempts = []

    def flaky(failures: int, error: BaseException):
        def operation():
            attempts.append(1)
            if len(attempts) <= failures:
                raise error
            return "done"

        return operation

    assert invitations_module._retrying(flaky(2, psycopg.errors.SerializationFailure())) == "done"
    assert len(attempts) == 3

    attempts.clear()
    assert invitations_module._retrying(flaky(1, psycopg.errors.DeadlockDetected())) == "done"
    assert len(attempts) == 2

    # Anything else is the caller's problem on the first attempt.
    attempts.clear()
    with pytest.raises(InvitationExpiredError):
        invitations_module._retrying(flaky(1, InvitationExpiredError("nope")))
    assert len(attempts) == 1


def test_the_retry_gives_up_rather_than_spinning():
    attempts = []

    def always_conflicts():
        attempts.append(1)
        raise psycopg.errors.SerializationFailure()

    with pytest.raises(psycopg.errors.SerializationFailure):
        invitations_module._retrying(always_conflicts)
    assert len(attempts) == invitations_module.TRANSIENT_CONFLICT_ATTEMPTS


@live_postgres
def test_live_a_retried_acceptance_still_creates_exactly_one_org(service, live_db, monkeypatch):
    """A retry must re-decide, not replay: one org, one membership, one row.

    The first attempt is aborted at the transaction boundary the way a
    serialization failure aborts it, so nothing it did survives; the second
    runs its own pre-read against current committed state.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    real_audited = invitations_module.audited
    calls = []

    def flaky_audited(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.errors.SerializationFailure()
        return real_audited(*args, **kwargs)

    monkeypatch.setattr(invitations_module, "audited", flaky_audited)

    outcome = _accept(service, secret=issued.raw_secret.reveal())

    assert len(calls) == 2
    assert outcome.org_created is True
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {ORG, OTHER_ORG, outcome.org_id}
    assert len(_rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))) == 1
    assert len([row for row in _audit_rows(live_db) if row["action"] == "org.create"]) == 1


@live_postgres
def test_live_racing_accepts_under_serializable_still_get_terminal_states(live_db, monkeypatch):
    """The finding's actual scenario, against a SERIALIZABLE database.

    A deployment can set ``default_transaction_isolation`` on the database or
    the role at any time. Under SERIALIZABLE the same races abort with 40001
    instead of resolving by blocking, so without the retry an invitee would
    see a 503 where the contract promises a terminal state. Correctness never
    depended on the isolation level; the contract does.
    """

    from psycopg import sql

    with live_db.connection() as conn:
        # Not a hardcoded name: COLLAB_HUB_TEST_POSTGRES_URL may point anywhere,
        # and ALTER DATABASE takes an identifier, not a parameter.
        database_name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_isolation = 'serializable'").format(
                sql.Identifier(database_name)
            )
        )
    try:
        # A pool opened now; its connections pick the new default up.
        strict = _database()
        try:
            with strict.connection() as conn:
                level = conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
            assert level == "serializable", level

            service = PostgresInvitationService(strict)
            issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

            logins = [INVITEE, OTHER_INVITEE, "racer-3"]
            barrier = threading.Barrier(len(logins), timeout=30)
            real_audited = invitations_module.audited

            def barriered(*args, **kwargs):
                barrier.wait()
                return real_audited(*args, **kwargs)

            monkeypatch.setattr(invitations_module, "audited", barriered)

            outcomes: list[object] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def run(user_id: str):
                try:
                    result = _accept(service, user_id=user_id, secret=issued.raw_secret.reveal())
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    with lock:
                        errors.append(exc)
                else:
                    with lock:
                        outcomes.append(result)

            threads = [threading.Thread(target=run, args=(login,)) for login in logins]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            assert not any(thread.is_alive() for thread in threads)

            # Every loser got a *terminal state*, not a database error.
            assert len(outcomes) == 1
            assert len(errors) == len(logins) - 1
            assert all(isinstance(error, InvitationAlreadyUsedError) for error in errors), errors

            assert {row["id"] for row in _rows(strict, "SELECT id FROM collab_orgs")} == {
                ORG,
                OTHER_ORG,
                outcomes[0].org_id,
            }
            members = _rows(
                strict, "SELECT user_id FROM collab_org_members WHERE org_id = %s", (outcomes[0].org_id,)
            )
            assert len(members) == 1
            assert len([row for row in _audit_rows(strict) if row["action"] == "org.create"]) == 1
        finally:
            strict.close()
    finally:
        with live_db.connection() as conn:
            conn.execute(
                sql.SQL("ALTER DATABASE {} RESET default_transaction_isolation").format(
                    sql.Identifier(database_name)
                )
            )


@live_postgres
def test_live_a_revoke_that_commits_mid_acceptance_is_seen_as_revoked(service, live_db):
    """What the ``FOR UPDATE`` re-read buys, isolated from the other barriers.

    The guarded ``UPDATE ... WHERE status = 'pending'`` would also refuse this
    acceptance — but it would refuse it as *already used*, after having
    inserted an organization and a membership that then roll back. Reading the
    locked row first means every check runs against the committed truth, so a
    revocation that landed a millisecond earlier is reported as a revocation.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)

    def revoke(conn):
        conn.execute(
            "UPDATE collab_invitations SET status = 'revoked', revoked_at = now(), revoked_by = %s WHERE id = %s",
            (OPERATOR, issued.invitation.id),
        )

    outcomes, errors = _accept_while_locked(
        service, live_db, _Hold(issued.invitation.id, issued.raw_secret.reveal(), revoke)
    )

    assert outcomes == []
    assert len(errors) == 1
    assert isinstance(errors[0], InvitationRevokedError), errors[0]
    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,)) == []
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {ORG, OTHER_ORG}
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send"]


@live_postgres
def test_live_the_same_login_racing_itself_replays_rather_than_conflicting(service, live_db):
    """A double-submitted acceptance page, forced into the worst interleaving.

    Both attempts pass their pre-read while the invitation is pending, so the
    second one is inside its transaction when the first commits. Re-reading
    the *locked* row is what turns that into the same idempotent success:
    without it the second attempt would push on to the membership insert and
    fail its own login's primary key, answering ``already_in_organization`` to
    someone who had just been made an owner.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    winner_org = "org-first-submit"

    def accept_as_the_same_login(conn):
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (winner_org, INVITEE))
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
            (INVITEE, winner_org, ROLE_OWNER),
        )
        conn.execute(
            "UPDATE collab_invitations SET status = 'accepted', accepted_at = now(),"
            " accepted_by = %s, accepted_org_id = %s WHERE id = %s",
            (INVITEE, winner_org, issued.invitation.id),
        )

    outcomes, errors = _accept_while_locked(
        service, live_db, _Hold(issued.invitation.id, issued.raw_secret.reveal(), accept_as_the_same_login)
    )

    assert errors == [], errors
    assert len(outcomes) == 1
    assert outcomes[0].replay is True
    assert outcomes[0].org_id == winner_org
    assert len(_rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))) == 1
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {ORG, OTHER_ORG, winner_org}
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send"]


def _wait_until_expired(database, invitation_id: str, timeout: float = 20.0) -> None:
    """Block until the database's own clock is past the invitation's expiry.

    Polled against ``clock_timestamp()`` rather than slept, for the same
    reason the lock waits are: the test must depend on the server's clock, not
    on this machine's scheduler.
    """

    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        while not deadline.is_set():
            with database.connection() as conn:
                lapsed = conn.execute(
                    "SELECT clock_timestamp() > expires_at AS lapsed FROM collab_invitations WHERE id = %s",
                    (invitation_id,),
                ).fetchone()["lapsed"]
            if lapsed:
                return
        raise AssertionError(f"invitation {invitation_id} did not lapse within {timeout}s")
    finally:
        timer.cancel()


@live_postgres
def test_live_an_invitation_that_lapses_while_blocked_is_expired_not_accepted(service, live_db):
    """The expiry bypass: a token redeemed after it expired, via lock delay.

    PostgreSQL's ``now()`` is fixed at transaction start, and ``audited()``
    opens the transaction *before* the locked read. So an acceptance that
    blocks on ``FOR UPDATE`` — or later on a conflicting membership insert —
    keeps evaluating expiry against the instant its request began. Every
    check happened while the invitation was still live; by the time the
    consume ran, it was not. The window is exactly as long as contention
    lasts, and this suite's own concurrency tests hold that lock deliberately.

    Reproduced here at its widest: a short-lived invitation, the row pinned
    under another transaction's lock until it lapses, and only then released.
    The acceptance must resolve to *expired* — and must leave nothing behind,
    because an expiry discovered at the consume is still a rollback.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    with live_db.connection() as conn:
        conn.execute(
            "UPDATE collab_invitations SET expires_at = clock_timestamp() + interval '2 seconds' WHERE id = %s",
            (issued.invitation.id,),
        )

    outcomes: list[object] = []
    errors: list[BaseException] = []

    def accepter():
        try:
            outcomes.append(_accept(service, secret=issued.raw_secret.reveal()))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    holder = _database(max_size=2)
    try:
        with holder.connection() as conn:
            conn.execute(
                "SELECT id FROM collab_invitations WHERE id = %s FOR UPDATE",
                (issued.invitation.id,),
            )
            thread = threading.Thread(target=accepter)
            thread.start()
            # The acceptance is now inside its transaction, past its pre-read,
            # blocked on the lock — which is exactly the state in which its
            # transaction clock goes stale.
            _wait_until_blocked(live_db)
            _wait_until_expired(live_db, issued.invitation.id)
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        holder.close()

    assert outcomes == [], "an expired invitation was redeemed"
    assert len(errors) == 1
    assert isinstance(errors[0], InvitationExpiredError), errors[0]

    # Nothing survives the refusal, and the token is not consumed.
    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING
    assert _rows(live_db, "SELECT user_id FROM collab_org_members WHERE user_id = %s", (INVITEE,)) == []
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {ORG, OTHER_ORG}
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send"]


@live_postgres
def test_live_revoke_is_unaffected_by_the_transaction_clock(service, live_db):
    """Revoke compares no timestamps, and a lapsed invitation stays revocable.

    Stated as a test because "why was this not changed too" is the obvious
    next question: revoke branches on the stored status and guards its UPDATE
    on ``status = 'pending'``, so there is no deadline for a stale clock to
    misread — and retiring a lapsed link is a thing issuers legitimately do.
    """

    invitation = service.create(OWNER_CTX, email=INVITED_EMAIL, org_id=ORG).invitation
    _expire(live_db, invitation.id)

    revoked = service.revoke(OWNER_CTX, invitation.id, expect_org_id=ORG)
    assert revoked.status == STATUS_REVOKED
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send", "invitation.revoke"]


@live_postgres
def test_live_the_membership_primary_key_is_the_barrier_across_tokens(service, live_db):
    """The cross-token race, forced: the ``ON CONFLICT`` branch really runs.

    Two different tokens lock two different invitation rows, so they never
    meet on the invitation. What stops the second one is
    ``collab_org_members``'s primary key — and the whole transaction,
    including the organization inserted moments earlier, rolls back with it.
    Holding an uncommitted conflicting insert reproduces exactly that,
    on demand.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    failures: list[BaseException] = []
    results: list[object] = []

    def accepter():
        try:
            results.append(_accept(service, secret=issued.raw_secret.reveal()))
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            failures.append(exc)

    holder = _database(max_size=2)
    try:
        with holder.connection() as conn:
            # An uncommitted membership row for the same login: invisible to
            # the acceptance's pre-check, fatal to its insert.
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (INVITEE, OTHER_ORG, ROLE_MEMBER),
            )
            thread = threading.Thread(target=accepter)
            thread.start()
            _wait_until_blocked(live_db)
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        holder.close()

    assert results == []
    assert len(failures) == 1
    assert isinstance(failures[0], AlreadyInOrganizationError)
    (member,) = _rows(live_db, "SELECT org_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))
    assert member["org_id"] == OTHER_ORG
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {ORG, OTHER_ORG}
    assert _invitation_row(live_db, issued.invitation.id)["status"] == STATUS_PENDING
    assert [row["action"] for row in _audit_rows(live_db)] == ["invitation.send"]


@live_postgres
@pytest.mark.parametrize("accepters", [1, 2, 4])
def test_live_racing_accepts_of_one_token_yield_one_membership_and_one_org(service, live_db, monkeypatch, accepters):
    """The end-to-end claim, with real threads all the way through.

    A barrier immediately before the audited transaction guarantees every
    thread finished its pre-read while the invitation was still ``pending``,
    which is the interleaving that could produce two organizations. Run with
    one distinct login and with several, because the two lose differently:
    the same login replays, a different login is refused.
    """

    issued = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    logins = [INVITEE] + [f"racer-{index}" for index in range(accepters - 1)]
    barrier = threading.Barrier(len(logins), timeout=30)
    real_audited = invitations_module.audited

    def barriered(*args, **kwargs):
        barrier.wait()
        return real_audited(*args, **kwargs)

    monkeypatch.setattr(invitations_module, "audited", barriered)

    outcomes: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(user_id: str):
        try:
            outcome = _accept(service, user_id=user_id, secret=issued.raw_secret.reveal())
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            with lock:
                errors.append(exc)
        else:
            with lock:
                outcomes.append(outcome)

    threads = [threading.Thread(target=run, args=(login,)) for login in logins]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    # Exactly one acceptance took effect, whichever thread got there first.
    assert len(outcomes) == 1
    assert all(isinstance(error, InvitationAlreadyUsedError) for error in errors)
    assert len(errors) == len(logins) - 1

    org_ids = {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")}
    assert org_ids == {ORG, OTHER_ORG, outcomes[0].org_id}
    members = _rows(live_db, "SELECT user_id, org_id FROM collab_org_members WHERE org_id = %s", (outcomes[0].org_id,))
    assert len(members) == 1
    creates = [row for row in _audit_rows(live_db) if row["action"] == "org.create"]
    assert len(creates) == 1
    assert creates[0]["actor"] == members[0]["user_id"]


@live_postgres
def test_live_one_login_racing_two_tokens_ends_with_one_membership_and_one_org(service, live_db, monkeypatch):
    """The other race: same person, two invitations, both org-creating."""

    first = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    second = service.create(OPERATOR_CTX, email=INVITED_EMAIL, org_id=None)
    barrier = threading.Barrier(2, timeout=30)
    real_audited = invitations_module.audited

    def barriered(*args, **kwargs):
        barrier.wait()
        return real_audited(*args, **kwargs)

    monkeypatch.setattr(invitations_module, "audited", barriered)

    outcomes: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(secret: str):
        try:
            outcome = _accept(service, secret=secret)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            with lock:
                errors.append(exc)
        else:
            with lock:
                outcomes.append(outcome)

    threads = [
        threading.Thread(target=run, args=(first.raw_secret.reveal(),)),
        threading.Thread(target=run, args=(second.raw_secret.reveal(),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    assert len(outcomes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], AlreadyInOrganizationError)

    memberships = _rows(live_db, "SELECT user_id, org_id FROM collab_org_members WHERE user_id = %s", (INVITEE,))
    assert len(memberships) == 1
    assert memberships[0]["org_id"] == outcomes[0].org_id
    assert {row["id"] for row in _rows(live_db, "SELECT id FROM collab_orgs")} == {
        ORG,
        OTHER_ORG,
        outcomes[0].org_id,
    }
    # The loser's token was not consumed, and recorded nothing.
    statuses = {row["id"]: row["status"] for row in _rows(live_db, "SELECT id, status FROM collab_invitations")}
    assert sorted(statuses.values()) == [STATUS_ACCEPTED, STATUS_PENDING]
    assert len([row for row in _audit_rows(live_db) if row["action"] == "org.create"]) == 1


# --- Migration behavior ------------------------------------------------------


@live_postgres
def test_live_later_versions_apply_on_a_database_already_at_version_two():
    """Append-only, proven: a v2 database migrates forward without a rewrite.

    Named for the property rather than for a version number, so adding v5 does
    not require renaming the test that proves v2 still migrates.
    """

    database = _database(max_size=2)
    try:
        _drop_all(database)
        original = invitations_module.__dict__  # touched so the import is used
        del original
        # Migrate to v2 only, the way a previously-deployed pod left it.
        two_only = tuple(entry for entry in COLLAB_SCHEMA_MIGRATIONS if entry[0] <= 2)
        import collab_hub_api.frames.collab_schema as schema_module

        real = schema_module.COLLAB_SCHEMA_MIGRATIONS
        schema_module.COLLAB_SCHEMA_MIGRATIONS = two_only
        try:
            run_collab_schema_migrations(database)
        finally:
            schema_module.COLLAB_SCHEMA_MIGRATIONS = real

        with database.connection() as conn:
            assert conn.execute("SELECT to_regclass('collab_invitations') AS t").fetchone()["t"] is None

        run_collab_schema_migrations(database)

        with database.connection() as conn:
            assert conn.execute("SELECT to_regclass('collab_invitations') AS t").fetchone()["t"] is not None
            applied = conn.execute("SELECT version FROM collab_schema_migrations ORDER BY version").fetchall()
            # v5 (#180) replaces a constraint version 2 created, so this test
            # is also the proof that the replacement lands on a database that
            # really was left at v2 -- which is every deployed one.
            action_checks = conn.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'collab_audit_events'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%%action%%'
                """
            ).fetchall()
        # Taken from the list rather than spelled out, so the test earns its
        # name: appending a version does not edit this assertion.
        assert [row["version"] for row in applied] == [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
        assert [row["conname"] for row in action_checks] == ["collab_audit_events_action_check"], (
            "a mis-named DROP would leave the old constraint beside the new one, "
            "and the old one still refuses service_access.grant"
        )
        _drop_all(database)
    finally:
        database.close()


# --- Delivery ordering, and the secret's whole life --------------------------


class RecordingDelivery:
    """A delivery adapter that inspects the database as it is called."""

    def __init__(self, database):
        self._database = database
        self.calls: list[dict] = []
        self.committed_when_called: list[bool] = []

    def deliver(self, *, invitation_id, recipient, invitation_secret, organization_name, expires_at):
        with self._database.connection() as conn:
            visible = conn.execute(
                "SELECT count(*) AS n FROM collab_invitations WHERE id = %s",
                (invitation_id,),
            ).fetchone()["n"]
        self.committed_when_called.append(visible == 1)
        self.calls.append(
            {
                "invitation_id": invitation_id,
                "recipient": recipient,
                "secret": invitation_secret,
                "organization_name": organization_name,
                "expires_at": expires_at,
            }
        )
        return DeliveryOutcome(status=DELIVERY_PROVIDER_ACCEPTED, provider_message_id="msg-1")


@pytest_asyncio.fixture
async def live_client(tmp_path, monkeypatch):
    """The whole app against a live database, with delivery recorded."""

    _membership_env(monkeypatch)
    scratch = _database(max_size=4)
    _drop_all(scratch)
    config = _config(
        tmp_path,
        orgs={"backend": ""},
        postgres={"url": POSTGRES_URL, "auto_migrate": True},
    )
    app = make_app(config)
    async with app.router.lifespan_context(app):
        with scratch.connection() as conn:
            conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (OTHER_ORG, OPERATOR))
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (OWNER, ORG, ROLE_OWNER),
            )
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (MEMBER, ORG, ROLE_MEMBER),
            )
            conn.execute(
                "INSERT INTO collab_platform_roles (user_id, role) VALUES (%s, %s)",
                (OPERATOR, PLATFORM_ROLE_OPERATOR),
            )
        delivery = RecordingDelivery(scratch)
        app.state.invitation_email_delivery = delivery
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, scratch, delivery
    _drop_all(scratch)
    scratch.close()


@live_postgres
async def test_live_http_the_operator_can_bootstrap_the_first_organization(live_client, caplog):
    """The end-to-end payload of the whole track, on the hardened permutation.

    An operator who belongs to no organization sends one link; a login that
    belongs to nothing accepts it; an organization and its owner exist. The
    secret only ever travels through the email adapter and the accept body.
    """

    client, database, delivery = live_client
    caplog.set_level("DEBUG")

    created = await client.post(
        "/v1/operator/invitations",
        json={"email": INVITED_EMAIL},
        cookies=cookies_for(OPERATOR, email="ops@example.com"),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email"] == STORED_EMAIL
    assert body["org_id"] is None
    assert body["status"] == STATUS_PENDING
    assert body["delivery_status"] == DELIVERY_PROVIDER_ACCEPTED

    # The unrecoverable side effect ran after the transaction committed.
    assert delivery.committed_when_called == [True]
    secret = delivery.calls[0]["secret"]
    assert delivery.calls[0]["recipient"] == STORED_EMAIL
    assert delivery.calls[0]["organization_name"] is None

    # R3: not in the response, and not in any log record.
    assert secret not in created.text
    assert secret not in json.dumps(body)
    assert not any(secret in record.getMessage() for record in caplog.records)
    assert not any(secret in str(record.__dict__) for record in caplog.records)

    accepted = await client.post(
        "/v1/invitations/accept",
        json={"token": secret},
        cookies=cookies_for(INVITEE, email=INVITED_EMAIL),
    )
    assert accepted.status_code == 200, accepted.text
    outcome = accepted.json()
    assert outcome["role"] == ROLE_OWNER
    assert outcome["org_created"] is True
    assert secret not in accepted.text
    assert not any(secret in str(record.__dict__) for record in caplog.records)

    with database.connection() as conn:
        member = conn.execute(
            "SELECT org_id, role FROM collab_org_members WHERE user_id = %s", (INVITEE,)
        ).fetchone()
        name = conn.execute("SELECT name FROM collab_orgs WHERE id = %s", (outcome["org_id"],)).fetchone()["name"]
    assert member == {"org_id": outcome["org_id"], "role": ROLE_OWNER}
    assert name == NEUTRAL_ORG_NAME

    # And the new owner is now an ordinary org-scoped caller.
    listed = await client.get(f"/v1/orgs/{outcome['org_id']}/invitations", cookies=cookies_for(INVITEE))
    assert listed.status_code == 200
    assert listed.json() == {
        "invitations": [],
        "limit": invitations_router.DEFAULT_PAGE_SIZE,
        "offset": 0,
        "has_more": False,
    }


@live_postgres
async def test_live_http_an_owner_cannot_invite_into_another_organization(live_client):
    client, _database, delivery = live_client

    refused = await client.post(
        f"/v1/orgs/{OTHER_ORG}/invitations",
        json={"email": INVITED_EMAIL},
        cookies=cookies_for(OWNER, email="owner@example.com"),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == error_codes.FORBIDDEN
    assert delivery.calls == []

    allowed = await client.post(
        f"/v1/orgs/{ORG}/invitations",
        json={"email": INVITED_EMAIL},
        cookies=cookies_for(OWNER, email="owner@example.com"),
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["org_id"] == ORG
    # A join-this-org invitation names the organization in its email, so the
    # template cannot tell the invitee that accepting creates one.
    assert delivery.calls[0]["organization_name"] == NEUTRAL_ORG_NAME


@live_postgres
async def test_live_http_an_ordinary_member_and_an_operatorless_caller_are_refused(live_client):
    client, _database, _delivery = live_client

    for cookies in (cookies_for(MEMBER), cookies_for(OWNER)):
        response = await client.post(
            "/v1/operator/invitations",
            json={"email": INVITED_EMAIL},
            cookies=cookies,
        )
        assert response.status_code == 403, response.text

    member_attempt = await client.post(
        f"/v1/orgs/{ORG}/invitations",
        json={"email": INVITED_EMAIL},
        cookies=cookies_for(MEMBER),
    )
    assert member_attempt.status_code == 403

    # And an operator does not thereby become an owner of anybody's org.
    operator_attempt = await client.post(
        f"/v1/orgs/{ORG}/invitations",
        json={"email": INVITED_EMAIL},
        cookies=cookies_for(OPERATOR),
    )
    assert operator_attempt.status_code == 403


@live_postgres
@pytest.mark.parametrize(
    ("email", "verified", "expected_status", "expected_code"),
    [
        # A genuinely different address. The lowered spelling of the invited
        # one used to belong here and now redeems successfully (#157), so
        # keeping it would have tested the amendment rather than the code.
        ("someone-else@example.com", True, 403, error_codes.INVITATION_EMAIL_MISMATCH),
        (INVITED_EMAIL, False, 403, error_codes.EMAIL_NOT_VERIFIED),
        (None, True, 403, error_codes.EMAIL_NOT_VERIFIED),
    ],
)
async def test_live_http_claim_failures_carry_their_own_codes(
    live_client, email, verified, expected_status, expected_code
):
    client, database, delivery = live_client
    created = await client.post(
        "/v1/operator/invitations",
        json={"email": INVITED_EMAIL},
        cookies=cookies_for(OPERATOR),
    )
    assert created.status_code == 201
    secret = delivery.calls[0]["secret"]

    response = await client.post(
        "/v1/invitations/accept",
        json={"token": secret},
        cookies=cookies_for(INVITEE, email=email, email_verified=verified),
    )
    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code

    with database.connection() as conn:
        assert conn.execute("SELECT status FROM collab_invitations").fetchone()["status"] == STATUS_PENDING


@live_postgres
async def test_live_http_terminal_states_have_distinct_codes(live_client):
    client, database, delivery = live_client

    async def issue() -> str:
        response = await client.post(
            "/v1/operator/invitations", json={"email": INVITED_EMAIL}, cookies=cookies_for(OPERATOR)
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    expired_id = await issue()
    expired_secret = delivery.calls[-1]["secret"]
    with database.connection() as conn:
        conn.execute(
            "UPDATE collab_invitations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (expired_id,),
        )

    revoked_id = await issue()
    revoked_secret = delivery.calls[-1]["secret"]
    revoked = await client.post(
        f"/v1/operator/invitations/{revoked_id}/revoke", cookies=cookies_for(OPERATOR)
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == STATUS_REVOKED

    used_id = await issue()
    used_secret = delivery.calls[-1]["secret"]
    assert used_id
    first = await client.post(
        "/v1/invitations/accept",
        json={"token": used_secret},
        cookies=cookies_for(INVITEE, email=INVITED_EMAIL),
    )
    assert first.status_code == 200, first.text

    cases = {
        expired_secret: error_codes.INVITATION_EXPIRED,
        revoked_secret: error_codes.INVITATION_REVOKED,
        used_secret: error_codes.INVITATION_ALREADY_USED,
        mint_invitation_secret().raw.reveal(): error_codes.INVITATION_NOT_FOUND,
    }
    for secret, code in cases.items():
        response = await client.post(
            "/v1/invitations/accept",
            json={"token": secret},
            cookies=cookies_for(OTHER_INVITEE, email=INVITED_EMAIL),
        )
        assert response.json()["error"]["code"] == code, (secret, response.text)

    # And a login already bound elsewhere gets its own code.
    bound = await issue()
    assert bound
    response = await client.post(
        "/v1/invitations/accept",
        json={"token": delivery.calls[-1]["secret"]},
        cookies=cookies_for(INVITEE, email=INVITED_EMAIL),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == error_codes.ALREADY_IN_ORGANIZATION


@live_postgres
async def test_live_http_a_malformed_address_is_a_422_that_quotes_nothing(live_client):
    client, database, _delivery = live_client

    response = await client.post(
        "/v1/operator/invitations",
        json={"email": "Somebody <somebody@example.com>"},
        cookies=cookies_for(OPERATOR),
    )
    assert response.status_code == 422, response.text
    assert "somebody@example.com" not in response.text
    with database.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM collab_invitations").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Naming a placeholder organization (#44) — the first-invite flow's write half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored", [None, "", "  ", "Unnamed organization", "UNNAMED ORGANIZATION"])
def test_the_placeholder_predicate_matches_what_the_column_default_writes(stored):
    assert is_placeholder_organization_name(stored)


def test_the_placeholder_predicate_agrees_with_the_schema_constant():
    from collab_hub_api.frames.collab_schema import NEUTRAL_ORG_NAME

    assert is_placeholder_organization_name(NEUTRAL_ORG_NAME)
    assert not is_placeholder_organization_name("Acme Widgets")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "Unnamed organization",
        "unnamed ORGANIZATION",
        "a" * 121,
        "x\ny",
        "x\x00",
        "x\x7f",
        "Acme\u0085Widgets",  # C1 NEL: a line break to many renderers, not an ASCII control
        "Acme\u2028Widgets",  # LINE SEPARATOR
        "Acme\u2029Widgets",  # PARAGRAPH SEPARATOR
        "Acme\u202eWidgets",  # RIGHT-TO-LEFT OVERRIDE: renders as something other than itself
        "Acme\u200dWidgets",  # ZERO WIDTH JOINER
        "\u2800\u2800",  # Braille blanks: renders as nothing
        "\u0301\u0308",  # combining marks alone: nothing to combine with
        "---",  # punctuation alone
        "Unnamed\u00a0organization",  # the placeholder with a no-break space
        "Unnamed   organization",  # the placeholder with a run of spaces
        None,  # not a string at all
        42,  # nor is a number
    ],
)
def test_an_unusable_organization_name_is_refused(bad):
    with pytest.raises(ValueError):
        validate_organization_name(bad)


def test_a_usable_organization_name_has_its_spacing_normalized_and_is_otherwise_kept():
    assert validate_organization_name("  Acme  Widgets ") == "Acme Widgets"
    assert validate_organization_name("Acme\u00a0Widgets") == "Acme Widgets"
    assert validate_organization_name("a" * 120) == "a" * 120
    assert validate_organization_name("Ünïcode & Co.") == "Ünïcode & Co."
    assert validate_organization_name("株式会社テスト") == "株式会社テスト"
    assert validate_organization_name("42") == "42"


def test_naming_is_one_audited_read_then_write_and_refuses_without_writing(monkeypatch):
    """Non-live pin of the one-shot transaction body (the live tests own the
    real ``FOR UPDATE`` semantics): a missing organization refuses, a named
    one refuses **without writing**, and the placeholder case writes exactly
    one UPDATE, with the normalized name, on the audited connection it was
    handed — never a second checkout."""

    missing = object()

    class _Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _Conn:
        def __init__(self, current_name):
            self._current = current_name
            self.updates = []

        def execute(self, sql, params=()):
            if sql.lstrip().startswith("SELECT"):
                assert "FOR UPDATE" in sql
                return _Result(None if self._current is missing else {"name": self._current})
            self.updates.append(params)
            return _Result(None)

    class _Event:
        def __init__(self, conn):
            self.conn = conn

    audits = []

    class _Audited:
        def __init__(self, db, ctx, action, **kwargs):
            audits.append((action, kwargs))

        def __enter__(self):
            return _Event(conn)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(invitations_module, "audited", _Audited)
    service = PostgresInvitationService(object())

    conn = _Conn(missing)
    with pytest.raises(invitations_module.OrgNotFoundError):
        service.name_organization(OPERATOR_CTX, org_id=ORG, name="Acme Widgets")
    assert conn.updates == []

    conn = _Conn("Already Chosen")
    with pytest.raises(invitations_module.OrganizationAlreadyNamedError):
        service.name_organization(OPERATOR_CTX, org_id=ORG, name="Acme Widgets")
    assert conn.updates == []

    conn = _Conn("Unnamed organization")
    named = service.name_organization(OPERATOR_CTX, org_id=ORG, name="  Acme  Widgets ")
    assert named == "Acme Widgets"
    assert conn.updates == [("Acme Widgets", ORG)]
    action, kwargs = audits[-1]
    assert action == invitations_module.AUDIT_ACTION_ORG_RENAME
    assert kwargs["target_label"] == "Acme Widgets"
    assert kwargs["org_id"] == ORG


@live_postgres
def test_live_naming_writes_the_name_and_its_event_together(service, live_db):
    """The fixture seeds ORG the way acceptance really creates one: no name, so
    the column default applies and the organization is the placeholder."""

    assert service.organization_name(ORG) == "Unnamed organization"
    assert service.name_organization(OWNER_CTX, org_id=ORG, name="  Acme Widgets ") == "Acme Widgets"
    assert service.organization_name(ORG) == "Acme Widgets"
    (event,) = _audit_rows(live_db)
    assert (event["action"], event["actor"], event["target_type"], event["target_id"]) == (
        "org.rename",
        OWNER,
        "org",
        ORG,
    )
    assert event["target_label"] == "Acme Widgets"
    assert event["org_id"] == ORG
    assert event["detail"] == {"replaced_placeholder": True}


@live_postgres
def test_live_naming_is_one_shot_and_a_refusal_writes_no_row(service, live_db):
    service.name_organization(OWNER_CTX, org_id=ORG, name="Acme Widgets")
    with pytest.raises(OrganizationAlreadyNamedError):
        service.name_organization(OWNER_CTX, org_id=ORG, name="Acme Corp")
    assert service.organization_name(ORG) == "Acme Widgets"
    assert [row["action"] for row in _audit_rows(live_db)] == ["org.rename"]
    # The other organization is untouched: the pin is the org id, not the actor.
    assert service.organization_name(OTHER_ORG) == "Unnamed organization"


@live_postgres
def test_live_two_owners_naming_at_once_resolve_to_one_row_and_one_refusal(service, live_db):
    """The one-shot rule under real concurrency: the FOR UPDATE read inside the
    audited transaction serializes the two, and the loser re-reads the
    committed name rather than the placeholder it saw on its page."""

    import threading

    start = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def attempt(label: str, name: str) -> None:
        start.wait()
        try:
            outcomes[label] = service.name_organization(OWNER_CTX, org_id=ORG, name=name)
        except Exception as exc:  # noqa: BLE001 - the test classifies it below
            outcomes[label] = exc

    threads = [
        threading.Thread(target=attempt, args=("a", "Acme Widgets")),
        threading.Thread(target=attempt, args=("b", "Acme Corp")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)

    winners = [v for v in outcomes.values() if isinstance(v, str)]
    losers = [v for v in outcomes.values() if isinstance(v, OrganizationAlreadyNamedError)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert service.organization_name(ORG) == winners[0]
    events = _audit_rows(live_db)
    assert [(row["action"], row["target_label"]) for row in events] == [("org.rename", winners[0])]


@live_postgres
def test_live_naming_an_unknown_organization_is_not_found_and_writes_no_row(service, live_db):
    with pytest.raises(invitations_module.OrgNotFoundError):
        service.name_organization(OWNER_CTX, org_id="org-nope", name="Acme Widgets")
    assert _audit_rows(live_db) == []


@live_postgres
def test_live_a_placeholder_name_never_reaches_the_row(service, live_db):
    with pytest.raises(ValueError):
        service.name_organization(OWNER_CTX, org_id=ORG, name="unnamed organization")
    assert _audit_rows(live_db) == []
    assert service.organization_name(ORG) == "Unnamed organization"
