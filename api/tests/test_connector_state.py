"""Turning a connector off, and having that actually mean something.

The switch lives in the database; the credentials stay in deployment
configuration. That split is the whole design: configuration decides what this
hub is *capable* of, and the switch decides what is *available* right now. A
disabled connector therefore looks exactly like an unconfigured one to every
route that uses it, which is a refusal those routes already handle.
"""

from __future__ import annotations

from collab_hub_api.config import ConnectorsConfig
from collab_hub_api.frames.connector_state import apply_disabled


def configured() -> ConnectorsConfig:
    return ConnectorsConfig.model_validate(
        {
            "slack": {"broker_token_url": "https://broker.example.com/token"},
            "github": {"static_access_token": "gh-not-a-real-token"},
            "google": {"broker_token_url": "https://broker.example.com/token"},
            "sharepoint": {"broker_token_url": "https://broker.example.com/microsoft/token"},
        }
    )


def test_a_disabled_connector_looks_unconfigured_to_everything_downstream():
    result = apply_disabled(configured(), {"slack"})

    assert result.slack.broker_token_url == ""
    assert result.slack.static_access_token == ""
    # Its neighbours are untouched.
    assert result.github.static_access_token == "gh-not-a-real-token"
    assert result.google.broker_token_url == "https://broker.example.com/token"
    assert result.sharepoint.broker_token_url == "https://broker.example.com/microsoft/token"


def test_sharepoint_is_switchable_and_keeps_its_non_credential_settings():
    result = apply_disabled(configured(), {"sharepoint"})

    assert result.sharepoint.broker_token_url == ""
    assert result.sharepoint.static_access_token == ""
    # Base URL and limits are not credentials; they stay so the section still
    # reads as "configured endpoint, no token" downstream.
    assert result.sharepoint.api_base_url == "https://graph.microsoft.com/v1.0"
    assert result.sharepoint.include_personal_onedrive is False


def test_disabling_nothing_returns_the_configuration_unchanged():
    original = configured()

    assert apply_disabled(original, set()) is original


def test_an_unknown_name_cannot_disable_something_by_accident():
    result = apply_disabled(configured(), {"fax"})

    assert result.slack.broker_token_url == "https://broker.example.com/token"


def test_the_original_configuration_is_not_mutated():
    """The app's own config object is shared; disabling must not scribble on it."""

    original = configured()
    apply_disabled(original, {"slack", "github"})

    assert original.slack.broker_token_url == "https://broker.example.com/token"
    assert original.github.static_access_token == "gh-not-a-real-token"
