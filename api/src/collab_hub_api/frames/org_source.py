"""Where the auth choke point gets the caller's organization: claims, membership, single, or self_service.

``auth_context_from_claims`` historically read ``org_id``/``workspace_id`` from
the token, with ``FRAMES_AUTH_DEFAULT_ORG``/``FRAMES_AUTH_DEFAULT_WORKSPACE``
as fallbacks. On a deployment where Keycloak mints no org claim — which is
every deployment today — that fallback is what every caller actually gets, so
**every user collapses into one organization and ``internal`` means "everyone
on the server"**. Retiring it is issue #63.

``FRAMES_AUTH_ORG_SOURCE`` selects the source:

- ``claims`` (or unset) — the historical behavior, unchanged byte for byte.
  What every existing deployment keeps on upgrade.
- ``membership`` — the server owns the org model: the caller's one
  ``collab_org_members`` row decides ``(org_id, role)``, ``workspace_id`` is
  the constant ``"default"``, org claims in the token are ignored entirely, and
  the ``FRAMES_AUTH_DEFAULT_*`` fallbacks are refused rather than ignored.
- ``single`` (issue #91) — membership resolution plus one declared difference:
  the deployment states that it hosts exactly one organization, and a caller
  with **no membership row at all** whose sign-in arrived through a *declared
  identity source* is auto-admitted — a real ``collab_org_members`` row is
  written on their first authenticated request. Everything else is byte for
  byte membership mode: rows stay the single source of truth, existing rows
  (active or removed, any organization) are authoritative and never rewritten,
  and every membership-mode precondition still applies. This is the mode's
  entire delta; it replaces the retired defaults, it does not revive them.
- ``self_service`` (issue #365) — membership resolution plus a different
  delta: a caller with **no membership row at all** whose token asserts a
  verified email gets **an organization of their own**, with themselves as
  ``owner``, created on their first authenticated request. It is the mode for
  a hub where anyone may sign up because usage is paid for per person: the
  no-organization state stops being a dead end. As under ``single``, rows stay
  the single source of truth and an existing row of any kind is never
  rewritten here (moving a person out of their own organization is
  invitation acceptance's job, not resolution's).

**Why auto-admission is gated on a declared identity source, not on
authentication.** "Every authenticated user is a member" admits every door
into the realm equally: federated sign-in (constrained by the provider's
domain policy), self-registration (constrained by ``registrationAllowed``),
and accounts an administrator created by hand — constrained by nothing, and
the mechanism by which one-off test accounts exist. Gating on the token's
``identity_provider`` claim makes the boundary the *declared provider's*
membership policy: a caller federated from a listed provider is admitted, and
any other login authenticates fine but resolves to ``no_organization`` until
membership is granted deliberately. The declaration is also checkable — the
alias either exists on the realm or it does not — where "anyone who can
authenticate" silently widens every time an account is created.

**Why this is a separate switch from the identity pin.** It would have been
shorter to hang membership resolution off ``FRAMES_AUTH_IDENTITY_CLAIM=sub``
(issue #61), since membership rows are keyed by the subject and the pin is
therefore a hard precondition of this mode. It is deliberately not done that
way: the internal hub's migration has to pin identity *first*, backfill
membership rows against the resulting subjects, verify coverage, and only then
retire the fallback. Fused into one switch, that sequence has no middle state
to stop in — the flip would strand every user whose membership row had not
been written yet. Two switches, one of which is a precondition of the other,
keeps the two steps independently reversible.

The parsing contract mirrors ``frames.identity``: exact match, no trimming or
case folding, and an unrecognized value fails startup instead of guessing.
Guessing is wrong in every direction here — quietly choosing ``claims`` would
resurrect the default-org fallback on the external deployment, quietly
choosing ``membership`` would lock out every user of a hub that has no
membership rows, and quietly choosing ``single`` would start admitting realm
accounts to an organization nobody declared.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .identity import IDENTITY_CLAIM_ENV, identity_pinned_to_sub

ORG_SOURCE_ENV = "FRAMES_AUTH_ORG_SOURCE"

ORG_SOURCE_CLAIMS = "claims"
ORG_SOURCE_MEMBERSHIP = "membership"
ORG_SOURCE_SINGLE = "single"
ORG_SOURCE_SELF_SERVICE = "self_service"

DEFAULT_ORG_ENV = "FRAMES_AUTH_DEFAULT_ORG"
DEFAULT_WORKSPACE_ENV = "FRAMES_AUTH_DEFAULT_WORKSPACE"
RETIRED_DEFAULT_ENVS = (DEFAULT_ORG_ENV, DEFAULT_WORKSPACE_ENV)
"""The fallbacks membership resolution retires. Refused at startup, not ignored."""

SINGLE_ORG_ID_ENV = "FRAMES_AUTH_SINGLE_ORG_ID"
SINGLE_ORG_NAME_ENV = "FRAMES_AUTH_SINGLE_ORG_NAME"
SINGLE_ORG_MEMBER_SOURCES_ENV = "FRAMES_AUTH_SINGLE_ORG_MEMBER_SOURCES"
"""Comma-separated identity-provider aliases whose sign-ins are auto-admitted.

