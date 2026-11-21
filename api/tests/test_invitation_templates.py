"""Editable invitation templates and the invitation listing filters (#348).

Unit tests run everywhere. The store and the listing SQL are exercised against
a real database when ``COLLAB_HUB_TEST_POSTGRES_URL`` is set, exactly as
``test_invitations.py`` does.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from collab_hub_api.frames.invitation_email import (  # noqa: E402
    ConfiguredInvitationEmailDelivery,
    DisabledInvitationEmailDelivery,
    SesInvitationEmailProvider,
)
from collab_hub_api.frames.invitation_templates import (  # noqa: E402
    REQUIRED_PLACEHOLDERS,
    TEMPLATE_EXISTING_ACCOUNT,
    TEMPLATE_KEYS,
    TEMPLATE_NEW_ACCOUNT,
    DefaultInvitationTemplateStore,
    InvitationTemplate,
    InvitationTemplatesUnavailableError,
    UnknownInvitationTemplateError,
    default_template,
    normalize_body,
    render_preview,
    required_placeholders,
    template_for_sending,
    template_problems,
)
from collab_hub_api.web.onboarding_email import SUBJECT  # noqa: E402

APP_INSTRUCTIONS = "Download the Collab desktop app from https://example.test/download"


def problems_for(subject: str, body: str) -> list[str]:
    return template_problems(subject, body, app_instructions=APP_INSTRUCTIONS, require_verified_email=True)


# ===========================================================================
# The built-in copy
# ===========================================================================


@pytest.mark.parametrize("key", TEMPLATE_KEYS)
def test_both_built_in_templates_pass_their_own_check(key):
    """The defaults are what reset restores, so they must be saveable as they are."""

    template = default_template(key)
    assert not template.customized
    assert problems_for(template.subject, template.body) == []


def test_the_existing_account_copy_says_sign_in_and_not_register():
    """The whole reason for the second template: "Create your account" fails for these people."""

    body = default_template(TEMPLATE_EXISTING_ACCOUNT).body
    assert "Already have an account? Sign in" in body
    assert "Create your account" not in body
    assert "email already" in body and "exists" in body
    assert default_template(TEMPLATE_EXISTING_ACCOUNT).subject != SUBJECT


def test_the_new_account_default_is_the_existing_onboarding_copy():
    """Nothing changes for a deployment that never opens the editor."""

    template = default_template(TEMPLATE_NEW_ACCOUNT)
    assert template.subject == SUBJECT
    assert "Create your account" in template.body


def test_the_documented_required_placeholders_are_the_enforced_ones():
    assert REQUIRED_PLACEHOLDERS == required_placeholders()


def test_an_unknown_template_key_is_refused():
    with pytest.raises(UnknownInvitationTemplateError):
        default_template("welcome_back")


# ===========================================================================
# What an edit may not do
# ===========================================================================


def _body_without(placeholder: str) -> str:
    return default_template(TEMPLATE_NEW_ACCOUNT).body.replace(placeholder, "")


@pytest.mark.parametrize("placeholder", REQUIRED_PLACEHOLDERS)
def test_each_required_placeholder_is_required(placeholder):
    problems = problems_for("Your invitation", _body_without(placeholder))
    assert any(placeholder in problem for problem in problems), problems


def test_an_unknown_placeholder_is_named_in_the_refusal():
    body = default_template(TEMPLATE_NEW_ACCOUNT).body.replace("Hi [NAME],", "Hi [FIRST NAME],")
    assert problems_for("Your invitation", body) == [
        "This hub does not fill in [FIRST NAME]. Use only the placeholders listed above, or remove the brackets."
    ]


def test_every_problem_is_reported_at_once():
    """Fixing one thing only to be told about the next is the editor wasting the operator's time."""

    body = _body_without("[DATA STATEMENT]").replace("Hi [NAME],", "Hi [FIRST NAME],")
    problems = problems_for("", body)
    assert len(problems) == 3, problems


def test_an_unpaired_conditional_is_refused_in_plain_words():
    body = default_template(TEMPLATE_NEW_ACCOUNT).body.replace("[END IF]\n", "")
    assert problems_for("Your invitation", body) == [
        "Each [IF VERIFIED EMAIL REQUIRED] needs a matching [END IF], and each must be on a line of its own."
    ]


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("", "empty"),
        ("Line one\nLine two", "line break"),
        ("Hello​there", "control character"),
        ("Welcome [NAME]", "placeholders"),
        ("x" * 201, "longer than"),
    ],
)
def test_a_bad_subject_is_refused_with_a_reason(subject, fragment):
    problems = problems_for(subject, default_template(TEMPLATE_NEW_ACCOUNT).body)
    assert any(fragment in problem for problem in problems), problems


