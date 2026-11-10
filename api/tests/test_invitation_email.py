from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, unquote, urlsplit

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber
from pydantic import ValidationError

from collab_hub_api.config import Config, build_invitation_email_delivery
from collab_hub_api.frames import invitation_email as invitation_email_module
from collab_hub_api.frames.credentials import InvitationSecret
from collab_hub_api.frames.invitation_email import (
    DELIVERY_FAILED,
    DELIVERY_PROVIDER_ACCEPTED,
    DELIVERY_UNKNOWN,
    ConfiguredInvitationEmailDelivery,
    DisabledInvitationEmailDelivery,
    InvitationEmailMessage,
    ProviderAcceptance,
    SesInvitationEmailProvider,
    build_setup_url,
    render_invitation_email,
    validate_accept_url,
    validate_mailbox,
)
from collab_hub_api.web.data_statement import DATA_STATEMENT_TEXT
from collab_hub_api.web.onboarding_email import SUBJECT as ONBOARDING_SUBJECT
from collab_hub_api.web.onboarding_email import UNRESOLVED_PLACEHOLDER

INVITATION_ID = "0e0d29eb-8095-4c5d-8f83-a226d1e10f22"
RECIPIENT = "invitee@example.test"
SECRET = "secret?with/path#and spaces"
EXPIRES_AT = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
APP_INSTRUCTIONS = "Download the Collab desktop app from https://example.test/download"
"""Whatever a deployment says about getting the app. Required config (#93):
there is no truthful default, and a placeholder must never reach an invitee."""


class RecordingSesClient:
    def __init__(self, response=None, failure: Exception | None = None) -> None:
        self.response = response or {"MessageId": "ses-message-id"}
        self.failure = failure
        self.calls: list[dict] = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.response


def _delivery(client: RecordingSesClient) -> ConfiguredInvitationEmailDelivery:
    provider = SesInvitationEmailProvider(
        sender_address="no-reply@collab.example.test",
        region="us-west-2",
        configuration_set="collab-invitations",
        client_factory=lambda _region, _config: client,
    )
    return ConfiguredInvitationEmailDelivery(
        provider,
        accept_url="https://collab.example.test/invite/accept",
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=True,
    )


def _deliver(delivery: ConfiguredInvitationEmailDelivery):
    return delivery.deliver(
        invitation_id=INVITATION_ID,
        recipient=RECIPIENT,
        invitation_secret=SECRET,
        organization_name="Example Org",
        expires_at=EXPIRES_AT,
    )


def test_setup_url_carries_the_secret_only_in_the_fragment():
    url = build_setup_url("https://collab.example.test/invite/accept", SECRET)
    parts = urlsplit(url)

    assert parts.path == "/invite/accept"
    assert parts.query == ""
    assert parse_qs(parts.fragment) == {"token": [SECRET]}
    assert SECRET not in parts.path
    assert SECRET not in parts.query
    assert unquote(parts.fragment.removeprefix("token=")) == SECRET


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/invite/accept",
        "ftp://collab.example.test/invite/accept",
        "http://collab.example.test/invite/accept",
        "https://collab.example.test/invite accept",
        "https://user@collab.example.test/invite/accept",
        "https://collab.example.test/invite/accept?token=bad",
        "https://collab.example.test/invite/accept#existing",
    ],
)
def test_accept_url_rejects_unsafe_or_ambiguous_bases(url):
    with pytest.raises(ValueError):
        validate_accept_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://collab.example.test/invite/accept",
        "https://collab.example.test:8443/invite/accept",
        "http://localhost:8000/invite/accept",
        "http://127.0.0.1:8000/invite/accept",
    ],
)
def test_accept_url_allows_https_and_loopback_development(url):
    assert validate_accept_url(url) == url


@pytest.mark.parametrize(
    "address",
    [
        " Invitee@example.test",
        "Invitee@example.test ",
        "Display Name <invitee@example.test>",
        "first@example.test,second@example.test",
        "first@example.test\nBcc: second@example.test",
        "missing-domain@",
        "missing-at.example.test",
    ],
)
def test_mailbox_validation_refuses_lists_display_names_and_injection(address):
    with pytest.raises(ValueError):
        validate_mailbox(address)


