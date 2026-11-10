"""What the panel can truthfully say about a connector.

The interesting constraint is that most connectors here are *brokered*: the
credential is minted per user, so the hub holds no token of its own and cannot
answer "does this connector work" on anyone's behalf. Reporting a green tick
from a configuration value would be a claim the server cannot support.
"""

from __future__ import annotations

from collab_hub_api.config import ConnectorsConfig
from collab_hub_api.frames.connector_status import CREDENTIAL_BROKER, CREDENTIAL_STATIC, connector_statuses


def test_a_connector_with_no_credential_source_is_reported_as_unconfigured():
    statuses = {status.key: status for status in connector_statuses(ConnectorsConfig())}

    assert set(statuses) == {"google", "slack", "github", "sharepoint"}
    assert all(not status.configured for status in statuses.values())
    assert all(status.credential is None for status in statuses.values())


def test_a_brokered_connector_is_configured_but_cannot_be_probed_by_the_hub():
    config = ConnectorsConfig.model_validate({"slack": {"broker_token_url": "https://broker.example.com/token"}})

    slack = {status.key: status for status in connector_statuses(config)}["slack"]

    assert slack.configured
    assert slack.credential == CREDENTIAL_BROKER
    assert slack.probeable is False


def test_a_static_credential_is_probeable_and_never_disclosed():
    config = ConnectorsConfig.model_validate({"github": {"static_access_token": "ghp-not-a-real-token"}})

    github = {status.key: status for status in connector_statuses(config)}["github"]

    assert github.configured
    assert github.credential == CREDENTIAL_STATIC
    assert github.probeable is True
    assert "ghp-not-a-real-token" not in repr(github)
