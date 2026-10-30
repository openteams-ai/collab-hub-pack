"""Shared rendering for the canonical legal documents (#95).

Two documents are served from this surface as their own anonymous pages —
the Terms of Service (:mod:`.terms_of_service`) and the Privacy Statement
(:mod:`.privacy_statement`). These are the **canonical, linkable** copies.
The deployment's Keycloak terms-acceptance step shows a one-sentence consent
line that links here, so the documents can change independently from the
acceptance flow.

Why the presentation is shared and the copy is not
--------------------------------------------------
Each document's text lives in its own module, so a commit that changes the
Terms touches the Terms and nothing else. See
:data:`~.terms_of_service.TERMS_OF_SERVICE_TEXT`.
To keep the terms and privacy statement from drifting, the banner, the
effective-date line, and the cross-links are rendered here, once, for both.

Why an effective date is part of the record
-------------------------------------------
Acceptance is recorded by Keycloak as a bare ``terms_and_conditions`` user
attribute holding an epoch timestamp. This is a single acceptance for both
without document versions (openteams-ai/collab.openteams.app#111). A
timestamp only means something if you can say what the documents said at that
moment. Two things make that possible, and both are obligations on whoever
edits the copy:

* this file's siblings are edited in commits that change the copy and
  nothing else, so ``git log`` answers "what were people shown, and when?";
* every document renders :data:`EFFECTIVE_DATE_LABEL` with its own
  ``LAST_UPDATED`` constant, so the correlation can be made from the page
  itself rather than from repository archaeology.

**Bump the document's ``LAST_UPDATED`` in the same commit that changes its
text.** A date that silently lags the copy is worse than no date, because it
asserts a correlation that is false.
"""

from __future__ import annotations

from collections.abc import Sequence

Section = tuple[str, tuple[str, ...]]
"""One heading and its paragraphs. A document's text constant is a tuple of
these, which keeps the whole document a *single* module-level constant — so a
diff of one name is a diff of everything the reader was shown."""

PLACEHOLDER_NOTICE = (
    "This is placeholder text, not the final agreement. It exists so the"
    " acceptance flow can be wired end to end. It has not been reviewed by"
    " counsel and does not state this deployment's actual obligations."
)
"""Shown at the top of any document whose copy is still a placeholder.

Rendered by :func:`legal_document_page` from the document's own
``placeholder`` flag rather than written into the copy, so that clearing it is
a deliberate one-line change in the document module and cannot be done by
accident while editing prose. Removing the flag is the act that says "counsel
has seen this"; nothing else in the codebase claims that.
"""

EFFECTIVE_DATE_LABEL = "In effect since"
"""Prefix for the date line. Named here so both documents say it identically."""


def legal_document_page(
    *,
    title: str,
    last_updated: str,
    sections: Sequence[Section],
    placeholder: bool,
    related: Sequence[tuple[str, str]],
    root_path: str = "",
) -> str:
    """Render one canonical legal document into a complete page.

    ``related`` is the cross-link list as ``(path, label)`` pairs — the other
    document and the data statement — so a reader who arrives at one of the
    three can reach the other two without going back to an email. Paths are
    surface constants, and the labels are authored here, so neither is
    request-derived; they are escaped anyway, because the day one of them
    becomes configurable should not be the day this page gains an injection.

    :func:`~.pages.render_page` is imported inside the function for the same
    reason :mod:`.data_statement` does it: importing a document module should
    cost strings, not the whole browser-surface layout and FastAPI with it.
    """

    from .pages import escape, render_page

    banner = f'<p class="notice">{escape(PLACEHOLDER_NOTICE)}</p>' if placeholder else ""
    date_line = f"<p><small>{escape(EFFECTIVE_DATE_LABEL)} {escape(last_updated)}.</small></p>"
    body = "".join(
        f"<h2>{escape(heading)}</h2>" + "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
        for heading, paragraphs in sections
    )
    links = "".join(
        f'<p><a href="{escape(root_path)}{escape(path)}">{escape(label)}</a></p>' for path, label in related
    )
    return render_page(
        title=title,
        body=(f"<h1>{escape(title)}</h1>{banner}{date_line}{body}<h2>Related</h2>{links}"),
        root_path=root_path,
    )
