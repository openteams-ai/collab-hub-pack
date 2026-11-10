"""The Privacy Statement (#95): the canonical, linkable copy.

Served anonymously at :data:`~.surface.PRIVACY_PATH`, for the reasons given
in :mod:`.terms_of_service` — chiefly that its audience has not accepted yet
and so cannot sign in to read it.

Its relationship to the data statement
--------------------------------------
:mod:`.data_statement` is **not** superseded by this document and is not
folded into it. The two answer different questions for different audiences:
the data statement is the short paragraph an invitee reads next to the accept
button, deliberately one screen and deliberately unchanged since the team
authored it; this is the full statement a person can go and read. They are
cross-linked, and the data statement's contact address is the one this
document quotes, so the short form and the long form name the same address.

The copy below is a **placeholder** — see :mod:`.terms_of_service` for what
replacing it involves, and :mod:`.legal_documents` for why this file's git
history is half of the acceptance record.
"""

from __future__ import annotations

from .data_statement import DATA_STATEMENT_CONTACT
from .legal_documents import Section, legal_document_page

PRIVACY_STATEMENT_TITLE = "Privacy Statement"

PRIVACY_STATEMENT_LAST_UPDATED = "11 September 2026"
"""The date the copy below last changed. Bump it in the same commit that
changes :data:`PRIVACY_STATEMENT_TEXT`."""

PRIVACY_STATEMENT_IS_PLACEHOLDER = True
"""Whether the copy is still placeholder text. Clearing this flag is the act
that asserts the document has been reviewed."""

PRIVACY_STATEMENT_TEXT: tuple[Section, ...] = (
    (
        "What we collect",
        (
            "This section will list the categories of information the service"
            " holds about you: account details, organization membership,"
            " content you create, and security and usage logs.",
        ),
    ),
    (
        "How we use it",
        (
            "This section will describe the purposes each category is used"
            " for — operating the service, supporting you, and keeping the"
            " deployment secure — and will state that it is not sold.",
        ),
    ),
    (
        "Who can see it",
        (
            "This section will describe who your information is visible to:"
            " other members according to the service's sharing settings, and"
            " the authorized staff who operate and support the service.",
        ),
    ),
    (
        "Where it is stored",
        (
            "This section will describe where the deployment stores your"
            " information and which third parties process any of it.",
        ),
    ),
    (
        "How long we keep it",
        (
            "This section will describe retention: how long each category is"
            " kept, and what is removed when your account ends.",
        ),
    ),
    (
        "Your choices",
        (
            "This section will describe what you can ask for — access to your"
            " information, correction, and deletion — and how long a request"
            " takes.",
            "You do not have to wait for this section to be finished to ask."
            f" Write to {DATA_STATEMENT_CONTACT} and the request will be"
            " handled.",
        ),
    ),
    (
        "Contact",
        (f"Questions about this statement, and deletion requests, go to {DATA_STATEMENT_CONTACT}.",),
    ),
)
"""The document, as one constant: a tuple of ``(heading, paragraphs)``.

"Your choices" carries a live commitment rather than a placeholder promise on
purpose. Every other section can honestly say "this will describe…", because
nothing is lost by describing it later — but a person reading a privacy
statement to find out how to get their data deleted must not be told to come
back when the lawyers are done. The address is the same one
:mod:`.data_statement` already publishes, and that route already works.
"""


def privacy_statement_page(*, root_path: str = "") -> str:
    """The canonical page. Anonymous by design — see the path's entry in
    :data:`~.surface.PUBLIC_WEB_PATHS` for the argument."""

    from .surface import DATA_STATEMENT_PATH, TERMS_PATH

    return legal_document_page(
        title=PRIVACY_STATEMENT_TITLE,
        last_updated=PRIVACY_STATEMENT_LAST_UPDATED,
        sections=PRIVACY_STATEMENT_TEXT,
        placeholder=PRIVACY_STATEMENT_IS_PLACEHOLDER,
        related=(
            (TERMS_PATH, "Terms of Service"),
            (DATA_STATEMENT_PATH, "Data statement"),
        ),
        root_path=root_path,
    )