def test_plain_text_template_has_complete_setup_and_no_sensitive_repr():
    setup_url = build_setup_url("https://collab.example.test/invite/accept", SECRET)
    message = render_invitation_email(
        recipient=RECIPIENT,
        setup_url=setup_url,
        organization_name=" Example\nOrganization ",
        expires_at=EXPIRES_AT,
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=True,
    )

    assert message.recipient == RECIPIENT
    # The approved copy is org-neutral (#153): one text serves an invitation
    # that joins an organization and one that creates it, so the organization
    # name is accepted and deliberately not rendered.
    assert message.subject == ONBOARDING_SUBJECT
    assert "Example Organization" not in message.subject
    # `.reveal()` because the rendered body embeds the one-time secret and is
    # therefore held as an InvitationSecret, not a str (issue #89).
    body = message.text_body.reveal()
    assert "Example Organization" not in body

    assert setup_url in body
    assert RECIPIENT in body
    assert "2026-08-12 12:00 UTC" in body

    # The three things the retired message omitted, which is why #93 exists.
    # Compared whitespace-flattened: the copy is wrapped for plain-text mail, so
    # a phrase can straddle a line break without meaning anything different.
    flat = " ".join(body.split())
    assert DATA_STATEMENT_TEXT in flat
    assert "collab-support@openteams.com" in flat
    # The address-confirmation instruction, which the invitee cannot skip: Gate
    # B refuses an unverified login, so a message that did not mention verifying
    # the address would strand them at the acceptance page.
    #
    # Pinned as the word rather than a phrase. The copy is meant to be edited
    # freely for tone -- an earlier version of this assertion demanded the exact
    # string "verification email" and failed the moment the sentence was
    # tightened, which tests the wording rather than the contract.
    assert "verify" in flat.lower()
    # And the line that contradicted the manual flow is gone.
    assert "not monitored" not in body
    # The manual-verification step went with an internal issue: telling
    # people to expect no verification mail is now the opposite of true.
    assert "Confirm your email address first" not in flat
    assert "confirm addresses by hand" not in flat

    # Nothing is left for a human to fill in, because nobody will.
    assert not UNRESOLVED_PLACEHOLDER.findall(body)
    assert APP_INSTRUCTIONS in body

    assert SECRET not in repr(message)
    assert RECIPIENT not in repr(message)


def test_ses_provider_sends_one_plain_text_recipient_with_configuration_set():
    client = RecordingSesClient()

    outcome = _deliver(_delivery(client))

    assert outcome.status == DELIVERY_PROVIDER_ACCEPTED
    assert outcome.provider_message_id == "ses-message-id"
    assert outcome.actionable is False
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["FromEmailAddress"] == "no-reply@collab.example.test"
    assert request["Destination"] == {"ToAddresses": [RECIPIENT]}
    assert request["ConfigurationSetName"] == "collab-invitations"
    assert request["EmailTags"] == [{"Name": "invitation_id", "Value": INVITATION_ID}]
    assert request["Content"]["Simple"]["Headers"] == [{"Name": "Auto-Submitted", "Value": "auto-generated"}]
    assert "Html" not in request["Content"]["Simple"]["Body"]
    assert quote(SECRET, safe="") in request["Content"]["Simple"]["Body"]["Text"]["Data"]