def test_an_empty_body_is_refused():
    assert problems_for("Your invitation", "   \n") == ["The message is empty."]


def test_a_reworded_template_with_every_required_placeholder_passes():
    body = (
        "Hello,\n\nJoin us: [INVITATION LINK]\nUse [INVITEE EMAIL] before [EXPIRY].\n\n"
        "[DESKTOP APP DOWNLOAD / INSTALL INSTRUCTIONS]\n\n[DATA STATEMENT]\n"
    )
    assert problems_for("Join Collab", body) == []


def test_brackets_in_the_deployments_app_instructions_do_not_fail_a_template():
    """The regression the send path already pins, held for the editor's check too."""

    problems = template_problems(
        "Your invitation",
        default_template(TEMPLATE_NEW_ACCOUNT).body,
        app_instructions="Download [MACOS_ARM64] build",
        require_verified_email=True,
    )
    assert problems == []


def test_a_required_placeholder_only_inside_the_conditional_is_refused():
    """With verification off that span is dropped, and the email would carry no link."""

    body = (
        "Hello\n[IF VERIFIED EMAIL REQUIRED]\n[INVITATION LINK] [INVITEE EMAIL] [EXPIRY]\n[DATA STATEMENT]\n[END IF]\n"
    )
    problems = problems_for("Your invitation", body)
    assert problems == [
        "[INVITATION LINK], [INVITEE EMAIL], [EXPIRY], [DATA STATEMENT] must also appear outside"
        " [IF VERIFIED EMAIL REQUIRED] … [END IF], or some invitations would go out without it."
    ]


def test_the_length_limit_is_measured_on_the_text_that_is_stored():
    """A body at the limit with no trailing newline grows by one when normalized."""

    from collab_hub_api.frames.invitation_templates import MAX_BODY_CHARS

    core = "[INVITATION LINK] [INVITEE EMAIL] [EXPIRY] [DATA STATEMENT] "
    body = core + "x" * (MAX_BODY_CHARS - len(core))
    assert len(body) == MAX_BODY_CHARS and not body.endswith("\n")
    assert problems_for("Your invitation", body) == [f"The message is longer than {MAX_BODY_CHARS} characters."]


def test_line_endings_are_normalized():
    assert normalize_body("a\r\nb\rc\n\n\n") == "a\nb\nc\n"


# ===========================================================================
# Preview
# ===========================================================================


def test_a_preview_is_filled_in_and_carries_no_real_token():
    template = default_template(TEMPLATE_EXISTING_ACCOUNT)
    subject, body = render_preview(
        template,
        accept_url="https://hub.example.test/invite/accept",
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=False,
        now=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
    )
    assert subject == template.subject
    assert "https://hub.example.test/invite/accept#token=preview-only-not-a-real-invitation" in body
    assert "person@example.com" in body
    assert "2026-11-23 12:00 UTC" in body
    assert APP_INSTRUCTIONS in body
    assert "[" not in body.replace("[MACOS", "")


def test_a_preview_works_without_email_configured():
    """A deployment with email off can still word its templates."""

    _, body = render_preview(
        default_template(TEMPLATE_NEW_ACCOUNT),
        accept_url=None,
        app_instructions=None,
        require_verified_email=True,
    )
    assert "hub.example.com" in body
    assert "desktop app" in body


# ===========================================================================
# Stores and the send path
# ===========================================================================


def test_without_a_database_the_defaults_are_read_and_edits_refused():
    store = DefaultInvitationTemplateStore()
    assert [template.key for template in store.list()] == list(TEMPLATE_KEYS)
    assert store.get(TEMPLATE_NEW_ACCOUNT) == default_template(TEMPLATE_NEW_ACCOUNT)
    with pytest.raises(InvitationTemplatesUnavailableError):
        store.save(None, TEMPLATE_NEW_ACCOUNT, subject="s", body="b")
    with pytest.raises(InvitationTemplatesUnavailableError):
        store.reset(None, TEMPLATE_NEW_ACCOUNT)


