"""Editable invitation email templates (#348).

What an invitee is told used to be one file in the package
(``web/onboarding_email.txt``), so changing a sentence meant a release. Two
things changed that:

- the public hub kept inviting people who had **already registered** but never
  accepted an invitation, and the copy told them to "Create your account",
  which fails for them with "email already exists". They need different words,
  so there are now two templates: :data:`TEMPLATE_NEW_ACCOUNT` and
  :data:`TEMPLATE_EXISTING_ACCOUNT`;
- operators need to adjust the wording without a deploy.

So each template is a row in ``collab_invitation_templates`` that an operator
edits from the admin panel, and **absence of a row means the built-in copy**.
The files under ``web/`` stay as that built-in copy: they are what a
deployment sends until someone edits it, and what "reset to default" restores.
A deployment that never opens the editor behaves exactly as it did before.

What an edit may not do
-----------------------
The template is free text, but four placeholders are required, because a
message without them is not an invitation anybody can act on, or is one the
data statement (#146) was promised to accompany:
:data:`REQUIRED_PLACEHOLDERS`. Everything else is checked by rendering the
template the way the send path does, with this deployment's own settings
(:func:`template_problems`), so a template that saves is a template that sends.
That matters because the send path drops an unrenderable message by design
(see ``ConfiguredInvitationEmailDelivery.deliver``); checking at save time is
what keeps an edit from silently stopping every invitation.

Every save and every reset is audited (``invitation_template.update`` /
``invitation_template.reset``): the text is what the hub says to the public
in its name, and "who changed it, when" is the first question after a bad
message goes out. The audit row records which template changed, not its text;
the row in the table *is* the current text.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from .audit import (
    AUDIT_ACTION_INVITATION_TEMPLATE_RESET,
    AUDIT_ACTION_INVITATION_TEMPLATE_UPDATE,
    AUDIT_TARGET_INVITATION_TEMPLATE,
    audited,
)
from .auth import AuthContext

logger = logging.getLogger("frames_server.invitation_templates")

TEMPLATE_NEW_ACCOUNT = "new_account"
"""For an address with no account: register, then accept."""

TEMPLATE_EXISTING_ACCOUNT = "existing_account"
"""For an address that already has an account but no organization: sign in,
then accept. Chosen at issuance by the admin panel's account check."""

TEMPLATE_KEYS = (TEMPLATE_NEW_ACCOUNT, TEMPLATE_EXISTING_ACCOUNT)
"""Closed, and CHECK-constrained in the table, for the same reason the audit
vocabulary is: a key nothing reads is a template nobody is sent."""

TEMPLATE_LABELS = {
    TEMPLATE_NEW_ACCOUNT: "New invitee",
    TEMPLATE_EXISTING_ACCOUNT: "Already registered, setup not finished",
}

MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 20_000
"""Generous bounds on what is stored. The built-in body is about 2,000
characters; these exist so the table is not a place to park arbitrary data."""

PREVIEW_RECIPIENT = "person@example.com"
"""Who a preview is addressed to. ``example.com`` is reserved, so nothing
rendered for a preview can reach anybody."""

PREVIEW_APP_INSTRUCTIONS = "(How to get the desktop app, from this deployment's configuration.)"
"""Stands in for ``frames.email.app_instructions`` where none is configured,
which is a deployment with invitation email switched off. Bracket-free on
purpose: the unresolved-placeholder check would otherwise reject it."""


class InvitationTemplatesUnavailableError(RuntimeError):
    """This deployment has no database to keep edited templates in."""


class UnknownInvitationTemplateError(ValueError):
    """A template key outside :data:`TEMPLATE_KEYS`."""


@dataclass(frozen=True)
class InvitationTemplate:
    """One template as a sender or the editor sees it.

    ``customized`` says whether an operator's text is in force. ``False`` means
    the built-in copy, and then ``updated_*`` are ``None``.
    """

    key: str
    subject: str
    body: str
    customized: bool
    updated_at: datetime | None = None
    updated_by: str | None = None
    updated_by_label: str | None = None


def _require_key(key: str) -> str:
    if key not in TEMPLATE_KEYS:
        raise UnknownInvitationTemplateError(f"{key!r} is not an invitation template")
    return key


def default_template(key: str) -> InvitationTemplate:
    """The built-in copy for *key*, from the files under ``web/``."""

    from ..web.onboarding_email import (
        DEFAULT_BODY_TEMPLATE,
        EXISTING_ACCOUNT_BODY_TEMPLATE,
        EXISTING_ACCOUNT_SUBJECT,
        SUBJECT,
    )

    if _require_key(key) == TEMPLATE_NEW_ACCOUNT:
        return InvitationTemplate(key=key, subject=SUBJECT, body=DEFAULT_BODY_TEMPLATE, customized=False)
    return InvitationTemplate(
        key=key, subject=EXISTING_ACCOUNT_SUBJECT, body=EXISTING_ACCOUNT_BODY_TEMPLATE, customized=False
    )


