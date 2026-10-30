"""The browser session cookie: a signed, expiring identity assertion.

Design decisions, stated once:

* **Stateless and signed, not a server-side session table.** The cookie's
  payload is JSON, HMAC-SHA256 signed with the deployment's
  ``web.session_secret``, so any replica can verify it with no shared store
  and no schema change (the ``collab_`` migrations are another issue's merge
  hot spot). The trade this buys is documented rather than hidden: the server
  cannot revoke an individual session before it expires — sign-out deletes
  the browser's copy, and a copy captured before sign-out keeps verifying
  until ``exp``. That is acceptable *here* because the cookie asserts only
  identity: every authorization decision (operator, org owner) is re-resolved
  from the server's own stores on each request (``web.authz``), so revoking a
  role locks the holder out immediately, valid session or not. The absolute
  lifetime is deliberately short (``web.session_lifetime_seconds``, default
  eight hours) and there is no sliding renewal.

* **The CSRF secret lives inside the session payload.** The cookie is
  HttpOnly, so page script (and any injected script) cannot read it; pages
  render the token into forms server-side and POST handlers compare it
  against the session's copy in constant time. A token is therefore valid for
  exactly one session and dies with it — nothing to store, nothing to rotate
  separately.

* **Purpose tags.** The session codec also signs the transient OIDC-flow
  cookie (state/nonce/PKCE verifier), and the two must never be mutually
  substitutable: a signature is computed over ``purpose || "." || payload``,
  so a transient blob presented as a session cookie (or vice versa) fails
  verification outright.

* **``__Host-`` cookie names, always ``Secure``.** ``__Host-`` makes the
  browser itself refuse the cookie unless it is Secure, Path=/, and set by
  this exact host — a subdomain or an http origin cannot plant one. There is
  deliberately no "insecure cookies for dev" switch: modern browsers treat
  ``localhost`` as a secure context and accept Secure cookies over plain http
  there, so the switch would only ever weaken a real deployment.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256

from fastapi import Response

SESSION_COOKIE = "__Host-nexus_web_session"
TRANSIENT_COOKIE = "__Host-nexus_web_oidc"

SESSION_PURPOSE = "nexus-web-session-v1"
TRANSIENT_PURPOSE = "nexus-web-oidc-v1"

TRANSIENT_LIFETIME_SECONDS = 600
"""How long a started sign-in may take to come back from Keycloak.

Long enough for a person to type a password and complete an OTP prompt;
short enough that an abandoned state/nonce/verifier triple is not sitting in
the browser for hours.
"""

CLOCK_SKEW_SECONDS = 60
"""Tolerance for an ``iat`` slightly in the future (replica clock drift)."""

MIN_SESSION_SECRET_LENGTH = 32
"""The signing secret must carry real entropy; a short one is refused at
startup rather than silently signing weakly (see ``config.WebConfig``)."""

VERIFIED_CLAIM_MAX_AGE_SECONDS = 300
"""How old the session's ``email``/``email_verified`` assertion may be when it
is used as an **authorization input**.

Identity in this cookie is stable — a subject does not stop being that
subject — which is why an eight-hour session is acceptable for it. The
verified-address pair is different in kind: it is a fact about the account at
the IdP *right now*, and it can be revoked there. Invitation acceptance is the
one place on this surface that decides something on it, and what it decides is
permanent (one organization per login, no change afterwards). An assertion up
to a whole session old could therefore bind a membership on a verification the
IdP had already withdrawn.

