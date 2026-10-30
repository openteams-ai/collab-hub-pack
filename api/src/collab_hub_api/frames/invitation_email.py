"""Provider-safe invitation email delivery.

This module deliberately stops at the delivery boundary. Invitation state,
token rotation, durable rate limiting, and audited mutation belong to the
invitation lifecycle service; the provider must not guess those semantics.

The successful SES result is named ``provider_accepted`` rather than
``delivered``. A successful ``SendEmail`` call only proves that SES accepted
the request. Later delivery, bounce, complaint, and delay events are separate
runtime evidence.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    SSLError,
    UnknownEndpointError,
)

from .credentials import InvitationSecret

logger = logging.getLogger("frames_server.invitation_email")

DELIVERY_PROVIDER_ACCEPTED = "provider_accepted"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SES_TAG_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_AWS_REGION = re.compile(r"^[a-z]{2}(-[a-z0-9]+)+-[0-9]+$")
_SES_CONFIGURATION_SET = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, repr=False, slots=True)
class InvitationEmailMessage:
    """One sensitive outbound message.

    ``repr=False`` is intentional: the body contains the one-time invitation
    secret and must not appear in tracebacks or diagnostic object dumps.

    ``text_body`` is an :class:`~.invitations.InvitationSecret` rather than a
    ``str`` (issue #89) because suppressing the repr covers rendering and
    nothing else. The rendered body embeds a live, one-time credential, so
    every route that reads the *field value* — ``dataclasses.asdict``,
    ``astuple``, ``vars()``, ``json.dumps(..., default=vars)``, and FastAPI's
    response encoder, which walks dataclass fields and will return what it
    finds in a 200 body — would otherwise hand it out. Wrapping the value is
    what closes all of them at once; ``slots=True`` additionally leaves the
    message with no ``__dict__`` to flatten.

    ``.reveal()`` at the single point the string reaches the provider is the
    one deliberate escape.
    """

    recipient: str
    subject: str
    text_body: InvitationSecret


@dataclass(frozen=True)
class ProviderAcceptance:
    """SES accepted one request; this is not proof of final delivery."""

    message_id: str


@dataclass(frozen=True)
class DeliveryOutcome:
    """Sanitized state for the invitation lifecycle to persist or expose."""

    status: str
    error_code: str | None = None
    provider_message_id: str | None = None

    @property
    def actionable(self) -> bool:
        return self.status != DELIVERY_PROVIDER_ACCEPTED


class InvitationEmailProvider(Protocol):
    """Blocking provider seam called by a sync API route or threadpool."""

    def send(self, message: InvitationEmailMessage, *, invitation_id: str) -> ProviderAcceptance: ...


class SesV2Client(Protocol):
    def send_email(self, **kwargs: Any) -> Mapping[str, Any]: ...


class InvitationEmailProviderError(RuntimeError):
    """A redacted provider result with no original exception attached."""

    def __init__(self, code: str, *, status: str) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class DisabledInvitationEmailDelivery:
    """Compatibility default for deployments that have not enabled email."""

    configured = False
    """Whether a real provider stands behind :meth:`deliver`. The owner
    invitation page (#142) branches on this **before issuing**: with no
    provider it renders the one-time redemption link instead of attempting a
    send that is known to fail. The attribute is read with a default of
    ``True``, so an adapter that does not declare itself fails toward *not*
    rendering a live secret — a failed send is visible and recoverable, a
    needlessly rendered credential is neither."""

    def deliver(
        self,
        *,
        invitation_id: str,
        recipient: str,
        invitation_secret: str,
        organization_name: str | None,
        expires_at: datetime,
    ) -> DeliveryOutcome:
        del recipient, invitation_secret, organization_name, expires_at
        return _record_outcome(
            invitation_id,
            DeliveryOutcome(status=DELIVERY_FAILED, error_code="email_not_configured"),
        )


class ConfiguredInvitationEmailDelivery:
    """Render and send one invitation without retaining its sensitive content."""

    configured = True
    """See :attr:`DisabledInvitationEmailDelivery.configured`."""

    def __init__(
        self,
        provider: InvitationEmailProvider,
        *,
        accept_url: str,
        app_instructions: str,
        require_verified_email: bool = True,
    ) -> None:
        self._provider = provider
        self._accept_url = validate_accept_url(accept_url)
        # The copy has to describe the flow the deployment actually runs, so it
        # is driven by the *same* setting as the acceptance check rather than a
        # second switch someone could set differently. A deployment that stops
        # requiring verification and keeps telling invitees to watch for a
        # verification email has recreated the defect #171 existed to fix, with
        # the sign flipped.
        self._require_verified_email = require_verified_email
        # Validated at construction, not at first send: a deployment that
        # enables email without saying how invitees get the desktop app should
        # fail at startup, not silently produce a message with a placeholder in
        # it for the first real person who is invited.
        if not app_instructions.strip():
            raise ValueError("app_instructions is required when invitation email is enabled")
        self._app_instructions = app_instructions
        # Render one probe message now, for the same reason the check above
        # happens here: a deployment that enables invitation email and cannot
        # produce a message should fail where somebody is watching.
        #
        # **A probe render rather than a template check, and that distinction is
        # the whole point.** Resolving the copy alone proved the *template* was
        # sound and left `render_for_automated_delivery`'s placeholder refusal
        # running per send -- into the `except (TypeError, ValueError)` below,
        # which drops the message by design. Three review rounds running, a
        # change in this module traded one failure for a sharper one inside that
        # same swallow: first an unresolvable marker, then a widened pattern
        # that matched brackets in an operator's own `app_instructions`
        # ("Download [MACOS_ARM64] build" failed every send, silently). Each
        # instance was fixed and the class stayed open.
        #
        # Rendering the message the send path renders, with this deployment's
        # actual `app_instructions`, closes the class: anything that would make
        # a real send unrenderable makes construction raise instead.
        self._probe_render()

    def _probe_render(self) -> None:
        """Render a throwaway message, to fail at startup rather than per send.

        Imported locally to keep the reason the send path's import is local:
        nothing of the browser surface should reach the mail path at import
        time.

        The probe values are deliberately unroutable -- nothing is sent, and a
        reader who finds one of these strings in a log knows it came from here.
        """

        from ..web.onboarding_email import render_for_automated_delivery

        try:
            render_for_automated_delivery(
                link="https://startup-probe.invalid/invite#token=probe",
                recipient="startup-probe@invalid",
                expires_at=datetime(2000, 1, 1, tzinfo=UTC),
                app_instructions=self._app_instructions,
                require_verified_email=self._require_verified_email,
            )
        except ValueError as exc:
            raise ValueError(
                f"invitation email is enabled but a message cannot be rendered, so no invitation could be sent: {exc}"
            ) from exc

    def deliver(
        self,
        *,
        invitation_id: str,
        recipient: str,
        invitation_secret: str,
        organization_name: str | None,
        expires_at: datetime,
    ) -> DeliveryOutcome:
        try:
            setup_url = build_setup_url(self._accept_url, invitation_secret)
            message = render_invitation_email(
                app_instructions=self._app_instructions,
                recipient=recipient,
                setup_url=setup_url,
                organization_name=organization_name,
                expires_at=expires_at,
                require_verified_email=self._require_verified_email,
            )
        except (TypeError, ValueError):
            # Validation failures are deterministic and happen before a
            # provider call. Never include their message: it may quote the
            # recipient, setup URL, or invitation secret.
            outcome = DeliveryOutcome(status=DELIVERY_FAILED, error_code="invalid_invitation_email")
        else:
            try:
                accepted = self._provider.send(message, invitation_id=invitation_id)
            except InvitationEmailProviderError as exc:
                outcome = DeliveryOutcome(status=exc.status, error_code=exc.code)
            except Exception:  # noqa: BLE001 - untrusted provider implementations
                # An unexpected provider exception may have happened before
                # or after remote acceptance. Calling it failed would make an
                # automatic resend capable of delivering two messages.
                outcome = DeliveryOutcome(status=DELIVERY_UNKNOWN, error_code="provider_result_unknown")
            else:
                outcome = DeliveryOutcome(
                    status=DELIVERY_PROVIDER_ACCEPTED,
                    provider_message_id=accepted.message_id,
                )
        return _record_outcome(invitation_id, outcome)


class SesInvitationEmailProvider:
    """AWS SES v2 provider using the runtime's normal IAM credential chain.

    No access key, SMTP password, or caller-selectable endpoint is accepted.
    The client is created lazily so application startup performs no AWS call.
    SDK retries are disabled because ``SendEmail`` has no idempotency token;
    a transport timeout can occur after SES accepted a request.
    """

    def __init__(
        self,
        *,
        sender_address: str,
        region: str,
        configuration_set: str,
        request_timeout_seconds: float = 10.0,
        client_factory: Callable[[str, BotoConfig], SesV2Client] | None = None,
    ) -> None:
        self._sender_address = validate_mailbox(sender_address)
        self._region = _required_text(region, "SES region")
        if not _AWS_REGION.fullmatch(self._region):
            raise ValueError("SES region is not a valid AWS region name")
        self._configuration_set = _required_text(configuration_set, "SES configuration set")
        if not _SES_CONFIGURATION_SET.fullmatch(self._configuration_set):
            raise ValueError("SES configuration set contains unsupported characters")
        if request_timeout_seconds <= 0:
            raise ValueError("SES request timeout must be greater than zero")
        self._request_timeout_seconds = request_timeout_seconds
        self._client_factory = client_factory or _default_ses_client
        self._client: SesV2Client | None = None
        self._client_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"SesInvitationEmailProvider(region={self._region!r})"

    def send(self, message: InvitationEmailMessage, *, invitation_id: str) -> ProviderAcceptance:
        recipient = validate_mailbox(message.recipient)
        if not _SES_TAG_VALUE.fullmatch(invitation_id):
            raise InvitationEmailProviderError("invalid_invitation_id", status=DELIVERY_FAILED)

        try:
            client = self._get_client()
        except Exception:  # noqa: BLE001 - credentials/client construction boundary
            raise InvitationEmailProviderError("provider_unavailable", status=DELIVERY_FAILED) from None

        try:
            response = client.send_email(
                FromEmailAddress=self._sender_address,
                Destination={"ToAddresses": [recipient]},
                Content={
                    "Simple": {
                        "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                        # The one place the rendered body becomes a string
                        # again: the argument of the provider call that sends it.
                        "Body": {"Text": {"Data": message.text_body.reveal(), "Charset": "UTF-8"}},
                        "Headers": [{"Name": "Auto-Submitted", "Value": "auto-generated"}],
                    }
                },
                ConfigurationSetName=self._configuration_set,
                EmailTags=[{"Name": "invitation_id", "Value": invitation_id}],
            )
        except ClientError as exc:
            code = _ses_error_code(exc)
            raise InvitationEmailProviderError(code, status=DELIVERY_FAILED) from None
        except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError):
            # Signing could not start, so no request reached SES.
            raise InvitationEmailProviderError("provider_unavailable", status=DELIVERY_FAILED) from None
        except (ConnectTimeoutError, EndpointConnectionError, SSLError, UnknownEndpointError):
            # These fail before an HTTP response can be read and are useful to
            # distinguish from read/connection-close failures after a request
            # may already have reached SES.
            raise InvitationEmailProviderError("provider_unreachable", status=DELIVERY_FAILED) from None
        except BotoCoreError:
            # Botocore transport failures are ambiguous: the response may
            # have been lost after SES accepted the request.
            raise InvitationEmailProviderError("provider_result_unknown", status=DELIVERY_UNKNOWN) from None
        except Exception:
            raise InvitationEmailProviderError("provider_result_unknown", status=DELIVERY_UNKNOWN) from None

        message_id = response.get("MessageId")
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 512
            or any(ord(character) < 33 or ord(character) > 126 for character in message_id)
        ):
            raise InvitationEmailProviderError("invalid_provider_response", status=DELIVERY_UNKNOWN)
        return ProviderAcceptance(message_id=message_id)

    def _get_client(self) -> SesV2Client:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    config = BotoConfig(
                        connect_timeout=self._request_timeout_seconds,
                        read_timeout=self._request_timeout_seconds,
                        retries={"total_max_attempts": 1, "mode": "standard"},
                    )
                    self._client = self._client_factory(self._region, config)
        return self._client


InvitationEmailDelivery = ConfiguredInvitationEmailDelivery | DisabledInvitationEmailDelivery
"""Either delivery adapter, as one annotation for the lifecycle service.

Both expose the same ``deliver(...) -> DeliveryOutcome`` call and differ only
in whether a provider is configured, so callers never branch on which one they
hold (issue #89 consumes this seam and does not reimplement it).
"""


def validate_accept_url(value: str) -> str:
    """Require a safe page URL whose fragment is reserved for the secret."""

    candidate = value.strip()
    if not candidate:
        raise ValueError("invitation acceptance URL must not be empty")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ValueError("invitation acceptance URL must not contain whitespace or control characters")
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("invitation acceptance URL must be absolute")
    if parts.username is not None or parts.password is not None:
        raise ValueError("invitation acceptance URL must not contain user information")
    if parts.query or parts.fragment or candidate.endswith(("?", "#")):
        raise ValueError("invitation acceptance URL must not contain a query string or fragment")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("invitation acceptance URL must use HTTPS outside loopback development")
    return candidate


def build_setup_url(accept_url: str, invitation_secret: str) -> str:
    """Put the one-time secret in the URL fragment and nowhere else."""

    base = validate_accept_url(accept_url)
    if not invitation_secret:
        raise ValueError("invitation secret must not be empty")
    parts = urlsplit(base)
    fragment = f"token={quote(invitation_secret, safe='')}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", fragment))


def validate_mailbox(value: str) -> str:
    """Accept one exact addr-spec, never a display name or recipient list."""

    if value != value.strip() or not value.isascii() or any(ord(character) < 32 for character in value):
        raise ValueError("email address must be one exact ASCII addr-spec")
    try:
        address = Address(addr_spec=value)
    except (HeaderParseError, TypeError, ValueError) as exc:
        raise ValueError("email address must be one exact ASCII addr-spec") from exc
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("email address must be one exact ASCII addr-spec")
    return address.addr_spec


def render_invitation_email(
    *,
    recipient: str,
    setup_url: str,
    organization_name: str | None,
    expires_at: datetime,
    app_instructions: str,
    require_verified_email: bool = True,
) -> InvitationEmailMessage:
    """Render the **approved onboarding copy** for one invitation (#93).

    This used to compose its own terse message. It no longer does, and that is
    the point of #93: the copy an invitee reads is the same text whether an
    operator pastes it from the invitation page or the server sends it, because
    both render ``web/onboarding_email.txt`` — the single copy approved on #153.

    Composing a second version here is what produced the divergence that issue
    was opened about. The old message told the reader replies would not be read,
    while the manual flow depended on them writing in; it carried no data
    statement (#146); and it said nothing about what happens between
    registering and accepting, which is the one thing an invitee most needs
    told. That gap was filled first by a step explaining that no verification
    mail was coming, and then -- once the realm gained SMTP and
    `verifyEmail=true` -- by the instruction to open the verification email and
    reopen the invitation link.

    **Layering, stated rather than hidden.** The template lives under ``web/``
    because the invitation pages render it too, so this imports "upwards". The
    alternative was a second copy of the words on this side, which is precisely
    the failure above. Importing it costs nothing but strings —
    :mod:`..web.data_statement` defers its page import for exactly this reason —
    so no browser-surface machinery reaches the mail path.

    ``require_verified_email`` **does** default here, unlike the other
    pass-throughs. The fail-loud argument for requiring it is that a dropped
    keyword should be a ``TypeError`` at the first call -- but this function's
    only caller is inside :meth:`ConfiguredInvitationEmailDelivery.deliver`'s
    ``try``, whose ``except (TypeError, ValueError)`` turns that into a dropped
    email with the message never logged. So requiring it buys a silent outage
    where defaulting it buys copy that over-explains. The seam is still covered:
    the delivery threads the value and a test captures what the provider was
    handed.

    ``organization_name`` is accepted and deliberately unused: the approved copy
    is org-neutral, because operator invitations may *create* an organization
    where owner invitations join one, and one text serves both. It stays in the
    signature because the delivery contract is #89's and should not churn for a
    copy decision — and if the copy ever names the organization, this is where
    it plugs in.
    """

    from ..web.onboarding_email import SUBJECT, render_for_automated_delivery

    recipient = validate_mailbox(recipient)
    setup_parts = urlsplit(setup_url)
    if setup_parts.query or not setup_parts.fragment.startswith("token="):
        raise ValueError("setup URL must carry the invitation secret in its fragment")
    if setup_parts.scheme not in {"http", "https"} or not setup_parts.hostname:
        raise ValueError("setup URL must be absolute")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("invitation expiry must be timezone-aware")
    del organization_name  # see the docstring: org-neutral by decision

    body = render_for_automated_delivery(
        link=setup_url,
        recipient=recipient,
        expires_at=expires_at,
        app_instructions=app_instructions,
        require_verified_email=require_verified_email,
    )
    return InvitationEmailMessage(recipient=recipient, subject=SUBJECT, text_body=InvitationSecret(body))


def _record_outcome(invitation_id: str, outcome: DeliveryOutcome) -> DeliveryOutcome:
    level = logging.INFO if outcome.status == DELIVERY_PROVIDER_ACCEPTED else logging.WARNING
    log_invitation_id = invitation_id if _SES_TAG_VALUE.fullmatch(invitation_id) else "invalid"
    logger.log(
        level,
        "invitation_email_delivery_outcome",
        extra={
            "invitation_id": log_invitation_id,
            "delivery_status": outcome.status,
            "delivery_error_code": outcome.error_code,
        },
    )
    return outcome


def _ses_error_code(exc: ClientError) -> str:
    code = exc.response.get("Error", {}).get("Code")
    if code in {"TooManyRequestsException", "LimitExceededException"}:
        return "provider_rate_limited"
    if code in {"MailFromDomainNotVerifiedException", "NotFoundException", "BadRequestException"}:
        return "provider_configuration_error"
    if code in {"AccountSuspendedException", "SendingPausedException"}:
        return "provider_sending_disabled"
    if code == "MessageRejected":
        return "provider_rejected"
    return "provider_error"


def _required_text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _default_ses_client(region: str, config: BotoConfig) -> SesV2Client:
    return boto3.client("sesv2", region_name=region, config=config)
