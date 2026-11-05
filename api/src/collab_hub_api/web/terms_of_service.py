"""The Terms of Service (#95): the canonical, linkable copy.

Served anonymously at :data:`~.surface.TERMS_PATH`, for the same reason the
data statement is (:mod:`.data_statement`) and then one more: the audience
includes people who have not accepted yet and therefore cannot sign in. A
document you must already have agreed to in order to read is not a document
anyone agreed to.

The copy below is a **placeholder**. It is structured the way the finished
agreement will be, and each section says what it will contain, so the
acceptance flow can be wired end to end before counsel has written a word.
Replacing it is one commit: rewrite :data:`TERMS_OF_SERVICE_TEXT`, bump
:data:`TERMS_OF_SERVICE_LAST_UPDATED`, and clear
:data:`TERMS_OF_SERVICE_IS_PLACEHOLDER`.

This file's git history is half of the acceptance record — Keycloak stores
only a timestamp, with no document version — so an edit here should change
the copy and nothing else in the same commit. :mod:`.legal_documents` carries
the full argument.
"""

from __future__ import annotations

from .data_statement import DATA_STATEMENT_CONTACT
from .legal_documents import Section, legal_document_page

TERMS_OF_SERVICE_TITLE = "Terms of Service"

TERMS_OF_SERVICE_LAST_UPDATED = "11 September 2026"
"""The date the copy below last changed. Bump it in the same commit that
changes :data:`TERMS_OF_SERVICE_TEXT` — see :mod:`.legal_documents` for why a
stale date is worse than no date."""

TERMS_OF_SERVICE_IS_PLACEHOLDER = True
"""Whether the copy is still placeholder text. Clearing this flag is the act
that asserts the document has been reviewed; nothing else in the codebase
makes that claim, so it is deliberately a separate line from the prose."""

TERMS_OF_SERVICE_TEXT: tuple[Section, ...] = (
    (
        "What this service is",
        (
            "This section will describe the service these terms govern, who"
            " operates it, and which deployments it applies to.",
        ),
    ),
    (
        "Your account",
        (
            "This section will describe what an account entitles you to, your"
            " responsibility for activity under it, and the circumstances in"
            " which access may be suspended.",
        ),
    ),
    (
        "Acceptable use",
        (
            "This section will describe what may and may not be done with the"
            " service, including limits on automated access and on content"
            " that may be stored or shared through it.",
        ),
    ),
    (
        "Content you create",
        (
            "This section will describe who owns the content you create,"
            " what rights you grant the operator in order to run the service,"
            " and what happens to that content when your account ends.",
        ),
    ),
    (
        "Availability and changes",
        (
            "This section will describe what is and is not promised about"
            " availability, and how you will be told when these terms change.",
        ),
    ),
    (
        "Ending your use",
        (
            "This section will describe how you may stop using the service,"
            " how the operator may end your access, and what survives the end"
            " of the agreement.",
        ),
    ),
    (
        "Contact",
        (f"Questions about these terms go to {DATA_STATEMENT_CONTACT}.",),
    ),
)
"""The document, as one constant: a tuple of ``(heading, paragraphs)``.

One name holds the whole document so that a diff of this constant is a diff of
everything a reader was shown — which is what makes the git history usable as
the acceptance record. The contact address is read from
:mod:`.data_statement` rather than spelled a second time, so the address
people are told to write to cannot differ between two documents that are
shown to the same person minutes apart.
"""


def terms_of_service_page(*, root_path: str = "") -> str:
    """The canonical page. Anonymous by design — see the path's entry in
    :data:`~.surface.PUBLIC_WEB_PATHS` for the argument."""

    from .surface import DATA_STATEMENT_PATH, PRIVACY_PATH

    return legal_document_page(
        title=TERMS_OF_SERVICE_TITLE,
        last_updated=TERMS_OF_SERVICE_LAST_UPDATED,
        sections=TERMS_OF_SERVICE_TEXT,
        placeholder=TERMS_OF_SERVICE_IS_PLACEHOLDER,
        related=(
            (PRIVACY_PATH, "Privacy Statement"),
            (DATA_STATEMENT_PATH, "Data statement"),
        ),
        root_path=root_path,
    )