def test_ses_request_shape_is_accepted_by_the_installed_botocore_model():
    client = boto3.client(
        "sesv2",
        region_name="us-west-2",
        aws_access_key_id="fake-access-key",
        aws_secret_access_key="fake-secret-key",
    )
    setup_url = build_setup_url("https://collab.example.test/invite/accept", "opaque-secret")
    message = render_invitation_email(
        recipient=RECIPIENT,
        setup_url=setup_url,
        organization_name=None,
        expires_at=EXPIRES_AT,
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=True,
    )
    expected = {
        "FromEmailAddress": "no-reply@collab.example.test",
        "Destination": {"ToAddresses": [RECIPIENT]},
        "Content": {
            "Simple": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": message.text_body.reveal(), "Charset": "UTF-8"}},
                "Headers": [{"Name": "Auto-Submitted", "Value": "auto-generated"}],
            }
        },
        "ConfigurationSetName": "collab-invitations",
        "EmailTags": [{"Name": "invitation_id", "Value": INVITATION_ID}],
    }
    with Stubber(client) as stubber:
        stubber.add_response("send_email", {"MessageId": "ses-message-id"}, expected)
        provider = SesInvitationEmailProvider(
            sender_address="no-reply@collab.example.test",
            region="us-west-2",
            configuration_set="collab-invitations",
            client_factory=lambda _region, _config: client,
        )
        accepted = provider.send(message, invitation_id=INVITATION_ID)

    assert accepted.message_id == "ses-message-id"