**Where verification is not required, half of this reasoning stops applying and
the gate stays.** On a deployment with
``frames.invitations.require_verified_email`` off (#190), acceptance no longer
reads ``email_verified`` as an authorization input at all, so nothing can bind a
membership on a withdrawn verification. What still matters is the ``email``
claim's currency: it *is* an authorization input in both modes -- Gate B's match
is not configurable -- and an address can be reassigned at the IdP just as a
verification can be revoked. So the bound is unchanged and its stricter
justification is simply unused there. Left as it is deliberately: a re-auth
round trip is a small cost, and loosening a window because one of two reasons
went away is how a bound stops meaning anything.

This surface cannot read the IdP at will — it holds no access or refresh
token, deliberately (``web.oidc``), and acquiring one to fix this would be a
larger regression than the problem. What it can do is re-run the
authorization-code flow, which makes Keycloak mint a **new ID token from the
current user record**. That is usually invisible: an active SSO session
satisfies it without prompting, and the claims still come out current, because
protocol mappers read the user at token-mint time rather than at sign-in.

This is the bound **measured against the deciding replica's own clock**. It is
not the real-time worst case, and the difference is not rounding — see
:data:`VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS`, which is the number any claim
about this should quote. Lower is safer; the only cost of lowering it is more
redirect round trips.
"""

VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS = VERIFIED_CLAIM_MAX_AGE_SECONDS + CLOCK_SKEW_SECONDS
"""The true worst-case age of an assertion this server will act on.

Derived rather than written down, so it cannot drift from the two numbers it
comes from.

The gap is :data:`CLOCK_SKEW_SECONDS`. The codec deliberately accepts an
``iat`` up to that far in the future, so a session minted by a replica running
at the skew limit carries an ``issued_at`` ahead of real time by that much,
and the age this module computes understates the real one by the same amount.
Everything still terminates — the clamp in
:func:`verified_claims_are_current` stops a future ``iat`` from meaning
"fresh forever" — but "no more than five minutes" was not true, and the
honest number is six.

Stated here, in the code, rather than only in a document: the next person to
reason about this will reason from the constant, and a constant that means
something narrower than its name is how a documented bound quietly becomes
wrong.
"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class WebSession:
    """The authenticated browser identity a verified session cookie asserts.

    ``user`` is the same ACL principal the rest of the server uses (chosen by
    ``frames.auth.user_from_claims`` at sign-in, so the deployment's identity
    pin applies to the web surface unchanged). ``name`` and ``email`` are
    display-only, exactly as on ``AuthContext.display`` — nothing here may be
    compared against an ACL except ``user``.

    Deliberately **no roles**. A role frozen into a cookie at sign-in outlives
    its revocation; the authorization helpers in ``web.authz`` resolve roles
    from the stores on every request instead.
    """

    user: str
    name: str | None
    email: str | None
    csrf: str
    issued_at: int
    expires_at: int
    email_verified: bool = False
    """Whether the IdP asserted this session's ``email`` is verified.

    Carried because invitation acceptance (#90) matches the invited address
    against the caller's *verified* claim, and the browser holds a session
    cookie rather than an ID token — so if this were not on the session, the
    acceptance page could not answer the one question that decides whether a
    stranger who knows the address may redeem the invitation.

    It rides next to ``email`` for the same reason
    :class:`~..frames.auth.DisplayIdentity` pairs them: an address without
    this flag is a self-asserted string, and reading one without the other is
    the mistake worth making structurally awkward.

    Defaults to ``False``, which is what a session cookie minted before this
    field existed decodes to — fail-closed, and the invitee is told to verify
    their address rather than being let through on a claim nobody made.
    """


class SessionCodec:
    """Signs and verifies the web surface's cookies.

    One codec per app, keyed by the configured ``web.session_secret``. The
    HMAC key is the SHA-256 of the secret so that key material handed to
    ``hmac`` has a fixed length regardless of how the secret was generated.
    """

    def __init__(self, secret: str) -> None:
        if len(secret) < MIN_SESSION_SECRET_LENGTH:
            raise ValueError(f"web session secret must be at least {MIN_SESSION_SECRET_LENGTH} characters")
        self._key = sha256(secret.encode()).digest()

    def _signature(self, purpose: str, payload: bytes) -> bytes:
        return hmac.new(self._key, purpose.encode() + b"." + payload, sha256).digest()

    def encode(self, purpose: str, data: dict) -> str:
        payload = json.dumps(data, separators=(",", ":")).encode()
        return f"{_b64encode(payload)}.{_b64encode(self._signature(purpose, payload))}"

    def decode(self, purpose: str, value: str, *, now: float | None = None) -> dict | None:
        """Verify and decode a cookie value, or ``None`` for anything else.

        ``None`` uniformly — a tampered signature, a foreign secret, a
        different purpose, an expired payload, and plain garbage are all the
        same answer, because every caller treats them the same way (no
        session; start over). Distinguishing them would only build an oracle.
        """

        if now is None:
            now = time.time()
        try:
            payload_b64, signature_b64 = value.split(".")
            payload = _b64decode(payload_b64)
            signature = _b64decode(signature_b64)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(signature, self._signature(purpose, payload)):
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        issued_at = data.get("iat")
        expires_at = data.get("exp")
        if not isinstance(issued_at, int) or not isinstance(expires_at, int):
            return None
        if issued_at > now + CLOCK_SKEW_SECONDS:
            return None
        if expires_at <= now:
            return None
        return data

    # --- session cookie ----------------------------------------------------

    def encode_session(self, session: WebSession) -> str:
        return self.encode(
            SESSION_PURPOSE,
            {
                "sub": session.user,
                "name": session.name,
                "email": session.email,
                "email_verified": session.email_verified,
                "csrf": session.csrf,
                "iat": session.issued_at,
                "exp": session.expires_at,
            },
        )

    def decode_session(self, value: str, *, now: float | None = None) -> WebSession | None:
        data = self.decode(SESSION_PURPOSE, value, now=now)
        if data is None:
            return None
        user = data.get("sub")
        csrf = data.get("csrf")
        if not isinstance(user, str) or not user or not isinstance(csrf, str) or not csrf:
            return None
        name = data.get("name")
        email = data.get("email")
        return WebSession(
            user=user,
            name=name if isinstance(name, str) else None,
            email=email if isinstance(email, str) else None,
            csrf=csrf,
            issued_at=data["iat"],
            expires_at=data["exp"],
            # `is True`, not truthiness, and for the same reason the claims
            # reader does it (frames.auth.display_identity_from_claims): the
            # string "false" is truthy, and a payload shape this decoder did
            # not mint must not be able to assert verification by accident.
            email_verified=data.get("email_verified") is True,
        )


def set_session_cookie(response: Response, value: str, *, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def set_transient_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        TRANSIENT_COOKIE,
        value,
        max_age=TRANSIENT_LIFETIME_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_transient_cookie(response: Response) -> None:
    response.delete_cookie(TRANSIENT_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def verified_claims_are_current(session: WebSession, *, now: float | None = None) -> bool:
    """Whether this session's verified-address assertion may still be acted on.

    ``issued_at`` is when the ID token those claims came from was verified, so
    it *is* the assertion's age; no second timestamp is minted, because a
    second timestamp is a second thing that can be wrong.

    Clamped at zero rather than trusting the subtraction: the codec already
    refuses an ``iat`` beyond the skew allowance, but a replica an instant
    ahead would otherwise produce a negative age, and "negative age" must not
    be a way to be fresh forever if this ever moves somewhere the codec's
    check does not run.

    What this answers is "at most :data:`VERIFIED_CLAIM_MAX_AGE_SECONDS` old
    by this replica's clock". In real time the answer is bounded by
    :data:`VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS`, and that is the number to
    quote when describing the guarantee.
    """

    if now is None:
        now = time.time()
    return max(0.0, now - session.issued_at) <= VERIFIED_CLAIM_MAX_AGE_SECONDS


def csrf_token_matches(session: WebSession, presented: str) -> bool:
    """Constant-time comparison of a presented CSRF token with the session's.

    Bytes, not str: ``compare_digest`` raises ``TypeError`` on non-ASCII
    strings, and ``presented`` arrives from a form field — a wrong token must
    be a refusal, never an exception.
    """

    return bool(presented) and hmac.compare_digest(presented.encode(), session.csrf.encode())
