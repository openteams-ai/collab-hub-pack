"""The ``cogs:`` config block (#87): cross-source validation and Secret indirection.

Per-source rules are covered in test_cog_registry.py against the model itself;
here every case goes through ``Config`` so the assertion is "this fails at
settings load", which is the whole point of validating in config.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from collab_hub_api.config import Config, resolve_cogs_source_secrets

HARBOR_URL = "https://harbor.example.com"
STATIC_URL = "https://registry.example.com"


def harbor_source(**overrides) -> dict:
    return {"id": "harbor-main", "kind": "harbor", "url": HARBOR_URL, "projects": ["cogs"], **overrides}


def static_source(**overrides) -> dict:
    return {"id": "public", "kind": "static", "url": STATIC_URL, "repositories": ["cogs/alpha"], **overrides}


def parse_cogs(**cogs) -> Config:
    return Config.parse({"cogs": cogs})


# One fixture holds every refused configuration in chart-values form and in
# the settings form the chart renders from it; scripts/chart_rules_parity.py
# proves the two forms equivalent (with helm) and this file feeds the settings
# form to Config. See the fixture's header.
NEGATIVE_CASES = Path(__file__).resolve().parents[2] / "scripts" / "testdata" / "chart" / "cogs-negative-cases.yaml"


def negative_cases() -> list[dict]:
    return yaml.safe_load(NEGATIVE_CASES.read_text())["cases"]


def accepted_cases() -> list[dict]:
    return yaml.safe_load(NEGATIVE_CASES.read_text()).get("accepted", [])


# --- Defaults and the enabled/sources coupling -------------------------------


def test_defaults_are_off_and_empty():
    cogs = Config.parse().cogs

    assert cogs.registry_sources == []
    assert cogs.index.enabled is False
    assert cogs.index.interval_seconds == 300
    assert cogs.index.run_on_startup is True


def test_sources_with_index_disabled_is_valid():
    # The read API can serve a static index: sources configured, no sweeps.
    cogs = parse_cogs(registry_sources=[harbor_source(), static_source()], index={"enabled": False}).cogs

    assert [source.id for source in cogs.registry_sources] == ["harbor-main", "public"]
    assert cogs.index.enabled is False


def test_index_enabled_requires_at_least_one_source():
    with pytest.raises(ValidationError, match="cogs.index.enabled is true but cogs.registry_sources is empty"):
        parse_cogs(index={"enabled": True})


def test_index_enabled_with_a_source_is_valid():
    cogs = parse_cogs(registry_sources=[static_source()], index={"enabled": True, "interval_seconds": 60}).cogs

    assert cogs.index.enabled is True
    assert cogs.index.interval_seconds == 60


@pytest.mark.parametrize("interval", [0, 9, 24 * 3600 + 1])
def test_index_interval_is_bounded(interval):
    with pytest.raises(ValidationError, match="interval_seconds"):
        parse_cogs(registry_sources=[static_source()], index={"interval_seconds": interval})


# --- Each validation failure, observed at settings load ----------------------


def test_unknown_kind_fails():
    with pytest.raises(ValidationError, match="kind"):
        parse_cogs(registry_sources=[harbor_source(kind="quay")])


def test_duplicate_id_fails_and_names_both_positions():
    with pytest.raises(ValidationError, match=r"registry_sources\[1\] reuses id 'main'.*registry_sources\[0\]"):
        parse_cogs(registry_sources=[harbor_source(id="main"), static_source(id="main")])


def test_harbor_without_projects_fails():
    with pytest.raises(ValidationError, match="requires at least one entry in projects"):
        parse_cogs(registry_sources=[harbor_source(projects=[])])


def test_static_without_repositories_or_index_url_fails():
    with pytest.raises(ValidationError, match="requires repositories and/or index_url"):
        parse_cogs(registry_sources=[static_source(repositories=[])])


def test_unknown_source_key_fails_rather_than_being_ignored():
    # A typo in a secret indirection (``pasword_env``) must not leave the
    # source silently unauthenticated.
    with pytest.raises(ValidationError, match="pasword_env"):
        parse_cogs(registry_sources=[harbor_source(credentials={"username": "robot", "pasword_env": "X"})])


# --- Secret indirection: *_env names a variable the chart mounts -------------


def test_credentials_and_webhook_resolve_from_named_env_vars(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME", "robot$cogs+indexer")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD", " robot-secret-value ")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET", "hook-secret-value")

    [source] = parse_cogs(
        registry_sources=[
            harbor_source(
                credentials={
                    "username_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME",
                    "password_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD",
                },
                webhook_secret_env="COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET",
            )
        ]
    ).cogs.registry_sources

    assert source.credentials.configured
    assert source.credentials.username == "robot$cogs+indexer"
    # Surrounding whitespace is removed on the env route as it is inline; the
    # policy and its test are below (test_surrounding_whitespace_...).
    assert source.credentials.password.get_secret_value() == "robot-secret-value"
    assert source.webhook_secret.get_secret_value() == "hook-secret-value"
    # The built model's repr masks the secrets (SecretStr); the error-text
    # guarantee is a separate test below.
    assert "robot-secret-value" not in repr(source)
    assert "hook-secret-value" not in repr(source)


def test_surrounding_whitespace_is_trimmed_the_same_on_both_routes(monkeypatch):
    """One whitespace policy for a secret, however it arrives.

    Inline values are stripped by the registry models' own validators (as
    ``WebConfig`` strips its client and session secrets). A value read from
    the environment is inserted as a ``SecretStr``, which those validators
    pass through, so the resolver applies the same strip. The case that
    matters operationally is the trailing newline a ``--from-file`` Secret
    carries: verbatim, it would fail authentication at the registry with an
    error naming nothing useful. Whitespace-only stays "empty" (tested
    above); a credential whose surrounding whitespace is significant is not
    supported, and the docs say so.
    """

    monkeypatch.setenv("PW", " robot-secret-value\n")
    monkeypatch.setenv("HOOK", "\thook-secret-value\n")

    [from_env] = parse_cogs(
        registry_sources=[
            harbor_source(credentials={"username": "robot", "password_env": "PW"}, webhook_secret_env="HOOK")
        ]
    ).cogs.registry_sources
    [inline] = parse_cogs(
        registry_sources=[
            harbor_source(
                credentials={"username": "robot", "password": " robot-secret-value\n"},
                webhook_secret="\thook-secret-value\n",
            )
        ]
    ).cogs.registry_sources

    assert from_env.credentials.password.get_secret_value() == "robot-secret-value"
    assert inline.credentials.password.get_secret_value() == "robot-secret-value"
    assert from_env.webhook_secret.get_secret_value() == "hook-secret-value"
    assert inline.webhook_secret.get_secret_value() == "hook-secret-value"


def test_inline_username_with_password_from_env(monkeypatch):
    monkeypatch.setenv("PW", "hunter2-secret")

    [source] = parse_cogs(
        registry_sources=[harbor_source(credentials={"username": "robot", "password_env": "PW"})]
    ).cogs.registry_sources

    assert source.credentials.password.get_secret_value() == "hunter2-secret"


def test_missing_env_var_fails_at_startup_and_names_it(monkeypatch):
    monkeypatch.delenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        parse_cogs(
            registry_sources=[
                harbor_source(
                    credentials={"username": "robot", "password_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD"}
                )
            ]
        )

    message = str(excinfo.value)
    assert "registry_sources[0] ('harbor-main').credentials.password_env" in message
    assert "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD, which is not set" in message


def test_empty_env_var_is_treated_as_missing(monkeypatch):
    # An empty Secret value is what a wrong `key:` under existingSecret yields.
    monkeypatch.setenv("WH", "   ")

    with pytest.raises(ValidationError, match="names WH, which is empty"):
        parse_cogs(registry_sources=[harbor_source(webhook_secret_env="WH")])


def test_blank_env_name_is_refused():
    with pytest.raises(ValidationError, match="password_env must name an environment variable"):
        parse_cogs(registry_sources=[harbor_source(credentials={"username": "robot", "password_env": " "})])


def test_inline_value_and_env_indirection_together_are_refused(monkeypatch):
    monkeypatch.setenv("PW", "from-env")

    with pytest.raises(ValidationError, match="sets both password and password_env"):
        credentials = {"username": "robot", "password": "inline", "password_env": "PW"}
        parse_cogs(registry_sources=[harbor_source(credentials=credentials)])


def test_env_password_without_username_still_trips_both_or_neither(monkeypatch):
    # Resolution happens before the model's own rules, which then still apply.
    monkeypatch.setenv("PW", "secret-value")

    with pytest.raises(ValidationError, match="must be set together"):
        parse_cogs(registry_sources=[harbor_source(credentials={"password_env": "PW"})])


def _error_text(excinfo: pytest.ExceptionInfo[ValidationError]) -> str:
    """Every rendering a startup log or a traceback could carry."""

    return "\n".join([str(excinfo.value), repr(excinfo.value), repr(excinfo.value.errors()), excinfo.value.json()])


def test_resolved_password_never_appears_in_validation_errors(monkeypatch):
    # The resolver inserts the password *before* the model's own rules run,
    # and pydantic quotes the failing input in its error text; a source with
    # a password but no username must fail without echoing the password.
    monkeypatch.setenv("PW", "REVIEW_SYNTHETIC_PASSWORD")

    with pytest.raises(ValidationError, match="must be set together") as excinfo:
        parse_cogs(registry_sources=[harbor_source(credentials={"password_env": "PW"})])

    assert "REVIEW_SYNTHETIC_PASSWORD" not in _error_text(excinfo)


def test_resolved_webhook_secret_never_appears_in_validation_errors(monkeypatch):
    # kind=static refuses a webhook secret; the refusal quotes the source, and
    # the source now holds the resolved secret.
    monkeypatch.setenv("WH", "REVIEW_SYNTHETIC_WEBHOOK")

    with pytest.raises(ValidationError, match="has no webhook") as excinfo:
        parse_cogs(registry_sources=[static_source(webhook_secret_env="WH")])

    assert "REVIEW_SYNTHETIC_WEBHOOK" not in _error_text(excinfo)


def test_resolved_secrets_stay_hidden_when_an_unrelated_field_fails(monkeypatch):
    # A different field's failure on the same source still quotes the whole
    # source dict; the secrets inside it must be masked there too.
    monkeypatch.setenv("U", "robot")
    monkeypatch.setenv("PW", "REVIEW_SYNTHETIC_PASSWORD")
    monkeypatch.setenv("WH", "REVIEW_SYNTHETIC_WEBHOOK")

    with pytest.raises(ValidationError) as excinfo:
        parse_cogs(
            registry_sources=[
                harbor_source(
                    projects=[],
                    credentials={"username_env": "U", "password_env": "PW"},
                    webhook_secret_env="WH",
                )
            ]
        )

    text = _error_text(excinfo)
    assert "requires at least one entry in projects" in text
    assert "REVIEW_SYNTHETIC_PASSWORD" not in text
    assert "REVIEW_SYNTHETIC_WEBHOOK" not in text


def test_config_errors_do_not_echo_inputs_at_all():
    # Defence in depth behind the SecretStr wrapping: the settings class hides
    # every input, so a Postgres URL with a password in it is not quoted either.
    with pytest.raises(ValidationError) as excinfo:
        postgres = {"url": "postgresql://u:REVIEW_SYNTHETIC_DB_PW@db/x", "pool": {"max_size": 0}}
        Config.parse({"frames": {"postgres": postgres}})

    assert "max_size" in str(excinfo.value)
    assert "REVIEW_SYNTHETIC_DB_PW" not in _error_text(excinfo)
    assert "input_value" not in str(excinfo.value)


def test_resolver_passes_non_mappings_through():
    sentinel = object()

    assert resolve_cogs_source_secrets(sentinel, 0, {}) is sentinel


def test_resolver_does_not_mutate_its_input():
    raw = {"id": "x", "credentials": {"username": "u", "password_env": "PW"}}

    resolved = resolve_cogs_source_secrets(raw, 0, {"PW": "pw"})

    assert raw["credentials"] == {"username": "u", "password_env": "PW"}
    assert resolved["credentials"]["username"] == "u"
    assert resolved["credentials"]["password"].get_secret_value() == "pw"
    assert "pw" not in repr(resolved)


# --- The chart's contract: one JSON env var for the list, plus named Secrets --


def test_env_round_trip_as_the_chart_renders_it(monkeypatch):
    """The exact shape api-deployment.yaml produces: JSON list + per-source Secret env vars."""

    sources = [
        {
            "id": "harbor-main",
            "kind": "harbor",
            "url": HARBOR_URL,
            "api_url": "http://harbor-core.harbor.svc:80",
            "token_url": "http://harbor-core.harbor.svc:80/service/token",
            "projects": ["cogs"],
            "ca_bundle_path": "/etc/collab-hub/cogs-ca/ca.crt",
            "credentials": {
                "username_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME",
                "password_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD",
            },
            "webhook_secret_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET",
        },
        {
            "id": "public.mirror",
            "kind": "static",
            "url": STATIC_URL,
            "index_url": f"{STATIC_URL}/catalog.v1.json",
            "credentials": {
                "username_env": "COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_USERNAME",
                "password_env": "COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_PASSWORD",
            },
        },
    ]
    monkeypatch.setenv("COLLAB_HUB_API__COGS__REGISTRY_SOURCES", json.dumps(sources))
    monkeypatch.setenv("COLLAB_HUB_API__COGS__INDEX__ENABLED", "true")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS", "120")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP", "false")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME", "robot$cogs+indexer")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD", "robot-secret-value")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET", "hook-secret-value")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_USERNAME", "reader")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_PASSWORD", "reader-secret")

    cogs = Config().cogs

    assert cogs.index.enabled is True
    assert cogs.index.interval_seconds == 120
    assert cogs.index.run_on_startup is False
    harbor, static = cogs.registry_sources
    assert harbor.kind == "harbor"
    assert harbor.api_url == "http://harbor-core.harbor.svc:80"
    assert harbor.ca_bundle_path == "/etc/collab-hub/cogs-ca/ca.crt"
    assert harbor.credentials.username == "robot$cogs+indexer"
    assert harbor.credentials.password.get_secret_value() == "robot-secret-value"
    assert harbor.webhook_secret.get_secret_value() == "hook-secret-value"
    assert static.kind == "static"
    assert static.credentials.password.get_secret_value() == "reader-secret"
    assert static.webhook_secret.get_secret_value() == ""


def test_env_round_trip_missing_secret_names_the_variable(monkeypatch):
    monkeypatch.setenv(
        "COLLAB_HUB_API__COGS__REGISTRY_SOURCES",
        json.dumps([harbor_source(credentials={"username_env": "U", "password_env": "P"})]),
    )
    monkeypatch.setenv("U", "robot")
    monkeypatch.delenv("P", raising=False)

    with pytest.raises(ValidationError, match="password_env names P, which is not set"):
        Config()


def test_index_style_env_override_is_not_a_supported_route(monkeypatch):
    """Pins the measurement behind the *_env design (pydantic-settings 2.14).

    Layered over a JSON list, ``..._SOURCES__0__CREDENTIALS__PASSWORD`` is
    silently ignored. If a future pydantic-settings starts honoring it this
    test fails, which is the cue to revisit the indirection — not a bug.
    """

    monkeypatch.setenv(
        "COLLAB_HUB_API__COGS__REGISTRY_SOURCES",
        json.dumps([static_source(credentials={"username": "reader", "password": "inline-secret"})]),
    )
    monkeypatch.setenv("COLLAB_HUB_API__COGS__REGISTRY_SOURCES__0__CREDENTIALS__PASSWORD", "from-index-var")

    [source] = Config().cogs.registry_sources

    assert source.credentials.password.get_secret_value() == "inline-secret"


# --- cogs.serve: pulls through the Hub (issue #179) ---------------------------


def test_serving_is_off_by_default_with_documented_limits():
    serve = Config.parse().cogs.serve

    assert serve.enabled is False and serve.public_url == ""
    assert serve.credential_ttl_seconds == 900, "fifteen minutes"
    assert serve.token_ttl_seconds == 300
    assert serve.max_blob_bytes == 1024**3
    assert serve.max_blob_seconds == 900


@pytest.mark.parametrize(
    ("public_url", "host"),
    [
        ("https://hub.example.com", "hub.example.com"),
        ("  https://hub.example.com  ", "hub.example.com"),
        ("https://hub.example.com:8443", "hub.example.com:8443"),
        ("http://localhost:8000", "localhost:8000"),
        ("https://[::1]:5000", "[::1]:5000"),
        # The scheme's default port is left out and the host is lowercase, so
        # the value is exactly the authority of the Hub API origin.
        ("https://hub.example.com:443", "hub.example.com"),
        ("http://hub.example.com:80", "hub.example.com"),
        ("https://hub.example.com:80", "hub.example.com:80"),
        ("https://Hub.Example.com", "hub.example.com"),
    ],
)
def test_the_registry_host_is_the_public_urls_host_and_port(public_url, host):
    serve = parse_cogs(registry_sources=[static_source()], serve={"enabled": True, "public_url": public_url}).cogs.serve

    assert serve.host == host
    assert serve.public_url == public_url.strip()


@pytest.mark.parametrize(
    ("public_url", "message"),
    [
        ("hub.example.com", "must be an http\\(s\\) origin with a host"),
        ("https://", "must be an http\\(s\\) origin with a host"),
        ("https://hub.example.com:99999", "has an invalid port"),
        ("https://hub.example.com/v2", "must be a bare origin"),
        ("https://hub.example.com/", "must be a bare origin"),
        ("https://hub.example.com?x=1", "must be a bare origin"),
        ("https://hub.example.com/#", "must be a bare origin"),
        ("https://user:secret-pw@hub.example.com", "must not embed a username or password"),
    ],
)
def test_a_public_url_that_is_not_a_bare_origin_is_refused(public_url, message):
    with pytest.raises(ValidationError, match=message) as caught:
        parse_cogs(registry_sources=[static_source()], serve={"enabled": True, "public_url": public_url})
    assert "secret-pw" not in str(caught.value)


def test_blob_redirect_hosts_are_hostnames_or_dot_suffixes():
    source = static_source(blob_redirect_hosts=[" storage.example.com ", ".s3.amazonaws.com", "10.0.0.7"])
    parsed = parse_cogs(registry_sources=[source]).cogs.registry_sources[0]
    assert parsed.blob_redirect_hosts == ["storage.example.com", ".s3.amazonaws.com", "10.0.0.7"]
    assert parse_cogs(registry_sources=[static_source()]).cogs.registry_sources[0].blob_redirect_hosts == []
    bad_entries = ("https://storage.example.com", "storage.example.com:9000", "Storage.Example.com", "a/b", "", ".")
    for bad in (*bad_entries, "*.x"):
        with pytest.raises(ValidationError, match="blob_redirect_hosts entry"):
            parse_cogs(registry_sources=[static_source(blob_redirect_hosts=[bad])])


def test_serving_needs_a_source_and_a_public_url_only_when_enabled():
    # Off: the rest of the block is inert, so a values file can carry it ahead of the switch.
    assert parse_cogs(serve={"public_url": "https://hub.example.com"}).cogs.serve.enabled is False
    with pytest.raises(ValidationError, match="cogs.serve.enabled is true but cogs.registry_sources is empty"):
        parse_cogs(serve={"enabled": True, "public_url": "https://hub.example.com"})
    with pytest.raises(ValidationError, match="cogs.serve.enabled is true but cogs.serve.public_url is empty"):
        parse_cogs(registry_sources=[static_source()], serve={"enabled": True})


def test_serving_round_trips_through_the_environment_as_the_chart_renders_it(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__COGS__REGISTRY_SOURCES", json.dumps([static_source()]))
    monkeypatch.setenv("COLLAB_HUB_API__COGS__SERVE__ENABLED", "true")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__SERVE__PUBLIC_URL", "https://hub.example.com")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__SERVE__MAX_BLOB_BYTES", "5368709120")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__SERVE__CREDENTIAL_TTL_SECONDS", "600")

    serve = Config().cogs.serve

    assert serve.enabled is True and serve.host == "hub.example.com"
    assert serve.max_blob_bytes == 5 * 1024**3 and serve.credential_ttl_seconds == 600


# --- Parity with the chart: the same refusals, from one fixture ---------------


def test_negative_case_fixture_is_well_formed():
    cases = negative_cases()
    names = [case["name"] for case in cases]

    assert len(names) == len(set(names)), "duplicate case names"
    for case in cases:
        assert "chart_error" in case, case["name"]
        assert ("app_error" in case) != ("why_chart_only" in case), (
            f"{case['name']}: exactly one of app_error / why_chart_only"
        )
        if "app_error" in case:
            assert "settings" in case, f"{case['name']}: app_error needs a settings form"


@pytest.mark.parametrize(
    "case",
    [case for case in negative_cases() if "settings" in case],
    ids=lambda case: case["name"],
)
def test_the_api_agrees_with_the_chart_on_each_refusal(case, monkeypatch):
    """The API side of scripts/chart_rules_parity.py.

    A case with ``app_error`` is refused by both layers; a case with
    ``why_chart_only`` is refused by the chart but must be *accepted* here,
    because it is what the chart would have rendered — asserting acceptance
    is what keeps the reason in ``why_chart_only`` honest.
    """

    for name, value in case.get("env", {}).items():
        monkeypatch.setenv(name, value)

    if "app_error" in case:
        with pytest.raises(ValidationError, match=case["app_error"]):
            Config.parse({"cogs": case["settings"]})
    else:
        Config.parse({"cogs": case["settings"]})


@pytest.mark.parametrize("case", accepted_cases(), ids=lambda case: case["name"])
def test_the_api_accepts_each_boundary_configuration_the_chart_renders(case):
    """The API side of the fixture's ``accepted`` section.

    The chart side proves these render and that the rendered settings equal
    this form; here the same form must build a Config. Together they pin the
    schema against drifting stricter than the API (ports, IPv6 literals,
    in-cluster hostnames, dotted project names).
    """

    cogs = Config.parse({"cogs": case["settings"]}).cogs

    assert [source.id for source in cogs.registry_sources] == [s["id"] for s in case["settings"]["registry_sources"]]
