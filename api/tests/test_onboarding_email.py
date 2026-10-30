"""The onboarding email template (#155).

The copy lives in exactly one place — ``web/onboarding_email.txt`` — so there
is no second copy to drift from. What these tests protect instead is the
template's contract with the renderer: every placeholder the copy uses is one
something substitutes, the two that cannot be derived from an invitation are the
only ones that can survive a partial render, and the file stays wrapped for
plain-text email.

The failure these are written against is a future copy edit — someone adds
``[ORGANIZATION]`` to the template, nothing fills it in, and an invitee reads
it verbatim.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from importlib.resources import files

import pytest

from collab_hub_api.web.data_statement import DATA_STATEMENT_TEXT
from collab_hub_api.web.onboarding_email import (
    APP_INSTRUCTIONS_PLACEHOLDER,
    AUTOMATED_GREETING_NAME,
    DATA_STATEMENT_PLACEHOLDER,
    EXPIRY_PLACEHOLDER,
    LINK_PLACEHOLDER,
    NAME_PLACEHOLDER,
    RECIPIENT_PLACEHOLDER,
    SUBJECT,
    TEMPLATE_FILENAME,
    WRAP_WIDTH,
    format_expiry,
    render_for_automated_delivery,
    render_onboarding_email,
)

PLACEHOLDER_PATTERN = re.compile(r"\[[A-Z][A-Z /]*\]")

SUBSTITUTED = {
    LINK_PLACEHOLDER,
    RECIPIENT_PLACEHOLDER,
    EXPIRY_PLACEHOLDER,
    DATA_STATEMENT_PLACEHOLDER,
}
"""Placeholders the renderer fills in. No sender among them: the copy names
none, so nothing per-operator reaches the rendered message (#153)."""

LEFT_FOR_THE_CALLER = {NAME_PLACEHOLDER, APP_INSTRUCTIONS_PLACEHOLDER}
"""Placeholders the invitation cannot supply — a greeting and the app
instructions. The sending path supplies both and refuses to send otherwise;
these tests measure that they are the only two it *has* to."""


def _template_text() -> str:
    return files("collab_hub_api.web").joinpath(TEMPLATE_FILENAME).read_text(encoding="utf-8")


def _rendered(**overrides) -> str:
    kwargs = {
        "link": "https://frames.example.test/invite/accept#token=abc",
        "recipient": "invitee@example.test",
        "expires_at": datetime(2026, 8, 14, 21, 30, tzinfo=timezone.utc),
    }
    kwargs.update(overrides)
    return render_onboarding_email(**kwargs)


def test_the_template_ships_with_the_package() -> None:
    """Read as package data, not from the source tree.

    If this fails in a built artifact but passes in a checkout, the template
    was left out of the wheel and every invitation page would raise instead of
    rendering.
    """

    assert _template_text().startswith("Subject: ")
    assert SUBJECT == "Your invitation to the OpenTeams Collab beta"


CONDITIONAL_MARKERS = {"[IF VERIFIED EMAIL REQUIRED]", "[END IF]"}
"""Spans kept or dropped by configuration rather than filled in.

Their own set, so the contract test keeps saying which slots are *substituted*
and which are *selected* -- two different failure modes, and folding them
together would let a marker be renamed into a placeholder unnoticed.
"""


def test_every_placeholder_in_the_copy_is_one_something_fills_in() -> None:
    """The guard on future copy edits, in both directions.

    A placeholder added to the template that nothing substitutes would be sent
    to an invitee verbatim; a placeholder the renderer substitutes that no
    longer appears in the template is a rename that silently stopped working.

    Conditional markers are spelled like placeholders on purpose -- see
    ``CONDITIONAL_BLOCK`` -- so they appear here too, and one added without
    being taught to the renderer fails this rather than reaching an invitee.
    """

    in_template = set(PLACEHOLDER_PATTERN.findall(_template_text()))
    assert in_template == SUBSTITUTED | LEFT_FOR_THE_CALLER | CONDITIONAL_MARKERS


def test_only_the_two_unknowable_placeholders_survive_rendering() -> None:
    """Anything else left in ``[BRACKETS]`` would be read by a real person."""

    assert set(PLACEHOLDER_PATTERN.findall(_rendered())) == LEFT_FOR_THE_CALLER


def test_the_email_carries_the_data_statement_verbatim() -> None:
    """#146's constant is the single source; the email must not paraphrase it.

    Whitespace-insensitive because the statement is wrapped for plain text
    here and rendered by CSS on the page — the words must match, the line
    breaks need not.
    """

    words = lambda text: " ".join(text.split())  # noqa: E731 - one-line local
    assert words(DATA_STATEMENT_TEXT) in words(_rendered())


def test_the_invitation_specifics_are_filled_in_not_left_to_the_operator() -> None:
    """The whole point of #155: link, address and expiry are already there."""

    rendered = _rendered()
    assert "https://frames.example.test/invite/accept#token=abc" in rendered
    assert "invitee@example.test" in rendered
    assert "2026-08-14 21:30 UTC" in rendered
    assert rendered.rstrip().endswith("The OpenTeams Collab team")


def test_the_copy_stays_wrapped_for_plain_text_email() -> None:
    """A guard on copy edits: unwrapped paragraphs render badly in mail clients.

    Rendered with short substitutions so the only thing under test is the
    template's own wrapping. The invitation link is exempt — it is a URL on its
    own line and wrapping it would break it, which is why the renderer never
    re-flows anything it substitutes.
    """

    rendered = _rendered(link="https://x.test/a#token=b", recipient="a@b.test")
    too_long = [line for line in rendered.splitlines() if len(line) > WRAP_WIDTH]
    assert too_long == []


def test_a_naive_expiry_is_refused_rather_than_assumed_to_be_utc() -> None:
    """An expiry is a deadline; reinterpreting its timezone moves it."""

    with pytest.raises(ValueError, match="timezone-aware"):
        format_expiry(datetime(2026, 8, 14, 21, 30))


# ===========================================================================
# Automated delivery (#93): the same copy, with nothing left for a human
# ===========================================================================

DELIVERY_KWARGS = {
    "link": "https://frames.example.test/invite/accept#token=abc",
    "recipient": "invitee@example.test",
    "expires_at": datetime(2026, 8, 14, 21, 30, tzinfo=timezone.utc),
    "app_instructions": "Download the app from https://example.test/download",
}


def test_automated_delivery_leaves_no_placeholder_for_the_reader() -> None:
    """Nobody proof-reads a sent message: the invitee just reads what is there."""

    body = render_for_automated_delivery(**DELIVERY_KWARGS)
    assert not PLACEHOLDER_PATTERN.findall(body)
    assert body.startswith(f"Hi {AUTOMATED_GREETING_NAME},")
    assert DELIVERY_KWARGS["app_instructions"] in body


def test_automated_delivery_still_renders_the_same_approved_copy() -> None:
    """The point of #93: one text, whoever sends it.

    The sent body must differ from a partial render *only* by the two
    substitutions a human would otherwise have made — which is what made
    automating delivery a substitution rather than a rewrite.
    """

    sent = render_for_automated_delivery(**DELIVERY_KWARGS)
    pasted = render_onboarding_email(**{k: v for k, v in DELIVERY_KWARGS.items() if k != "app_instructions"})
    assert sent == (
        pasted.replace(NAME_PLACEHOLDER, AUTOMATED_GREETING_NAME).replace(
            APP_INSTRUCTIONS_PLACEHOLDER, DELIVERY_KWARGS["app_instructions"]
        )
    )


def test_automated_delivery_refuses_empty_app_instructions() -> None:
    """No truthful default exists, so there is nothing to fall back to."""

    with pytest.raises(ValueError, match="app_instructions"):
        render_for_automated_delivery(**{**DELIVERY_KWARGS, "app_instructions": "   "})


def test_automated_delivery_refuses_a_placeholder_added_to_the_copy_later() -> None:
    """The guard is a pattern, not a list of the two known slots.

    A future copy edit that adds ``[ORGANIZATION]`` and nothing to fill it must
    fail here rather than mail the brackets to somebody.
    """

    import collab_hub_api.web.onboarding_email as module

    # `BODIES`, not `_BODY_TEMPLATE`: conditionals resolve at import now, so
    # the rendered body comes from there. That is the cost of the import-time
    # resolution -- one less patchable seam -- paid for by a damaged template
    # failing at startup instead of per send with a swallowed message.
    original = dict(module.BODIES)
    module.BODIES = {
        flag: body.replace("Thanks,", "Sent on behalf of [ORGANIZATION].\n\nThanks,", 1)
        for flag, body in original.items()
    }
    try:
        with pytest.raises(ValueError, match=r"\[ORGANIZATION\]"):
            render_for_automated_delivery(**DELIVERY_KWARGS)
    finally:
        module.BODIES = original


# ---------------------------------------------------------------------------
# Copy that follows the configuration (#190)
# ---------------------------------------------------------------------------


def _rendered_with(*, require_verified_email: bool) -> str:
    return render_for_automated_delivery(
        link="https://web.test/invite#token=" + "T" * 43,
        recipient="invitee@example.com",
        expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        app_instructions="Download it from https://example.test/collab",
        require_verified_email=require_verified_email,
    )


def test_the_verification_instruction_follows_the_configuration() -> None:
    """The whole point: the copy describes the flow the deployment runs.

    A deployment that stopped requiring verification while still telling
    invitees to watch for a verification email would have recreated the defect
    #171 fixed, with the sign flipped -- and the invitee would be waiting for
    mail that never comes, exactly as they were before #171.
    """

    strict = _rendered_with(require_verified_email=True)
    relaxed = _rendered_with(require_verified_email=False)

    assert "verify the address" in strict
    assert "verify" not in relaxed.lower(), "no instruction about a mail that will not arrive"


def test_neither_variant_leaks_a_marker_to_the_reader() -> None:
    """Kept or dropped, the markers themselves must never be sent."""

    for flag in (True, False):
        body = _rendered_with(require_verified_email=flag)
        assert "[IF" not in body and "[END IF]" not in body
        assert set(PLACEHOLDER_PATTERN.findall(body)) == set()


def test_dropping_the_block_leaves_the_paragraphs_correctly_spaced() -> None:
    """Whitespace asserted rather than reasoned about.

    A dropped span that took its separator with it would run two paragraphs
    together; one that left its separator behind would open a double blank
    before step 3. Both read fine in a diff and wrong in a mail client.
    """

    relaxed = _rendered_with(require_verified_email=False).splitlines()
    step_three = relaxed.index("3. ACCEPT YOUR INVITATION")
    assert relaxed[step_three - 1] == "", "step 3 keeps its blank line above"
    assert relaxed[step_three - 2] == "   not substitute another one.", (
        "the paragraph above step 3 is the one the dropped span followed"
    )
    assert relaxed[step_three - 3] != "", "exactly one blank line, not two"


def test_the_step_count_is_the_same_either_way() -> None:
    """The variable span is a paragraph inside step 2, not a step.

    Which is what keeps this simple: nothing renumbers, and the opening line
    that promises four steps stays true under both configurations.
    """

    for flag in (True, False):
        body = _rendered_with(require_verified_email=flag)
        numbered = [line for line in body.splitlines() if re.match(r"^\d\. ", line)]
        assert len(numbered) == 4, numbered
        assert "four steps" in body


def test_the_strict_variant_is_what_a_caller_gets_by_default() -> None:
    """Fail safe: copy that over-explains is a nuisance, copy that omits a
    required step strands the reader."""

    default = render_for_automated_delivery(
        link="https://web.test/invite#token=" + "T" * 43,
        recipient="invitee@example.com",
        expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        app_instructions="Download it from https://example.test/collab",
    )
    assert default == _rendered_with(require_verified_email=True)


@pytest.mark.parametrize(
    ("label", "damage"),
    [
        # No CRLF case: the loader translates universal newlines, so the
        # resolver never sees `\r\n`. The earlier version built one by hand and
        # so asserted tolerance of an input that cannot occur.
        ("trailing spaces after a marker", lambda body: body.replace("[END IF]\n", "[END IF]   \n")),
        (
            "trailing tab after a marker",
            lambda body: body.replace("[IF VERIFIED EMAIL REQUIRED]\n", "[IF VERIFIED EMAIL REQUIRED]\t\n"),
        ),
    ],
)
@pytest.mark.parametrize("require_verified_email", [True, False])
def test_line_ending_and_whitespace_drift_is_tolerated(label, damage, require_verified_email) -> None:
    """Reachable drift is absorbed rather than refused.

    A space or tab left after a marker by someone reflowing the copy is a real
    edit somebody can commit, and an exact-newline requirement would turn it
    into a refusal to send every invitation.

    What this no longer asserts is tolerance of a CRLF template: the loader
    performs universal-newline translation, so that input cannot reach here.
    """

    del label
    import collab_hub_api.web.onboarding_email as module

    resolved = module._resolve_conditionals(
        damage(module._BODY_TEMPLATE), require_verified_email=require_verified_email
    )
    assert not module.CONDITIONAL_MARKER.search(resolved)
    assert ("verify the address" in resolved) is require_verified_email


@pytest.mark.parametrize(
    ("label", "damage", "expect_named"),
    [
        (
            "an unclosed [IF ...]",
            lambda body: body.replace("[END IF]\n", ""),
            "[IF VERIFIED EMAIL REQUIRED]",
        ),
        (
            "a second condition the resolver does not know",
            lambda body: body.replace("[END IF]\n", "[END IF]\n\n[IF SOMETHING ELSE]\nx\n[END IF]\n", 1),
            "[IF SOMETHING ELSE]",
        ),
        (
            "a marker that does not begin its line",
            lambda body: body.replace("[IF VERIFIED EMAIL REQUIRED]\n", "text [IF VERIFIED EMAIL REQUIRED]\n", 1),
            "[IF VERIFIED EMAIL REQUIRED]",
        ),
    ],
)
def test_an_unresolvable_template_names_what_actually_survived(label, damage, expect_named) -> None:
    """What the raise is for, and it must not guess at the cause.

    An earlier message said "most likely an unclosed [IF ...]", which is wrong
    for the likelier case: a *second* condition added to the copy, whose marker
    the generic pattern matches and the block pattern -- keyed to one literal
    name -- does not. So the message reports the surviving markers and lets the
    reader see which it was.

    The line-start anchoring is here too: before it, a marker with text in front
    of it resolved and silently left that text behind, while the message claimed
    markers must sit alone on their own lines.
    """

    del label
    import collab_hub_api.web.onboarding_email as module

    with pytest.raises(ValueError) as raised:
        module._resolve_conditionals(damage(module._BODY_TEMPLATE), require_verified_email=True)
    assert expect_named in str(raised.value)
    assert "unclosed" not in str(raised.value), "the message must not guess at a cause"


def test_both_variants_are_resolved_once() -> None:
    import collab_hub_api.web.onboarding_email as module

    assert set(module.BODIES) == {True, False}
    assert "verify the address" in module.BODIES[True]
    assert "verify" not in module.BODIES[False].lower()


@pytest.mark.parametrize(
    ("app_instructions", "raises"),
    [
        ("Download from https://example.test/collab", False),
        # An operator's own bracketed token. Legitimate copy: the placeholder net
        # is scoped narrowly enough not to claim it, and a widening that did
        # claim it failed every send silently.
        ("Download the [MACOS_ARM64] build", False),
        # A genuinely unresolvable placeholder. This must stop the deployment.
        ("Download [SOME PLACEHOLDER] build", True),
    ],
)
def test_configuring_invitation_email_renders_a_probe_at_startup(app_instructions, raises) -> None:
    """Construction renders the message the send path renders.

    Resolving the template alone was not enough, and the reason is the shape of
    three consecutive defects rather than any one of them: the placeholder
    refusal runs inside `render_for_automated_delivery`, whose caller sits inside
    `deliver`'s `except (TypeError, ValueError)`, where the message is dropped by
    design. So each fix moved the failure to a different input reaching the same
    swallow -- an unresolvable marker, then a widened pattern matching an
    operator's own brackets.

    A probe render at construction closes that class, and **the raising case is
    the proof**: if construction did not render, a body it cannot render would
    not raise. An earlier version of this test asserted the same thing by
    dropping the module from `sys.modules` and watching it come back, which left
    two live copies of the module behind -- disarming the placeholder test later
    in this file, because its top-level import was bound to the first copy while
    the reimport produced a second.
    """

    from collab_hub_api.frames.invitation_email import ConfiguredInvitationEmailDelivery

    class _Provider:
        def send(self, message, *, invitation_id):  # pragma: no cover - never called
            raise AssertionError("construction must not send")

    def construct():
        return ConfiguredInvitationEmailDelivery(
            _Provider(),
            accept_url="https://web.test/invite",
            app_instructions=app_instructions,
        )

    if raises:
        with pytest.raises(ValueError, match="cannot be rendered"):
            construct()
    else:
        assert construct() is not None
