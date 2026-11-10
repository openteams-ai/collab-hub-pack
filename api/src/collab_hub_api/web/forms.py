"""Shared form handling for the surface's server-rendered management pages.

Lifted out of ``routers.admin`` when the owner invitation page (#142) became
the second page with the same POST shape: an urlencoded form, read under a
byte cap, with the CSRF comparison run as a predicate so a refusal can be
answered with the page itself. The byte-counting primitives were already
shared (:mod:`.request_limits`, extracted for the same reason one page
earlier); this module is the parsing-and-checking layer above them, moved
here before a second copy could drift from the first.

Everything here keeps the properties ``routers.admin`` argued for, and that
module's docstring remains the long-form argument:

* the body is refused above the cap **by counting what arrives**, never from
  ``Content-Length`` alone;
* ``request.form()`` is never called — the urlencoded fields are parsed from
  the already-bounded bytes, so Starlette's unbounded form parse is not on
  any path through this module;
* a refusal issued before the body was read must close the connection
  (:func:`~.request_limits.connection_close_headers`), and
  :class:`FormRefused` exists so no refusal can be added that forgets it.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

from fastapi import Request

from .pages import escape, render_page
from .request_limits import bounded_body, declares_oversize
from .session import WebSession, csrf_token_matches

logger = logging.getLogger("frames_server.web")

__all__ = [
    "FORM_CONTENT_TYPE",
    "MULTIPART_CONTENT_TYPE",
    "MAX_FORM_BYTES",
    "REQUEST_TOO_LARGE",
    "UNSUPPORTED_MEDIA_TYPE",
    "FormRefused",
    "csrf_ok",
    "form_field",
    "form_fields",
    "media_type",
    "refused_form_page",
]

REQUEST_TOO_LARGE = 413
"""Spelled as the number because ``starlette.status`` renamed this constant
and the old spelling now emits a ``DeprecationWarning`` on every use."""

UNSUPPORTED_MEDIA_TYPE = 415
"""Same reason as above: spelled as the number."""

MAX_FORM_BYTES = 4096
"""The cap on what a management-page ``POST`` will read, enforced by counting.

The legitimate content is a CSRF token plus one bounded address or invitation
id — a few hundred bytes. Enforced in :func:`~.request_limits.bounded_body`
rather than from ``Content-Length``; see that module for why the header
cannot be the gate.
"""

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
"""The only body type these pages accept.

Gated **before** the body is read, and deliberately excluding
``multipart/form-data``: multipart is the shape whose parsing cost is not
bounded by the byte count alone, and nothing on these pages uploads anything.
"""

MULTIPART_CONTENT_TYPE = "multipart/form-data"
"""The form shape this module refuses, spelled once.

:func:`form_fields` needs no branch on it — anything that is not
:data:`FORM_CONTENT_TYPE` is refused the same way — but
:func:`~.authz.require_csrf` does: it must recognize "the request claims a
form body" before deciding to read one, and a second spelling of this string
there is a second spelling that can drift.
"""


def media_type(request: Request) -> str:
    """The request's declared media type: ``Content-Type`` minus its parameters.

    Split on the first ``;``, stripped, and lowercased, so
    ``application/x-www-form-urlencoded; charset=UTF-8`` and the same type in
    capitals both compare equal to :data:`FORM_CONTENT_TYPE` (media types are
    case-insensitive), while a type that merely *begins* with it —
    ``application/x-www-form-urlencoded-not-really`` — does not (#71). Spelled
    once here for every management page, for :func:`~.authz.require_csrf`'s
    form fallback, and for the invitation redemption endpoint's JSON gate.
    """

    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class FormRefused(Exception):
    """The body was refused before it was read. Carries the status to answer.

    An exception rather than a sentinel return because every one of these
    outcomes shares the same obligation — the body was *not* consumed, so the
    response must close the connection — and routing them through one raise
    means a new refusal cannot be added that forgets it.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"request body refused with {status_code}")
        self.status_code = status_code


