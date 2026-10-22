"""What an operator can truthfully be told about each connector.

Read-only, and deliberately so. Turning a connector on or off from the panel
would mean the panel writing deployment configuration, which is a much larger
change than it appears -- the values are Helm's, a running pod would have to be
told, and two sources of truth for "is Slack on" is precisely the kind of
divergence an admin panel is supposed to remove.

Why there is no green tick
--------------------------
Most connectors here are **brokered**: the access token is minted per user at
request time, so the hub holds no credential of its own for them. "Is Slack
healthy" therefore has no hub-level answer -- it has one answer per person, and
the server cannot produce any of them without acting as that person.

A connector configured with a **static** token is different: the hub does hold
a credential, and a probe would say something real. This module reports which
case each connector is in, and nothing more. Reporting a health state derived
from the presence of a configuration value would be a claim dressed as a
measurement, which on an administration screen is worse than saying nothing.

Nothing here reads a secret's value. ``configured`` is derived from whether a
credential source is *set*, never from what it contains, and the dataclass
carries no field that could hold one.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CREDENTIAL_BROKER",
    "CREDENTIAL_STATIC",
    "ConnectorStatus",
    "connector_statuses",
]

CREDENTIAL_BROKER = "broker"
"""Tokens are minted per user by the broker; the hub holds none of its own."""

CREDENTIAL_STATIC = "static"
"""One deployment-wide token, which the hub does hold."""


@dataclass(frozen=True)
class ConnectorStatus:
    """One connector, as the panel shows it.

    Carries no secret and no field that could hold one: the closest it comes is
    ``credential``, which names *how* the connector is authenticated rather
    than what with.
    """

    key: str
    label: str
    configured: bool
    credential: str | None
    probeable: bool
    """Whether a hub-level health check could say anything.

    False for brokered connectors, and the reason is in the module docstring:
    without a credential of its own the hub can only answer this question by
    borrowing somebody's identity.
    """


_CONNECTORS = (
    ("google", "Google Workspace"),
    ("slack", "Slack"),
    ("github", "GitHub"),
    ("sharepoint", "SharePoint"),
)


def connector_statuses(connectors) -> list[ConnectorStatus]:
    """Describe every connector this deployment knows about.

    Every connector is listed, configured or not. An operator asking "is Slack
    set up" is answered by seeing Slack listed as unconfigured; a panel that
    omitted it would leave them unable to tell "not set up" from "not a thing
    this hub has".
    """

    statuses = []
    for key, label in _CONNECTORS:
        section = getattr(connectors, key, None)
        credential = _credential(section)
        statuses.append(
            ConnectorStatus(
                key=key,
                label=label,
                configured=credential is not None,
                credential=credential,
                probeable=credential == CREDENTIAL_STATIC,
            )
        )
    return statuses


def _credential(section) -> str | None:
    """How this connector authenticates, or ``None`` if it cannot.

    The broker is checked first: a deployment carrying both a broker URL and a
    static token is a brokered deployment with a leftover, and describing it as
    static would point an operator at the wrong thing when per-user access
    breaks.
    """

    if section is None:
        return None
    if getattr(section, "broker_token_url", ""):
        return CREDENTIAL_BROKER
    if getattr(section, "static_access_token", ""):
        return CREDENTIAL_STATIC
    return None
