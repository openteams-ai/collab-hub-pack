"""Machine-readable error codes carried by the frames API error envelope.

Every non-2xx response shaped by :func:`routers.frames.error_response` carries
``{"error": {"code": ..., "message": ...}}``. Clients branch on ``code`` and
never on ``message`` — the desktop's ``frontend/src/api/hubaccess.ts`` names
this module by path — so these strings are **wire contract**: never rename one,
and register a new one here before returning it so the spelling is decided once
and reused everywhere.

Scope: the codes emitted by the app-level handlers in :func:`core.make_app`,
the path-protection middleware, and the auth choke point. Router-local codes
(``frame_not_found``, ``groups_unavailable``, …) are still spelled at their
handlers in ``routers/``; moving them here is a mechanical follow-up, not part
of this module's contract.
"""

from __future__ import annotations

# --- Generic HTTP mappings (core.make_app's HTTPException handler) -----------
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
HTTP_ERROR = "http_error"
VALIDATION_ERROR = "validation_error"

# --- Store/state conditions --------------------------------------------------
FRAME_UPDATE_CONFLICT = "frame_update_conflict"

FRAME_DECODE_ERROR = "frame_decode_error"
"""A stored Frame's bytes could not be decoded into a valid model (HTTP 500).

The object exists but is corrupt or schema-incompatible, so this is emphatically
**not** ``frame_not_found``: surfacing it as a 404 would tell a client the Frame
is gone when it is actually present and unreadable. The pre-refactor behaviour
was an unstructured 500 leaking a raw ``JSONDecodeError``/``ValidationError``;
this code only gives that 500 a structured envelope.
"""

DATABASE_UNAVAILABLE = "database_unavailable"
"""A configured Postgres backend is unreachable, or its pool is saturated."""

ORGANIZATIONS_UNAVAILABLE = "organizations_unavailable"
"""The organization store has no backend at all (fail-closed 503).

Distinct from :data:`DATABASE_UNAVAILABLE`, which means "configured but
unreachable". This one is a deployment bug: ``make_app`` refuses to start a
membership-resolving deployment without an organization backend, so seeing it
at runtime means app state was assembled outside ``make_app``.
"""

# --- Organization membership (issue #63) -------------------------------------
NO_ORGANIZATION = "no_organization"
"""Authenticated, but not an active member of any organization (HTTP 403).

Deliberately **not** a 401. The desktop maps 401 to "sign in", which is useless
advice to a pending invitee or a removed member who *is* signed in — it
produces a re-login loop that cannot succeed. apollo-desktop#637 already
recognises this exact envelope (403 plus this code) and renders a fixed
explanatory state instead of an error.
"""

ALREADY_IN_ORGANIZATION = "already_in_organization"
"""Invitation acceptance refused: this login already has a membership row (409).

One home organization per login (``collab_org_members.user_id`` is the primary
key), so acceptance fails for a login that already has one — active *or*
removed, in any organization, including the invitation's own target. Reserved
by issue #63 and returned by issue #89's accept endpoint.
"""

# --- Invitations (issue #89) --------------------------------------------------
#
# The invitation lifecycle's terminal states. Each is a distinct outcome the
# acceptance page renders differently, which is why they are separate codes
# rather than one "invitation_unusable": "ask for a new link" and "sign in
# with the other address" are different instructions to a person.

INVITATION_NOT_FOUND = "invitation_not_found"
"""No invitation matches this token or id (404).

Also the answer for an invitation that exists but lies outside the caller's
authority, so probing the management surface cannot enumerate other
organizations' invitations.
"""

INVITATION_EXPIRED = "invitation_expired"
"""The invitation lapsed before it was accepted (410)."""

INVITATION_REVOKED = "invitation_revoked"
"""The invitation was revoked by its issuer or an operator (410)."""

INVITATION_ALREADY_USED = "invitation_already_used"
"""Single-use replay: the token was already redeemed by another login (410).

The same login redeeming its own token again is **not** this — that is an
idempotent success, so a reloaded acceptance page does not strand its user.
"""

EMAIL_NOT_VERIFIED = "email_not_verified"
"""The caller's token carries no usable verified email claim (403).

Covers ``email_verified`` absent, false, or non-boolean and ``email`` absent
or unusable, deliberately as one state: the differences are IdP configuration,
not something the invitee can act on differently.

Since #190 this code also covers "the signed-in account carried no usable email
address". That is the *only* way it is reachable where
``frames.invitations.require_verified_email`` is off -- and it is reachable on a
**strict** deployment too, when a client's scopes omit ``email``. The code stays
one value so the desktop app and the acceptance page keep one state to handle;
the message distinguishes the two conditions, and the page copy offers a remedy
for each.
"""

INVITATION_EMAIL_MISMATCH = "invitation_email_mismatch"
"""The caller's verified email is not the invited address (403).

Gate B chose **exact** match (2026-08-03), so this is reachable for addresses
that differ only in case. Does not consume the invitation.
"""

ORGANIZATION_CREATION_REFUSED = "organization_creation_refused"
"""An org-creating invitation on a single-organization deployment (409).

`frames.auth.orgSource=single` declares that the hub hosts exactly one
organization; an invitation with no `org_id` would mint another on
acceptance. Raised at issuance and, for invitations issued before the
deployment flipped to `single`, at acceptance — where it consumes nothing.
"""

INVITATIONS_UNAVAILABLE = "invitations_unavailable"
"""The invitation service has no Postgres backend on this deployment (503).

Distinct from :data:`DATABASE_UNAVAILABLE`: nothing is unreachable, the
feature is simply not configured. Its guarantees are the row lock, the
primary key, and the transaction, so there is no in-memory backend to fall
back to.
"""

# --- Membership API for add-on services (issue #270) --------------------------

ORGANIZATION_NOT_FOUND = "organization_not_found"
"""``GET /v1/membership/organizations/{org_id}`` names no organization (404).

An organization whose members were all removed still exists and answers 200
with an empty roster; this code means the id was never an organization here.
"""