def required_placeholders() -> tuple[str, ...]:
    """The placeholders every template must contain, in the order the editor lists them."""

    from ..web.onboarding_email import (
        DATA_STATEMENT_PLACEHOLDER,
        EXPIRY_PLACEHOLDER,
        LINK_PLACEHOLDER,
        RECIPIENT_PLACEHOLDER,
    )

    return (LINK_PLACEHOLDER, RECIPIENT_PLACEHOLDER, EXPIRY_PLACEHOLDER, DATA_STATEMENT_PLACEHOLDER)


def optional_placeholders() -> tuple[str, ...]:
    """The placeholders a template may use and may leave out."""

    from ..web.onboarding_email import APP_INSTRUCTIONS_PLACEHOLDER, NAME_PLACEHOLDER

    return (NAME_PLACEHOLDER, APP_INSTRUCTIONS_PLACEHOLDER)


REQUIRED_PLACEHOLDERS = ("[INVITATION LINK]", "[INVITEE EMAIL]", "[EXPIRY]", "[DATA STATEMENT]")
"""Spelled out for readers; :func:`required_placeholders` is the source, and a
test holds the two equal."""


def normalize_body(body: str) -> str:
    """Line endings folded to ``\\n`` and exactly one trailing newline.

    Browsers already submit textarea text with ``\\n``; this is for an API
    caller that does not, so a stored template renders the same whoever saved
    it. The conditional markers must end their line, which the trailing
    newline guarantees for an ``[END IF]`` on the last line.
    """

    return body.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


CONDITIONAL_START = "[IF VERIFIED EMAIL REQUIRED]"
CONDITIONAL_END = "[END IF]"


def _subject_problems(subject: str) -> list[str]:
    from ..web.onboarding_email import UNRESOLVED_PLACEHOLDER

    problems = []
    if not subject.strip():
        problems.append("The subject is empty.")
    if len(subject) > MAX_SUBJECT_CHARS:
        problems.append(f"The subject is longer than {MAX_SUBJECT_CHARS} characters.")
    # A line break in a subject is a header-injection shape, and nothing a
    # mail client would show anyway. Other control and format characters are
    # refused for the same reason organization names refuse them.
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in subject):
        problems.append("The subject contains a line break or another control character.")
    if UNRESOLVED_PLACEHOLDER.search(subject):
        problems.append("The subject cannot use placeholders; only the body is filled in.")
    return problems


def _required_placeholder_problems(body: str) -> list[str]:
    """The required placeholders that would be missing from a sent message.

    Checked on the body as it is *sent*, under both settings of
    ``require_verified_email``, not on the text as typed: a placeholder that
    sits only inside ``[IF VERIFIED EMAIL REQUIRED]`` vanishes when that
    setting is off, and the message would go out with no link in it. A
    conditional that does not resolve is reported by the render check, so it
    is skipped here.
    """

    from ..web.onboarding_email import _resolve_conditionals

    missing: list[str] = []
    conditional_only: list[str] = []
    for placeholder in required_placeholders():
        if placeholder not in body:
            missing.append(placeholder)
            continue
        for setting in (True, False):
            try:
                resolved = _resolve_conditionals(body, require_verified_email=setting, source="invitation template")
            except ValueError:
                break
            if placeholder not in resolved:
                conditional_only.append(placeholder)
                break
    problems = []
    if missing:
        problems.append("The message must include " + ", ".join(missing) + ".")
    if conditional_only:
        problems.append(
            ", ".join(conditional_only)
            + f" must also appear outside {CONDITIONAL_START} … {CONDITIONAL_END}, or some invitations would go"
            " out without it."
        )
    return problems


