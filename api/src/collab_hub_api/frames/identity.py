"""ACL identity policy: which claim becomes the persisted principal, and what may be granted.

Legacy behavior resolves the stored owner identity from
``("preferred_username", "email", "sub")`` in order. Both fallbacks are unsafe
on a deployment with password accounts: ``preferred_username`` is user-chosen
and Keycloak lets it change, and ``email`` is self-asserted. The resolved
string is *persisted* in the six sites issue #61 names — which expand to nine
columns/fields — none of which has a rename path:

1. ``FrameMetadata.created_by`` — ``frames/models.py``, written by
   ``FrameStore.create_frame`` (``frames/store.py``).
2. ``FrameMetadata.owners`` — same models/store, plus ``set_owners``.
3. ``FrameMetadata.readers`` — same models/store, plus ``set_readers``.
4. ``Suggestion.submitted_by`` — ``frames/models.py``, written by
   ``FrameStore.add_suggestion`` (``frames/store.py``).
5. ``FrameGroup.created_by`` and ``FrameGroup.owners`` — ``frames/groups.py``
   (``created_by``/``owners`` columns of ``frames_server_frame_groups``).
6. ``frames_server_history.actor`` — ``frames/history.py``.

plus two primary keys built from the same string:

7. ``frames_server_active_frames (org_id, workspace_id, user_id)`` —
   ``frames/active_state.py``.
8. ``frames_server_usage_users (org_id, workspace_id, user_id)`` —
   ``frames/usage.py``.

and one more the issue's inventory does not name, found while writing this list:

9. ``frames_server_usage_events.user_id`` — ``frames/usage.py``. Not an ACL, but
   the same string, so an identity migration (#65) has to carry it too or the
   usage history splits in two.

So a username change silently orphans every frame that user owns, and there is
no in-place fix short of a data migration (issue #65). Pinning the principal to
the immutable ``sub`` removes the failure mode — but *flipping* the precedence
on a deployment that already stores email/username principals orphans that data
just as permanently, which is why the pin is a per-deployment choice with the
old behavior as its default, not a code-wide change.

``FRAMES_AUTH_IDENTITY_CLAIM`` selects the policy:

- unset or ``legacy`` — the historical precedence tuple. What every existing
  deployment keeps on upgrade, including the internal hub until its Gate D
  migration window (issue #65).
- ``sub`` — the verified ``sub`` claim is the only accepted identity, and ACL
  principals written *through the API* are subjects. Email-shaped grants are
  **rejected, not silently stored** (version-skew flag S1): a stored email
  principal matches no caller, so it looks like a successful share while
  granting nothing. For the external Collab deployment, which starts with no
  data.

**Subjects are opaque.** Nothing here infers identity from the *syntax* of a
``sub``. Keycloak mints dashed-hex UUIDs for realm-local accounts, composite
``f:<provider-id>:<external-id>`` values for federated-storage accounts, and
whatever an operator configures for service accounts — all equally valid, and a
token carrying any of them authenticates and has its subject persisted as
``created_by``. Validating a UUID shape on the grant path would therefore refuse
to grant to users the same deployment happily creates frames for, and would
refuse the very values the user directory hands the member picker
(``user_directory.py`` returns Keycloak's raw ``id``). The authoritative check
is **membership, not syntax** — "is this principal a member of the caller's
organization" — and it arrives with the scoped member list in
openteams-ai/collab-hub-pack#99. Until then the only thing rejected is the
mistake S1 names: a value that is an email address.

**Single-issuer assumption (review register R12).** A bare ``sub`` is unique
only *within* one issuer, so this deployment assumes exactly one trusted
issuer, and principals are therefore not issuer-qualified. That assumption is
load-bearing, not stylistic: the IdToken and bearer verifiers are configured
independently (``FRAMES_IDTOKEN_*`` / ``FRAMES_BEARER_*``), so a deployment
trusting two issuers would let a colliding ``sub`` minted by the second issuer
*be* the first issuer's user — read and manage. It is therefore enforced at
startup by :func:`enforce_single_issuer_for_pin`, not merely recorded. Trusting
a second issuer requires moving to ``(iss, sub)`` principals **first**, which is
itself a data migration.
"""

from __future__ import annotations

import os

IDENTITY_CLAIM_ENV = "FRAMES_AUTH_IDENTITY_CLAIM"

LEGACY_IDENTITY_CLAIM_PRECEDENCE = ("preferred_username", "email", "sub")
"""Claims tried, in order, when the pin is off. Mutable/self-asserted first."""

PINNED_IDENTITY_CLAIM = "sub"
"""The only claim consulted for the ACL principal when the pin is on."""

DISPLAY_CLAIMS = ("name", "preferred_username")
"""Claims tried, in order, for the caller's *display* name.

Deliberately disjoint from the principal: values resolved from these claims
reach :class:`~.auth.DisplayIdentity` and nothing else. See that class for why
the separation is structural rather than a convention.
"""


def identity_pinned_to_sub() -> bool:
    """Return whether the ACL principal is pinned to the OIDC ``sub`` claim.

    Unset means ``legacy``: an existing deployment upgrading to this build is
    unaffected until it opts in.

    An unrecognized value raises instead of falling back. Guessing is wrong in
    both directions — quietly choosing ``legacy`` would undo a pin an operator
    believes is on, and quietly choosing ``sub`` would strand a deployment whose
    stored principals are still emails. Matching is exact, with no trimming or
    case-folding, for the same reason: ``Legacy`` or ``" sub "`` is an
    unreviewed spelling of a security-relevant setting, and normalizing it would
    silently accept the operator's guess about how it is parsed.

    ``make_app`` calls this once at startup, so a typo fails the rollout rather
    than the first authenticated request.
    """

    mode = os.environ.get(IDENTITY_CLAIM_ENV, "")
    if mode in ("", "legacy"):
        return False
    if mode == PINNED_IDENTITY_CLAIM:
        return True
    raise RuntimeError(
        f"Unsupported {IDENTITY_CLAIM_ENV} value {mode!r}: expected exactly 'sub' "
        "(pin ACL principals to the OIDC subject) or 'legacy' "
        "(preferred_username/email/sub precedence, the default)."
    )