def test_a_template_that_cannot_be_read_falls_back_to_the_built_in_copy(caplog):
    class Broken:
        def get(self, key):
            raise RuntimeError("database went away")

    assert template_for_sending(Broken(), TEMPLATE_EXISTING_ACCOUNT) == default_template(TEMPLATE_EXISTING_ACCOUNT)
    assert template_for_sending(None, TEMPLATE_NEW_ACCOUNT) == default_template(TEMPLATE_NEW_ACCOUNT)
    assert "invitation_template_read_failed" in caplog.text


class RecordingSesClient:
    def __init__(self):
        self.calls: list[dict] = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-message-id"}


class FixedStore:
    def __init__(self, templates):
        self.templates = templates

    def get(self, key):
        return self.templates.get(key) or default_template(key)


def _delivery(client, store=None) -> ConfiguredInvitationEmailDelivery:
    provider = SesInvitationEmailProvider(
        sender_address="no-reply@collab.example.test",
        region="us-west-2",
        configuration_set="collab-invitations",
        client_factory=lambda _region, _config: client,
    )
    delivery = ConfiguredInvitationEmailDelivery(
        provider,
        accept_url="https://collab.example.test/invite/accept",
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=True,
    )
    if store is not None:
        delivery.attach_template_store(store)
    return delivery


def _send(delivery, template=TEMPLATE_NEW_ACCOUNT):
    return delivery.deliver(
        invitation_id="inv-1",
        recipient="invitee@example.test",
        invitation_secret="s3cret",
        organization_name=None,
        expires_at=datetime(2026, 11, 23, tzinfo=UTC),
        template=template,
    )


def _sent(client) -> tuple[str, str]:
    content = client.calls[-1]["Content"]["Simple"]
    return content["Subject"]["Data"], content["Body"]["Text"]["Data"]


def test_the_existing_account_template_is_what_gets_sent_when_asked_for():
    client = RecordingSesClient()
    outcome = _send(_delivery(client), TEMPLATE_EXISTING_ACCOUNT)

    assert outcome.status == "provider_accepted"
    subject, body = _sent(client)
    assert subject == default_template(TEMPLATE_EXISTING_ACCOUNT).subject
    assert "Already have an account? Sign in" in body
    assert "#token=s3cret" in body


def test_an_operators_edit_is_what_gets_sent():
    edited = InvitationTemplate(
        key=TEMPLATE_NEW_ACCOUNT,
        subject="You're invited to Collab",
        body="Hi [NAME]. Link: [INVITATION LINK] for [INVITEE EMAIL], by [EXPIRY].\n[DATA STATEMENT]\n",
        customized=True,
    )
    client = RecordingSesClient()
    _send(_delivery(client, FixedStore({TEMPLATE_NEW_ACCOUNT: edited})))

    subject, body = _sent(client)
    assert subject == "You're invited to Collab"
    assert body.startswith("Hi there. Link: https://collab.example.test/invite/accept#token=s3cret")


def test_an_edit_that_stopped_rendering_sends_the_built_in_copy_instead(caplog):
    """Checked at save, but a committed invitation must not be dropped if it slips through."""

    broken = InvitationTemplate(
        key=TEMPLATE_NEW_ACCOUNT,
        subject="Broken",
        body="[INVITATION LINK] [INVITEE EMAIL] [EXPIRY] [DATA STATEMENT] [SOMETHING NEW]\n",
        customized=True,
    )
    client = RecordingSesClient()
    outcome = _send(_delivery(client, FixedStore({TEMPLATE_NEW_ACCOUNT: broken})))

    assert outcome.status == "provider_accepted"
    subject, _ = _sent(client)
    assert subject == SUBJECT
    assert "invitation_template_unrenderable" in caplog.text


def test_an_edited_template_that_renders_without_the_link_is_not_sent():
    """Saved before the both-variants check existed, say: the built-in copy goes out instead."""

    no_link = InvitationTemplate(
        key=TEMPLATE_NEW_ACCOUNT,
        subject="No link",
        body="Hello\n[IF VERIFIED EMAIL REQUIRED]\n[INVITATION LINK] [INVITEE EMAIL] [EXPIRY]\n"
        "[DATA STATEMENT]\n[END IF]\n",
        customized=True,
    )
    provider = SesInvitationEmailProvider(
        sender_address="no-reply@collab.example.test",
        region="us-west-2",
        configuration_set="collab-invitations",
        client_factory=lambda _region, _config: client,
    )
    client = RecordingSesClient()
    delivery = ConfiguredInvitationEmailDelivery(
        provider,
        accept_url="https://collab.example.test/invite/accept",
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=False,
    )
    delivery.attach_template_store(FixedStore({TEMPLATE_NEW_ACCOUNT: no_link}))
    _send(delivery)

    subject, body = _sent(client)
    assert subject == SUBJECT
    assert "#token=s3cret" in body