def template_problems(
    subject: str,
    body: str,
    *,
    app_instructions: str | None,
    require_verified_email: bool,
) -> list[str]:
    """Everything that would stop this template from being sent, in plain words.

    An empty list means it renders on this deployment, under both settings of
    ``require_verified_email``: the template outlives a change to that
    setting, so it is checked against both, even though one is in force.
    """

    from ..web.onboarding_email import UNRESOLVED_PLACEHOLDER, render_for_automated_delivery

    problems = _subject_problems(subject)
    if not body.strip():
        problems.append("The message is empty.")
        return problems
    # Measured after normalizing, because that is the text the store keeps: a
    # body at the limit without a trailing newline would otherwise pass here
    # and fail the store's own bound.
    body = normalize_body(body)
    if len(body) > MAX_BODY_CHARS:
        problems.append(f"The message is longer than {MAX_BODY_CHARS} characters.")
        return problems
    problems.extend(_required_placeholder_problems(body))
    # Rendered even when something above is already wrong, so the operator
    # hears about an unknown placeholder in the same answer as a missing one
    # rather than one fix at a time. A missing required placeholder does not
    # stop rendering; it is simply absent from the result.

    known = set(required_placeholders()) | set(optional_placeholders()) | {CONDITIONAL_START, CONDITIONAL_END}
    unknown = sorted({token for token in UNRESOLVED_PLACEHOLDER.findall(body) if token not in known})
    if unknown:
        problems.append(
            "This hub does not fill in "
            + ", ".join(unknown)
            + ". Use only the placeholders listed above, or remove the brackets."
        )
        return problems

    instructions = app_instructions if app_instructions and app_instructions.strip() else PREVIEW_APP_INSTRUCTIONS
    for setting in (True, False):
        try:
            render_for_automated_delivery(
                link="https://template-check.invalid/invite#token=check",
                recipient=PREVIEW_RECIPIENT,
                expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
                app_instructions=instructions,
                require_verified_email=setting,
                template_body=body,
            )
        except ValueError as exc:
            # Unknown placeholders were caught above, so what is left is a
            # conditional that does not resolve. The renderer's own message
            # is written for a developer; this one says what to change.
            if "conditional" in str(exc):
                problems.append(
                    f"Each {CONDITIONAL_START} needs a matching {CONDITIONAL_END},"
                    " and each must be on a line of its own."
                )
            else:
                problems.append(f"This email cannot be rendered: {exc}")
            break
    return problems


def render_preview(
    template: InvitationTemplate,
    *,
    accept_url: str | None,
    app_instructions: str | None,
    require_verified_email: bool,
    now: datetime | None = None,
) -> tuple[str, str]:
    """The subject and body as an invitee would receive them, with sample values.

    The link carries a visibly fake token, so a preview that is copied
    somewhere cannot be redeemed.
    """

    from ..web.onboarding_email import render_for_automated_delivery
    from .invitation_email import build_setup_url
    from .invitations import INVITATION_TTL

    base = accept_url or "https://hub.example.com/invite/accept"
    issued = now or datetime.now(tz=timezone.utc)
    body = render_for_automated_delivery(
        link=build_setup_url(base, "preview-only-not-a-real-invitation"),
        recipient=PREVIEW_RECIPIENT,
        expires_at=issued + INVITATION_TTL,
        app_instructions=app_instructions
        if app_instructions and app_instructions.strip()
        else PREVIEW_APP_INSTRUCTIONS,
        require_verified_email=require_verified_email,
        template_body=normalize_body(template.body),
    )
    return template.subject, body


class DefaultInvitationTemplateStore:
    """No database: the built-in copy, and edits refused.

    Reading still works, because the send path asks for a template on every
    invitation and the built-in copy is a correct answer. Writing does not,
    because an edit that vanished at the next restart would be worse than one
    that was refused.
    """

    available = False

    def get(self, key: str) -> InvitationTemplate:
        return default_template(key)

    def list(self) -> list[InvitationTemplate]:
        return [default_template(key) for key in TEMPLATE_KEYS]

    def save(self, *args, **kwargs) -> InvitationTemplate:
        raise InvitationTemplatesUnavailableError("This deployment has no database for invitation templates")

    def reset(self, *args, **kwargs) -> InvitationTemplate:
        raise InvitationTemplatesUnavailableError("This deployment has no database for invitation templates")