def enforce_single_issuer_for_pin() -> None:
    """Refuse to start pinned while trusting an unnamed or second issuer (R12).

    See the module docstring: a bare ``sub`` identifies a person only within one
    issuer, and the two verifiers are configured independently, so recording the
    assumption is not enough to make it true. Every *configured* verifier must
    name its expected issuer, and all of them must name the same one.

    The IdToken verifier is derived **exactly** as
    :func:`~.auth.decode_id_token_payload` derives it, because anything looser
    misses real configurations: that function falls back to the bearer JWKS URL
    *and*, independently, to the bearer issuer. So an IdToken verifier exists
    whenever either JWKS URL is set — including with ``FRAMES_IDTOKEN_JWKS_URL``
    unset — and it may carry its own ``FRAMES_IDTOKEN_ISSUER`` while reusing the
    shared JWKS. Checking only ``FRAMES_IDTOKEN_JWKS_URL`` would let exactly the
    dangerous case start: cookies verified against a shared key set under issuer
    B while bearer tokens use issuer A.

    Deployments with no verifier configured at all (local/test unsafe-auth
    modes) have no issuer to check and pass vacuously.

    No-op when the pin is off — legacy principals are already not ``sub`` values,
    so the collision this prevents does not arise.
    """

    if not identity_pinned_to_sub():
        return

    bearer_jwks = os.environ.get("FRAMES_BEARER_JWKS_URL", "").strip()
    bearer_issuer = os.environ.get("FRAMES_BEARER_ISSUER", "").strip()
    # Both fall back to the bearer setting, separately — mirroring the decode path.
    idtoken_jwks = os.environ.get("FRAMES_IDTOKEN_JWKS_URL", "").strip() or bearer_jwks
    idtoken_issuer = os.environ.get("FRAMES_IDTOKEN_ISSUER", "").strip() or bearer_issuer

    issuers: set[str] = set()
    if bearer_jwks:
        if not bearer_issuer:
            raise RuntimeError(
                f"{IDENTITY_CLAIM_ENV}=sub requires FRAMES_BEARER_ISSUER: a bare 'sub' is "
                "unique only within one issuer, so an unpinned issuer makes it an unsafe "
                "ACL principal."
            )
        issuers.add(bearer_issuer)
    if idtoken_jwks:
        if not idtoken_issuer:
            raise RuntimeError(
                f"{IDENTITY_CLAIM_ENV}=sub requires FRAMES_IDTOKEN_ISSUER (or the "
                "FRAMES_BEARER_ISSUER fallback): a bare 'sub' is unique only within one "
                "issuer, so an unpinned issuer makes it an unsafe ACL principal."
            )
        issuers.add(idtoken_issuer)
    if len(issuers) > 1:
        raise RuntimeError(
            f"{IDENTITY_CLAIM_ENV}=sub requires a single trusted issuer, got {sorted(issuers)!r}: "
            "'sub' values minted by different issuers can collide, so principals would have to "
            "be issuer-qualified ((iss, sub)) before a second issuer may be trusted."
        )


def looks_like_email(value: str) -> bool:
    """Return whether *value* is an email address rather than a subject id.

    Containing an ``@`` is the whole test, and deliberately so: no OIDC issuer
    mints a subject containing one, so this separates "the sharer typed an email
    address" from every legitimate subject format without pretending to know
    what a subject looks like.
    """

    return "@" in value


def validate_acl_principal(value: str) -> str:
    """Validate one to-be-written ACL principal under the active policy.

    Pass-through under ``legacy``. Under the pin, a grant naming an **email
    address** can never match a caller — identities are subjects now — so it is
    rejected with a message that says so. Silently storing it is the dangerous
    outcome (S1): the sharer sees a successful write that grants access to
    nobody.

    Everything else is accepted as an opaque subject. This is not laxness, it is
    correctness: Keycloak subjects are realm-local UUIDs, composite
    ``f:<provider>:<id>`` values for federated storage, or operator-defined
    strings for service accounts, and a token carrying any of them already
    authenticates here and gets its subject persisted as ``created_by``. A
    syntax check that refused those would reject as a *grant* the very value the
    same deployment writes as an *owner*, and would reject the ids the user
    directory feeds the member picker. Whether a principal is a real person on
    this deployment is a membership question, answered by the scoped member list
    in openteams-ai/collab-hub-pack#99 — not by syntax.

    **Request models only.** Stored-record models (``FrameMetadata``,
    ``FrameGroup``) must never call this: pre-pin records legitimately contain
    email/username principals and have to keep loading so their owners can see
    and repair them. The removal routes (``DELETE .../owners/{email}``,
    ``.../readers/{email}``) are exempt for the same reason — cleaning a legacy
    principal out must stay possible. Whoever adds the next grant-writing
    request model has to opt into this explicitly.
    """

    if identity_pinned_to_sub() and looks_like_email(value):
        raise ValueError(
            f"ACL principals on this deployment are OIDC subject ids; {value!r} is an email "
            "address and would grant access to no one. Pick people through the organization "
            "member picker instead."
        )
    return value


def validate_acl_principals(values: list[str]) -> list[str]:
    """Validate a list of to-be-written ACL principals (see :func:`validate_acl_principal`)."""

    for value in values:
        validate_acl_principal(value)
    return values