def test_the_disabled_adapter_accepts_a_template_and_sends_nothing():
    delivery = DisabledInvitationEmailDelivery()
    delivery.attach_template_store(DefaultInvitationTemplateStore())
    outcome = delivery.deliver(
        invitation_id="inv-1",
        recipient="invitee@example.test",
        invitation_secret="s3cret",
        organization_name=None,
        expires_at=datetime(2026, 11, 23, tzinfo=UTC),
        template=TEMPLATE_EXISTING_ACCOUNT,
    )
    assert outcome.error_code == "email_not_configured"


# ===========================================================================
# Live Postgres: the store, and the listing filters
# ===========================================================================

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live template tests",
)

LIVE_TABLES = (
    "collab_invitation_templates",
    "collab_invitations",
    "collab_audit_events",
    "collab_org_members",
    "collab_orgs",
    "collab_schema_migrations",
)

OPERATOR = "0perator-1111-4111-8111-abcdefabcdef"


def _operator_ctx():
    from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
    from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR

    return AuthContext(
        user=OPERATOR,
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(name="Op", email="op@example.com", email_verified=True),
        platform_role=PLATFORM_ROLE_OPERATOR,
    )


@pytest.fixture
def live_db():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=15.0)
    try:
        with database.connection() as conn:
            for table in LIVE_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        run_collab_schema_migrations(database)
        yield database
    finally:
        database.close()


def _audit(database) -> list[dict]:
    with database.connection() as conn:
        return conn.execute(
            "SELECT action, target_type, target_id, actor, detail FROM collab_audit_events ORDER BY id"
        ).fetchall()


@live_postgres
def test_saving_reading_and_resetting_a_template(live_db):
    from collab_hub_api.frames.invitation_templates import PostgresInvitationTemplateStore

    store = PostgresInvitationTemplateStore(live_db)
    ctx = _operator_ctx()
    assert store.get(TEMPLATE_EXISTING_ACCOUNT) == default_template(TEMPLATE_EXISTING_ACCOUNT)

    body = "Hi [NAME]\r\n[INVITATION LINK] [INVITEE EMAIL] [EXPIRY]\r\n[DATA STATEMENT]"
    saved = store.save(ctx, TEMPLATE_EXISTING_ACCOUNT, subject="  Finish signing up  ", body=body)
    assert saved.customized and saved.subject == "Finish signing up"
    assert saved.body == "Hi [NAME]\n[INVITATION LINK] [INVITEE EMAIL] [EXPIRY]\n[DATA STATEMENT]\n"
    assert saved.updated_by == OPERATOR and saved.updated_by_label == "op@example.com"
    assert store.get(TEMPLATE_EXISTING_ACCOUNT) == saved
    assert [template.customized for template in store.list()] == [False, True]

    resaved = store.save(ctx, TEMPLATE_EXISTING_ACCOUNT, subject="Again", body=body)
    assert resaved.subject == "Again"

    assert store.reset(ctx, TEMPLATE_EXISTING_ACCOUNT) == default_template(TEMPLATE_EXISTING_ACCOUNT)
    assert store.get(TEMPLATE_EXISTING_ACCOUNT) == default_template(TEMPLATE_EXISTING_ACCOUNT)
    # A reset of nothing is not an action.
    store.reset(ctx, TEMPLATE_EXISTING_ACCOUNT)

    rows = _audit(live_db)
    assert [row["action"] for row in rows] == [
        "invitation_template.update",
        "invitation_template.update",
        "invitation_template.reset",
    ]
    assert {row["target_type"] for row in rows} == {"invitation_template"}
    assert {row["target_id"] for row in rows} == {TEMPLATE_EXISTING_ACCOUNT}
    assert all(row["detail"] == {"template": TEMPLATE_EXISTING_ACCOUNT} for row in rows)