Matched exactly (no trimming beyond the commas, no case folding — Keycloak
provider aliases are case-sensitive) against the token's
:data:`IDENTITY_PROVIDER_CLAIM`. There is deliberately no wildcard: "every
provider" is "every door into the realm", which is the shape this gate exists
to refuse.
"""

IDENTITY_PROVIDER_CLAIM = "identity_provider"
"""The claim naming which identity provider a session was brokered through.

Keycloak records the broker alias in the ``identity_provider`` user session
note; a "User Session Note" protocol mapper surfaces it as this claim on the
tokens the session mints. A token without the claim — a local realm account,
or a realm missing the mapper — is simply never auto-admitted: absence fails
closed to ``no_organization``, exactly like an undeclared provider.
"""


@dataclass(frozen=True)
class SingleOrgDeclaration:
    """What ``orgSource=single`` declares: one organization, and who may join it.

    ``org_id`` is load-bearing — membership rows key on it, and changing it
    later starts admitting new sign-ins to a *different* organization while
    every existing row stays where it was. ``org_name`` is display-only and is
    used only if the organization row does not exist yet; it never renames an
    existing row (renames are the audited ``org.rename``, a deliberate act).
    """

    org_id: str
    org_name: str
    member_sources: frozenset[str]


def _org_source() -> str:
    """Parse ``FRAMES_AUTH_ORG_SOURCE`` exactly, failing loud on anything else."""

    mode = os.environ.get(ORG_SOURCE_ENV, "")
    if mode in ("", ORG_SOURCE_CLAIMS):
        return ORG_SOURCE_CLAIMS
    if mode in (ORG_SOURCE_MEMBERSHIP, ORG_SOURCE_SINGLE, ORG_SOURCE_SELF_SERVICE):
        return mode
    raise RuntimeError(
        f"Unsupported {ORG_SOURCE_ENV} value {mode!r}: expected exactly "
        f"'{ORG_SOURCE_MEMBERSHIP}' (resolve the caller's organization from collab_org_members), "
        f"'{ORG_SOURCE_SINGLE}' (membership resolution plus auto-admission to one declared "
        f"organization for sign-ins from declared identity sources), "
        f"'{ORG_SOURCE_SELF_SERVICE}' (membership resolution plus an organization of their own "
        f"for every verified login without one), "
        f"or '{ORG_SOURCE_CLAIMS}' (org/workspace token claims with the "
        f"{DEFAULT_ORG_ENV}/{DEFAULT_WORKSPACE_ENV} fallbacks, the default)."
    )


def org_source_resolves_membership() -> bool:
    """Return whether org context is resolved from ``collab_org_members``.

    True for ``membership``, ``single`` and ``self_service`` — the latter two
    *are* membership resolution, differing only in how a missing row can come
    to exist. Everything conditioned on this — mounting invitations, requiring an
    organization store, the schema preflight, the ``collab_`` role axes —
    wants exactly that property, not the literal value.

    ``make_app`` calls this once at startup so a mistyped value fails the
    rollout rather than the first authenticated request.
    """

    return _org_source() != ORG_SOURCE_CLAIMS


def org_source_is_single() -> bool:
    """Return whether this deployment declares exactly one organization."""

    return _org_source() == ORG_SOURCE_SINGLE


def org_source_is_self_service() -> bool:
    """Return whether every verified login without a membership gets its own organization."""

    return _org_source() == ORG_SOURCE_SELF_SERVICE


def single_org_declaration() -> SingleOrgDeclaration | None:
    """The declared organization and its member sources, or ``None`` off ``single``.

    Raises rather than degrading on a malformed declaration: an empty id would
    write membership rows against an organization that cannot be addressed, an
    empty source list would make the mode a silent no-op that strands every
    user at ``no_organization`` anyway, and a ``*`` entry would be read by a
    person as a wildcard that the exact-match contract does not implement.
    ``make_app`` calls this at startup (via the precondition check below), so
    all of it fails the rollout, not the first sign-in.
    """

    if not org_source_is_single():
        return None

    org_id = os.environ.get(SINGLE_ORG_ID_ENV, "").strip()
    if not org_id:
        raise RuntimeError(
            f"{ORG_SOURCE_ENV}={ORG_SOURCE_SINGLE} requires {SINGLE_ORG_ID_ENV}: the mode is a "
            "declaration that this hub hosts exactly one organization, and a declaration names it."
        )
    org_name = os.environ.get(SINGLE_ORG_NAME_ENV, "").strip()
    if not org_name:
        raise RuntimeError(
            f"{ORG_SOURCE_ENV}={ORG_SOURCE_SINGLE} requires {SINGLE_ORG_NAME_ENV}: the name is "
            "used if the organization row is created, and 'Unnamed organization' on every "
            "member's screen is a misconfiguration nobody reports."
        )
    raw_sources = os.environ.get(SINGLE_ORG_MEMBER_SOURCES_ENV, "")
    sources = frozenset(entry.strip() for entry in raw_sources.split(",") if entry.strip())
    if not sources:
        raise RuntimeError(
            f"{ORG_SOURCE_ENV}={ORG_SOURCE_SINGLE} requires {SINGLE_ORG_MEMBER_SOURCES_ENV}: "
            "auto-admission is gated on the identity-provider alias a sign-in arrived through, "
            "so the deployment must name at least one. 'Every authenticated user' is not an "
            "option, deliberately — it admits hand-created realm accounts, the one door with "
            "no policy on it."
        )
    if "*" in sources:
        raise RuntimeError(
            f"{SINGLE_ORG_MEMBER_SOURCES_ENV} does not support '*': provider aliases are matched "
            "exactly, and a wildcard would be 'every door into the realm' — the shape this gate "
            "exists to refuse. Name the identity-provider alias(es)."
        )
    return SingleOrgDeclaration(org_id=org_id, org_name=org_name, member_sources=sources)


def enforce_membership_org_source_preconditions() -> None:
    """Refuse to start membership resolution with an unsafe surrounding config.

    Applies to every membership-resolving source (``membership``, ``single``
    and ``self_service`` — the latter two are the first plus a way for a
    missing row to come to exist, so they inherit every precondition). Checked from ``make_app``:

    1. **The identity pin must be on.** ``collab_org_members.user_id`` is the
       OIDC subject, so resolving a membership for a legacy
       ``preferred_username``/``email`` principal would look up the wrong key —
       denying everyone quietly, or worse, matching whoever happens to own a
       colliding value.
    2. **The retired fallbacks must be unset, not merely unread.** Nothing
       reads them under membership resolution, so a leftover value is invisible
       right up until someone flips the source back to ``claims`` during an
       incident and every user silently collapses into one organization again.
       A retirement that is only "ignored" is not a retirement.
    3. **A ``single`` declaration must be complete and well-formed** —
       :func:`single_org_declaration` raises on a missing organization id or
       name, an empty member-source list, and a ``*`` entry.

    The remaining precondition — an organization store with a real backend —
    needs the built store object and so lives in ``make_app`` itself.
    """

    if not org_source_resolves_membership():
        return

    source = _org_source()
    if not identity_pinned_to_sub():
        raise RuntimeError(
            f"{ORG_SOURCE_ENV}={source} requires {IDENTITY_CLAIM_ENV}=sub: "
            "membership rows are keyed by the OIDC subject, so a legacy "
            "(preferred_username/email) principal would be resolved against the wrong key."
        )
    for retired in RETIRED_DEFAULT_ENVS:
        if os.environ.get(retired, "").strip():
            raise RuntimeError(
                f"{retired} must be unset when {ORG_SOURCE_ENV}={source}: the "
                "default-org fallback is retired on membership-resolving deployments. Left set, "
                "it would spring back the moment the source is flipped to "
                f"'{ORG_SOURCE_CLAIMS}', collapsing every user into one organization where "
                "'internal' means everyone on the server."
            )
    single_org_declaration()