def test_sdk_retries_are_disabled_for_non_idempotent_send_email():
    captured = {}
    client = RecordingSesClient()

    def factory(region, config):
        captured["region"] = region
        captured["config"] = config
        return client

    provider = SesInvitationEmailProvider(
        sender_address="no-reply@collab.example.test",
        region="us-west-2",
        configuration_set="collab-invitations",
        request_timeout_seconds=7,
        client_factory=factory,
    )
    message = InvitationEmailMessage(recipient=RECIPIENT, subject="Subject", text_body=InvitationSecret("Body"))

    provider.send(message, invitation_id=INVITATION_ID)

    assert captured["region"] == "us-west-2"
    assert captured["config"].retries["total_max_attempts"] == 1
    assert captured["config"].connect_timeout == 7
    assert captured["config"].read_timeout == 7


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("TooManyRequestsException", "provider_rate_limited"),
        ("MailFromDomainNotVerifiedException", "provider_configuration_error"),
        ("SendingPausedException", "provider_sending_disabled"),
        ("MessageRejected", "provider_rejected"),
        ("UnrecognizedProviderError", "provider_error"),
    ],
)
def test_ses_failures_are_actionable_sanitized_codes(provider_code, expected, caplog):
    failure = ClientError(
        {
            "Error": {"Code": provider_code, "Message": f"{RECIPIENT} {SECRET} fake-secret-key"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "SendEmail",
    )
    client = RecordingSesClient(failure=failure)

    with caplog.at_level(logging.INFO, logger="frames_server.invitation_email"):
        outcome = _deliver(_delivery(client))

    assert outcome.status == DELIVERY_FAILED
    assert outcome.error_code == expected
    assert outcome.actionable is True
    assert len(client.calls) == 1
    assert RECIPIENT not in caplog.text
    assert SECRET not in caplog.text
    assert "fake-secret-key" not in caplog.text


def test_pre_request_connection_failure_is_failed_and_not_automatically_retried(caplog):
    failure = EndpointConnectionError(endpoint_url=f"https://{SECRET}.example.test")
    client = RecordingSesClient(failure=failure)

    with caplog.at_level(logging.INFO, logger="frames_server.invitation_email"):
        outcome = _deliver(_delivery(client))

    assert outcome.status == DELIVERY_FAILED
    assert outcome.error_code == "provider_unreachable"
    assert len(client.calls) == 1
    assert SECRET not in caplog.text
    assert RECIPIENT not in caplog.text


def test_read_timeout_remains_unknown_and_is_not_automatically_retried():
    failure = ReadTimeoutError(endpoint_url="https://email.us-west-2.amazonaws.com")
    client = RecordingSesClient(failure=failure)

    outcome = _deliver(_delivery(client))

    assert outcome.status == DELIVERY_UNKNOWN
    assert outcome.error_code == "provider_result_unknown"
    assert len(client.calls) == 1


def test_missing_workload_credentials_fail_before_any_remote_request():
    client = RecordingSesClient(failure=NoCredentialsError())

    outcome = _deliver(_delivery(client))

    assert outcome.status == DELIVERY_FAILED
    assert outcome.error_code == "provider_unavailable"
    assert len(client.calls) == 1


def test_unexpected_provider_value_error_after_render_is_unknown():
    class UnexpectedProvider:
        def send(self, _message, *, invitation_id):
            del invitation_id
            raise ValueError("provider failed after an unknown amount of work")

    delivery = ConfiguredInvitationEmailDelivery(
        UnexpectedProvider(),
        app_instructions=APP_INSTRUCTIONS,
        accept_url="https://collab.example.test/invite/accept",
    )

    outcome = _deliver(delivery)

    assert outcome.status == DELIVERY_UNKNOWN
    assert outcome.error_code == "provider_result_unknown"


def test_invalid_invitation_id_is_not_written_verbatim_to_logs(caplog):
    delivery = _delivery(RecordingSesClient())
    unsafe_id = "bad-id\nrecipient=" + RECIPIENT

    with caplog.at_level(logging.INFO, logger="frames_server.invitation_email"):
        outcome = delivery.deliver(
            invitation_id=unsafe_id,
            recipient=RECIPIENT,
            invitation_secret=SECRET,
            organization_name=None,
            expires_at=EXPIRES_AT,
        )

    assert outcome.status == DELIVERY_FAILED
    assert outcome.error_code == "invalid_invitation_id"
    assert unsafe_id not in caplog.text
    assert RECIPIENT not in caplog.text


def test_missing_message_id_is_unknown_not_delivered():
    outcome = _deliver(_delivery(RecordingSesClient(response={"ResponseMetadata": {"HTTPStatusCode": 200}})))

    assert outcome.status == DELIVERY_UNKNOWN
    assert outcome.error_code == "invalid_provider_response"


def test_disabled_delivery_fails_explicitly_without_touching_sensitive_inputs(caplog):
    delivery = DisabledInvitationEmailDelivery()

    with caplog.at_level(logging.INFO, logger="frames_server.invitation_email"):
        outcome = delivery.deliver(
            invitation_id=INVITATION_ID,
            recipient=RECIPIENT,
            invitation_secret=SECRET,
            organization_name="Example Org",
            expires_at=EXPIRES_AT,
        )

    assert outcome.status == DELIVERY_FAILED
    assert outcome.error_code == "email_not_configured"
    assert SECRET not in caplog.text
    assert RECIPIENT not in caplog.text


def test_email_config_defaults_to_disabled_for_upgrade_compatibility():
    config = Config.parse()

    assert config.frames.email.provider == "disabled"
    assert isinstance(build_invitation_email_delivery(config), DisabledInvitationEmailDelivery)


def test_email_config_parses_secret_backed_environment(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__EMAIL__PROVIDER", "ses")
    monkeypatch.setenv(
        "COLLAB_HUB_API__FRAMES__EMAIL__ACCEPT_URL",
        "https://collab.example.test/invite/accept",
    )
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__EMAIL__APP_INSTRUCTIONS", APP_INSTRUCTIONS)
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__EMAIL__SES__SENDER_ADDRESS", "no-reply@collab.example.test")
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__EMAIL__SES__REGION", "us-west-2")
    monkeypatch.setenv("COLLAB_HUB_API__FRAMES__EMAIL__SES__CONFIGURATION_SET", "collab-invitations")

    config = Config()

    assert config.frames.email.provider == "ses"
    assert config.frames.email.ses.region == "us-west-2"
    assert isinstance(build_invitation_email_delivery(config), ConfiguredInvitationEmailDelivery)


def test_configured_delivery_build_is_lazy_and_does_not_resolve_credentials(monkeypatch):
    config = Config.parse(
        {
            "frames": {
                "email": {
                    "provider": "ses",
                    "accept_url": "https://collab.example.test/invite/accept",
                    "app_instructions": APP_INSTRUCTIONS,
                    "ses": {
                        "sender_address": "no-reply@collab.example.test",
                        "region": "us-west-2",
                        "configuration_set": "collab-invitations",
                    },
                }
            }
        }
    )

    def unexpected_client_creation(*_args, **_kwargs):
        raise AssertionError("configured startup must not create an AWS client")

    monkeypatch.setattr(invitation_email_module.boto3, "client", unexpected_client_creation)

    assert isinstance(build_invitation_email_delivery(config), ConfiguredInvitationEmailDelivery)


@pytest.mark.parametrize(
    "email",
    [
        {"provider": "ses"},
        {
            "provider": "ses",
            "accept_url": "https://collab.example.test/invite/accept?token=bad",
            "ses": {
                "sender_address": "no-reply@collab.example.test",
                "region": "us-west-2",
                "configuration_set": "collab-invitations",
            },
        },
        {
            "provider": "ses",
            "accept_url": "https://collab.example.test/invite/accept",
            "ses": {
                "sender_address": "Display Name <no-reply@collab.example.test>",
                "region": "us-west-2",
                "configuration_set": "collab-invitations",
            },
        },
    ],
)
def test_enabled_email_config_fails_closed(email):
    with pytest.raises(ValidationError):
        Config.parse({"frames": {"email": email}})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "us-west-2.example.test"),
        ("configuration_set", "contains spaces"),
    ],
)
def test_ses_provider_rejects_endpoint_shaping_and_invalid_configuration(field, value):
    kwargs = {
        "sender_address": "no-reply@collab.example.test",
        "region": "us-west-2",
        "configuration_set": "collab-invitations",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        SesInvitationEmailProvider(**kwargs)


def test_the_delivery_copy_and_the_acceptance_rule_read_one_setting() -> None:
    """**The coupling that must not come apart.**

    Two independent switches -- one deciding what acceptance requires, one
    deciding what the email says -- would eventually be set differently, and
    the failure is silent: invitees told to watch for a verification email that
    the deployment no longer sends, which is #171's defect with the sign
    flipped. Asserted against the builder, because that is where a second
    switch would be introduced.
    """

    from collab_hub_api.config import Config, build_invitation_email_delivery

    def delivery_for(flag: bool):
        return build_invitation_email_delivery(
            Config.parse(
                {
                    "frames": {
                        "invitations": {"require_verified_email": flag},
                        "email": {
                            "provider": "ses",
                            "accept_url": "https://web.test/invite",
                            "app_instructions": "Download it",
                            "ses": {
                                "sender_address": "no-reply@web.test",
                                "region": "us-west-2",
                                "configuration_set": "collab-invitations",
                            },
                        },
                    }
                }
            )
        )

    assert delivery_for(True)._require_verified_email is True
    assert delivery_for(False)._require_verified_email is False


class _Capturing:
    """A provider that keeps what it was handed."""

    def __init__(self) -> None:
        self.sent: list[InvitationEmailMessage] = []

    def send(self, message, *, invitation_id):
        del invitation_id
        self.sent.append(message)
        return ProviderAcceptance(message_id="probe-message-id")


@pytest.mark.parametrize(
    ("require_verified_email", "expect_verification_copy"),
    [(True, True), (False, False)],
)
def test_the_sent_copy_follows_the_delivery_setting(require_verified_email, expect_verification_copy) -> None:
    """Through ``deliver()``, which is the only production path that renders a
    sent email.

    An earlier version of this read ``delivery._require_verified_email`` and
    passed it to the renderer itself -- asserting the value against itself while
    never exercising the wiring, so deleting the real thread left every test
    green. So this captures what the provider was actually handed.

    Parametrized rather than written twice: the two directions were near-identical
    functions each declaring their own provider class, which is how a pair meant
    to pin one seam drifts into asserting different things about it.
    """

    provider = _Capturing()
    delivery = ConfiguredInvitationEmailDelivery(
        provider,
        accept_url="https://web.test/invite",
        app_instructions=APP_INSTRUCTIONS,
        require_verified_email=require_verified_email,
    )
    delivery.deliver(
        invitation_id="inv-1",
        recipient=RECIPIENT,
        invitation_secret=SECRET,
        organization_name=None,
        expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )

    (message,) = provider.sent
    body = message.text_body.reveal()
    assert ("verify the address" in body) is expect_verification_copy
    # And the broad sweep, at this seam rather than only at the renderer:
    # rewording the conditional paragraph to "confirm your address by following
    # the verification link" would otherwise slip through the one test that goes
    # via deliver().
    if not expect_verification_copy:
        assert "verify" not in body.lower()
        assert "verification" not in body.lower()