@live_postgres
def test_concurrent_resets_record_one_reset(live_db):
    """Both see the row; only the one that deletes it records anything."""

    import threading

    from collab_hub_api.frames.invitation_templates import PostgresInvitationTemplateStore

    store = PostgresInvitationTemplateStore(live_db)
    ctx = _operator_ctx()
    store.save(
        ctx, TEMPLATE_NEW_ACCOUNT, subject="Edited", body="[INVITATION LINK] [INVITEE EMAIL] [EXPIRY] [DATA STATEMENT]"
    )

    start = threading.Barrier(4)

    def reset():
        start.wait()
        store.reset(ctx, TEMPLATE_NEW_ACCOUNT)

    threads = [threading.Thread(target=reset) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [row["action"] for row in _audit(live_db)] == ["invitation_template.update", "invitation_template.reset"]


@live_postgres
def test_the_store_refuses_a_template_missing_a_required_placeholder(live_db):
    from collab_hub_api.frames.invitation_templates import PostgresInvitationTemplateStore

    store = PostgresInvitationTemplateStore(live_db)
    with pytest.raises(ValueError):
        store.save(_operator_ctx(), TEMPLATE_NEW_ACCOUNT, subject="Hi", body="[INVITATION LINK] only\n")
    assert _audit(live_db) == []


@live_postgres
def test_the_table_refuses_an_unknown_template_key(live_db):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), live_db.connection() as conn:
        conn.execute(
            "INSERT INTO collab_invitation_templates (template, subject, body, updated_by)"
            " VALUES ('welcome_back', 's', 'b', 'u')"
        )


@live_postgres
def test_the_listing_filters_by_effective_state_and_address(live_db):
    from collab_hub_api.frames.invitations import PostgresInvitationService

    now = datetime.now(tz=UTC)
    rows = [
        # id, email, status, expires_at
        ("a-outstanding", "alice@example.com", "pending", now + timedelta(hours=40)),
        ("b-expired", "bob@example.com", "pending", now - timedelta(hours=1)),
        ("c-accepted", "carol@example.org", "accepted", now - timedelta(days=30)),
        ("d-revoked", "dave@example.com", "revoked", now + timedelta(hours=10)),
        # Pre-#157 rows can keep capitals; the search folds both sides.
        ("e-outstanding", "Erin@Example.com", "pending", now + timedelta(hours=20)),
    ]
    with live_db.connection() as conn:
        for invitation_id, email, status, expires_at in rows:
            conn.execute(
                "INSERT INTO collab_invitations (id, org_id, email, token_hash, created_by, expires_at, status)"
                " VALUES (%s, NULL, %s, %s, %s, %s, %s)",
                (invitation_id, email, invitation_id.ljust(64, "0"), OPERATOR, expires_at, "pending"),
            )
            if status == "accepted":
                conn.execute(
                    "UPDATE collab_invitations SET status = 'accepted', accepted_at = now(), accepted_by = %s"
                    " WHERE id = %s",
                    (OPERATOR, invitation_id),
                )
            elif status == "revoked":
                conn.execute(
                    "UPDATE collab_invitations SET status = 'revoked', revoked_at = now(), revoked_by = %s"
                    " WHERE id = %s",
                    (OPERATOR, invitation_id),
                )

    service = PostgresInvitationService(live_db)

    def ids(**kwargs):
        return sorted(row.id for row in service.list_all(limit=50, **kwargs).invitations)

    assert ids(status="outstanding") == ["a-outstanding", "e-outstanding"]
    assert ids(status="expired") == ["b-expired"]
    assert ids(status="accepted") == ["c-accepted"]
    assert ids(status="revoked") == ["d-revoked"]
    assert len(ids()) == 5
    assert ids(status="all", email_contains="EXAMPLE.COM") == [
        "a-outstanding",
        "b-expired",
        "d-revoked",
        "e-outstanding",
    ]
    assert ids(status="outstanding", email_contains="erin") == ["e-outstanding"]
    # A wildcard character is a character to find, not a pattern.
    assert ids(status="all", email_contains="%") == []

    assert service.status_counts() == {"outstanding": 2, "accepted": 1, "expired": 1, "revoked": 1, "all": 5}
    assert service.status_counts(email_contains="example.org") == {
        "outstanding": 0,
        "accepted": 1,
        "expired": 0,
        "revoked": 0,
        "all": 1,
    }

    with pytest.raises(ValueError):
        service.list_all(limit=50, status="pending")
