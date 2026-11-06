"""What a client is told about each connector this hub offers.

``GET /v1/connectors`` is how a client discovers connectors: it lists the ones
this deployment can actually serve, with everything needed to show one to a
user and start connecting it. A client that renders from that response needs no
connector list of its own, so a deployment decides what its users see.

This module holds the two halves of that answer that are not the caller's
connection state: the fixed description of each connector, and the
per-deployment facts read from configuration (is it offered, and which identity
provider does a user link).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from .models import (
    GITHUB_CONNECTOR_ID,
    GMAIL_CONNECTOR_ID,
    GOOGLE_CALENDAR_CONNECTOR_ID,
    GOOGLE_DRIVE_CONNECTOR_ID,
    NOTION_CONNECTOR_ID,
    SLACK_CONNECTOR_ID,
    ConnectorLink,
)

__all__ = ["CATALOG", "ConnectorDescriptor", "connector_link", "is_offered"]


@dataclass(frozen=True)
class ConnectorDescriptor:
    id: str
    name: str
    short_name: str
    description: str
    provider: str
    """The configuration section, and operator switch, this connector hangs off.

    Connectors that share a provider share one linked account: linking Google
    once serves Drive, Gmail and Calendar.
    """
    prompt: str | None = None
    """An OIDC ``prompt`` the link request must carry, where one is needed."""
    connect_hint: str | None = None
    """What a user should know before they start connecting, where it matters."""


CATALOG: tuple[ConnectorDescriptor, ...] = (
    ConnectorDescriptor(
        id=GOOGLE_DRIVE_CONNECTOR_ID,
        name="Google Drive",
        short_name="Drive",
        description="Search and read files from the connected Google Drive.",
        provider="google",
        # The three Google connectors widen one linked identity's consent, and
        # Google only re-asks for scopes when told to.
        prompt="consent",
    ),
    ConnectorDescriptor(
        id=GMAIL_CONNECTOR_ID,
        name="Gmail",
        short_name="Gmail",
        description="Search and read messages from the connected Gmail mailbox.",
        provider="google",
        prompt="consent",
    ),
    ConnectorDescriptor(
        id=GOOGLE_CALENDAR_CONNECTOR_ID,
        name="Google Calendar",
        short_name="Calendar",
        description="Search and read events from connected Google Calendars.",
        provider="google",
        prompt="consent",
    ),
    ConnectorDescriptor(
        id=SLACK_CONNECTOR_ID,
        name="Slack",
        short_name="Slack",
        description="Search and read messages from connected Slack channels.",
        provider="slack",
        connect_hint=(
            "Before authorizing Slack, confirm the workspace you want to connect on Slack's authorization "
            "screen. Your email address does not select it automatically."
        ),
    ),
    ConnectorDescriptor(
        id=GITHUB_CONNECTOR_ID,
        name="GitHub",
        short_name="GitHub",
        description="Search and read your GitHub issues, PRs, repository files, and project boards (read-only).",
        provider="github",
    ),
    ConnectorDescriptor(
        id=NOTION_CONNECTOR_ID,
        name="Notion",
        short_name="Notion",
        description="Search and read your Notion pages and databases (read-only).",
        provider="notion",
    ),
)
"""Every connector this build can serve, in the order a client should show them."""


def is_offered(section) -> bool:
    """Whether a provider's configuration section can serve anyone.

    Credentials are what make a connector real here. One an operator switched
    off arrives with them blanked (see :mod:`..frames.connector_state`), so the
    two reasons a hub does not offer a connector need only this one check.
    """

    return bool(section.broker_token_url or section.static_access_token)


# Keycloak serves a linked identity's token at
# ``<issuer>/broker/<alias>/token``. The alias is whatever the realm named the
# identity provider, and a client has to ask to link that same name.
_BROKER_TAIL = re.compile(r"/broker/([^/]*)/token/?$")
# What a client will accept as an alias. One outside it is not guessed at.
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def connector_link(descriptor: ConnectorDescriptor, section) -> ConnectorLink | None:
    """How a user connects *descriptor* on a deployment configured as *section*.

    ``None`` when there is nothing the user can do from here: a static token
    serves every caller already (and takes precedence over a broker when both
    are set), and a Keycloak broker URL whose alias a client could not use is
    reported as no link rather than as a different provider's.
    """

    if section.static_access_token or not section.broker_token_url:
        return None
    # The path is cut from the configured string as written, not through a URL
    # parser: a parser drops tabs and newlines and rejects some hosts outright,
    # and either would describe an alias other than the one configured.
    path = re.split(r"[?#]", section.broker_token_url, maxsplit=1)[0]
    tail = _BROKER_TAIL.search(path)
    if tail is None:
        # Not Keycloak-shaped, so the URL does not name the alias. The provider
        # key is the alias every shipped realm uses.
        return ConnectorLink(alias=descriptor.provider, prompt=descriptor.prompt)
    # Decoded after the segment is cut out, so an encoded slash stays inside it.
    alias = unquote(tail.group(1))
    if not _ALIAS.fullmatch(alias):
        return None
    return ConnectorLink(alias=alias, prompt=descriptor.prompt)