class PostgresInvitationTemplateStore:
    """Edited templates in ``collab_invitation_templates`` (migration v18)."""

    available = True

    def __init__(self, db):
        self._db = db

    def get(self, key: str) -> InvitationTemplate:
        """The template in force for *key*: the operator's text, or the built-in copy."""

        _require_key(key)
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT template, subject, body, updated_at, updated_by, updated_by_label"
                " FROM collab_invitation_templates WHERE template = %s",
                (key,),
            ).fetchone()
        return _from_row(row) if row is not None else default_template(key)

    def list(self) -> list[InvitationTemplate]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT template, subject, body, updated_at, updated_by, updated_by_label"
                " FROM collab_invitation_templates"
            ).fetchall()
        stored = {row["template"]: _from_row(row) for row in rows if row["template"] in TEMPLATE_KEYS}
        return [stored.get(key) or default_template(key) for key in TEMPLATE_KEYS]

    def save(self, ctx: AuthContext, key: str, *, subject: str, body: str) -> InvitationTemplate:
        """Store an operator's text for *key*, recorded as ``invitation_template.update``.

        The caller has already run :func:`template_problems`; this re-checks
        only what keeps the table sane (the key, the bounds, the required
        placeholders), because the full check needs deployment settings this
        store does not hold.
        """

        _require_key(key)
        body = normalize_body(body)
        subject = subject.strip()
        if _subject_problems(subject) or len(body) > MAX_BODY_CHARS:
            raise ValueError("invitation template is out of bounds")
        if _required_placeholder_problems(body):
            raise ValueError("invitation template is missing a required placeholder")
        label = ctx.display.email or ctx.display.name
        with audited(
            self._db,
            ctx,
            AUDIT_ACTION_INVITATION_TEMPLATE_UPDATE,
            target_type=AUDIT_TARGET_INVITATION_TEMPLATE,
            target_id=key,
            target_label=TEMPLATE_LABELS[key],
            org_id=None,
            detail={"template": key},
        ) as event:
            row = event.conn.execute(
                """
                INSERT INTO collab_invitation_templates
                    (template, subject, body, updated_at, updated_by, updated_by_label)
                VALUES (%s, %s, %s, now(), %s, %s)
                ON CONFLICT (template) DO UPDATE SET
                    subject = EXCLUDED.subject,
                    body = EXCLUDED.body,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by,
                    updated_by_label = EXCLUDED.updated_by_label
                RETURNING template, subject, body, updated_at, updated_by, updated_by_label
                """,
                (key, subject, body, ctx.user, label),
            ).fetchone()
        return _from_row(row)

    def reset(self, ctx: AuthContext, key: str) -> InvitationTemplate:
        """Go back to the built-in copy, recorded as ``invitation_template.reset``.

        Resetting a template that was never edited writes nothing, and no
        audit row: like a repeated revoke, it is not an action. The delete and
        the check that it deleted something are one statement inside the
        audited transaction, so two concurrent resets record one reset, not
        two.
        """

        _require_key(key)
        try:
            with audited(
                self._db,
                ctx,
                AUDIT_ACTION_INVITATION_TEMPLATE_RESET,
                target_type=AUDIT_TARGET_INVITATION_TEMPLATE,
                target_id=key,
                target_label=TEMPLATE_LABELS[key],
                org_id=None,
                detail={"template": key},
            ) as event:
                deleted = event.conn.execute(
                    "DELETE FROM collab_invitation_templates WHERE template = %s RETURNING template", (key,)
                ).fetchone()
                if deleted is None:
                    # Raising rolls the audit row back with the (empty) delete.
                    raise _NothingToReset
        except _NothingToReset:
            pass
        return default_template(key)


class _NothingToReset(Exception):
    """Abandons a reset that deleted nothing, so it records nothing."""


def _from_row(row) -> InvitationTemplate:
    return InvitationTemplate(
        key=row["template"],
        subject=row["subject"],
        body=row["body"],
        customized=True,
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
        updated_by_label=row["updated_by_label"],
    )


InvitationTemplateStore = PostgresInvitationTemplateStore | DefaultInvitationTemplateStore


def template_for_sending(store, key: str) -> InvitationTemplate:
    """The template to send for *key*, falling back to the built-in copy.

    A database hiccup reading the template must not cost an invitation that
    has already been committed: the built-in copy is a correct message, and the
    fallback is logged so a run of them is visible.
    """

    if store is None:
        return default_template(key)
    try:
        return store.get(key)
    except UnknownInvitationTemplateError:
        raise
    except Exception:  # noqa: BLE001 - any read failure degrades to the built-in copy
        logger.warning("invitation_template_read_failed", extra={"template": key})
        return default_template(key)


__all__ = [
    "MAX_BODY_CHARS",
    "MAX_SUBJECT_CHARS",
    "REQUIRED_PLACEHOLDERS",
    "TEMPLATE_EXISTING_ACCOUNT",
    "TEMPLATE_KEYS",
    "TEMPLATE_LABELS",
    "TEMPLATE_NEW_ACCOUNT",
    "DefaultInvitationTemplateStore",
    "InvitationTemplate",
    "InvitationTemplateStore",
    "InvitationTemplatesUnavailableError",
    "PostgresInvitationTemplateStore",
    "UnknownInvitationTemplateError",
    "default_template",
    "normalize_body",
    "optional_placeholders",
    "render_preview",
    "required_placeholders",
    "template_for_sending",
    "template_problems",
]