async def form_fields(request: Request, *, max_bytes: int = MAX_FORM_BYTES) -> dict[str, str]:
    """The urlencoded fields, read under the cap and parsed from those bytes.

    Deliberately **not** ``request.form()``. Starlette's form parse reads the
    body to completion with no bound of its own, so calling it is the very
    thing the cap exists to prevent; parsing the bounded bytes here keeps one
    read, under one limit, on every page that posts a form. It is also the
    read :func:`~.authz.require_csrf`'s form fallback goes through since
    #119, so the shared dependency and the pages that parse their own forms
    enforce one bound rather than two that can drift.

    Raises :class:`FormRefused` for a body the page will not read. The order
    is load-bearing: the content type is checked before anything is read, the
    declared size before that read starts, and the counted size during it.
    """

    if media_type(request) != FORM_CONTENT_TYPE:
        # An exact compare, never a prefix match (#71). Multipart in
        # particular: its parsing cost is not bounded by the byte count
        # alone, and nothing on these pages uploads anything.
        raise FormRefused(UNSUPPORTED_MEDIA_TYPE)
    if declares_oversize(request, max_bytes=max_bytes):
        raise FormRefused(REQUEST_TOO_LARGE)
    raw = await bounded_body(request, max_bytes=max_bytes)
    if raw is None:
        raise FormRefused(REQUEST_TOO_LARGE)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Not a form this page can read. Same answer as a malformed one: the
        # CSRF check fails closed on the empty mapping.
        return {}
    # `keep_blank_values` so an empty field is present-and-empty rather than
    # absent; `strict_parsing=False` so a malformed pair degrades to a missing
    # field rather than an exception on the request path.
    return {name: value for name, value in parse_qsl(decoded, keep_blank_values=True)}


def form_field(fields: dict[str, str], name: str, *, max_length: int) -> str:
    """One bounded field, or the empty string.

    Bounded here rather than trusted from the ``maxlength`` attribute, which is
    a hint to a browser and nothing at all to anything else.
    """

    value = fields.get(name)
    if not isinstance(value, str) or len(value) > max_length:
        return ""
    return value.strip()


def csrf_ok(request: Request, fields: dict[str, str], session: WebSession, *, page: str) -> bool:
    """The surface's CSRF check, as a predicate over already-parsed fields.

    ``require_csrf`` raises :class:`~.authz.WebForbidden`, which is right for
    a page with nothing else to say. The management pages have something to
    say — they answer with the page — so the check runs here and the refusal
    is rendered. The comparison is :func:`~.session.csrf_token_matches`: the
    same constant-time comparison against the same secret inside the signed,
    HttpOnly session cookie. That is the claim
    :data:`~.surface.CSRF_ENFORCED_IN_ROUTE` records for these paths.

    It reads the **already-parsed** mapping rather than calling
    ``request.form()``, so the shared dependency's unbounded form parse is not
    reachable from these pages at all.

    ``page`` names the page in the refusal's log line — the path prefix, not
    anything derived from the request.
    """

    presented = request.headers.get("x-csrf-token", "") or fields.get("csrf_token", "")
    if csrf_token_matches(session, presented):
        return True
    logger.info("web_forbidden", extra={"reason": f"missing or invalid CSRF token on {page}"})
    return False


def refused_form_page(*, root_path: str = "", status_code: int, back_path: str) -> str:
    """The page for a request body a management page declined to read.

    Deliberately independent of the session, the invitation service, and the
    listing: it is answered on the path where the caller is being told their
    request was too large or the wrong shape, and doing database work there
    would let a caller hammering the cap turn each refusal into two queries.

    One page for both refusals, with the heading naming which. Neither says
    anything about the submitted content — there is nothing useful to quote and
    an echoed body is a body in a response. ``back_path`` is the page the form
    came from, always a constant of :mod:`.surface`, never request-derived.
    """

    if status_code == UNSUPPORTED_MEDIA_TYPE:
        heading = "That form could not be read"
        detail = (
            "This page accepts an ordinary form submission and nothing else."
            " Use the form on the invitations page rather than posting to it"
            " directly."
        )
    else:
        heading = "That request was too large"
        detail = (
            "The form you sent is larger than this page will read, so nothing"
            " was created or changed. An email address and an invitation id are"
            " all it expects."
        )
    return render_page(
        title=heading,
        body=(
            f"<h1>{escape(heading)}</h1>"
            f"<p>{escape(detail)}</p>"
            f'<p><a href="{escape(root_path)}{back_path}">Back to invitations</a></p>'
        ),
        root_path=root_path,
    )
