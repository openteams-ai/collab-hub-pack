import os
import re
import sys
from collections.abc import Mapping
from typing import Any, Literal, Self

import l2sl
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .cogs.catalog import (
    CogCatalogStore,
    InMemoryCogCatalogStore,
    PostgresCogCatalogStore,
    UnavailableCogCatalogStore,
)
from .cogs.indexer import CogIndexer
from .cogs.registry import CogRegistrySourceConfig, build_registry_sources
from .frames.account_provisioning import DisabledServiceAccessGranter, ServiceAccessGranter
from .frames.active_state import (
    ActiveFrameStore,
    DisabledActiveFrameStore,
    InMemoryActiveFrameStore,
    PostgresActiveFrameStore,
)
from .frames.collab_schema import check_collab_schema_version, run_collab_schema_migrations
from .frames.db import (
    DEFAULT_MAX_SIZE,
    DEFAULT_MAX_WAITING,
    DEFAULT_MIN_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_WAITING_LIMIT,
    POOL_SIZE_LIMIT,
    TIMEOUT_SECONDS_LIMIT,
    PostgresPools,
)
from .frames.groups import (
    FrameGroupStore,
    InMemoryFrameGroupStore,
    PostgresFrameGroupStore,
    UnavailableFrameGroupStore,
)
from .frames.history import (
    FrameHistoryStore,
    InMemoryFrameHistoryStore,
    PostgresFrameHistoryStore,
    UnavailableFrameHistoryStore,
)
from .frames.invitation_email import (
    ConfiguredInvitationEmailDelivery,
    DisabledInvitationEmailDelivery,
    SesInvitationEmailProvider,
    validate_accept_url,
    validate_mailbox,
)
from .frames.invitations import (
    InvitationService,
    PostgresInvitationService,
    UnavailableInvitationService,
)
from .frames.keycloak_service_access import KeycloakServiceAccessGranter
from .frames.orgs import InMemoryOrgStore, OrgStore, PostgresOrgStore, UnavailableOrgStore
from .frames.store import FrameStore, LocalFsFrameStore, S3FrameStore
from .frames.usage import (
    InMemoryUsageStore,
    PostgresUsageStore,
    UnavailableUsageStore,
    UsageStore,
)
from .tasks.store import InMemoryTaskStore, PostgresTaskStore
from .user_directory import DisabledUserDirectoryClient, KeycloakUserDirectoryClient, UserDirectoryClient


def interactive_session() -> bool:
    return sys.stdout.isatty()


class ServerConfig(BaseModel):
    """How uvicorn is bound and what it believes about the hop in front of it.

    ``proxy_headers`` stays False here because the process default has to be
    the safe one for a server reached directly (local runs, port-forwards):
    trusting ``X-Forwarded-*`` from an arbitrary client lets it dictate the
    scheme and the client address that end up in logs. Behind a proxy that
    always sets those headers — the Nebari gateway or the chart's Ingress —
    the correct value is True, and the Helm chart turns it on by default for
    exactly that reason (``server.proxyHeaders``). ``forwarded_allow_ips``
    bounds which peers may set them; ``["*"]`` is only sound when the pod is
    unreachable except through the proxy.
    """

    hostname: str = "127.0.0.1"
    port: int = 8000
    proxy_headers: bool = False
    forwarded_allow_ips: list[str] = Field(default_factory=lambda: ["*"])
    root_path: str = ""


class LoggingConfig(BaseModel):
    level: l2sl.LogLevel = l2sl.LogLevel("info")
    as_json: bool = Field(default_factory=lambda: not interactive_session())


class ObservabilityConfig(BaseModel):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class CORSConfig(BaseModel):
    """Cross-origin rules for browser callers.

    ``allowed_origins`` keeps its historical ``["*"]`` default so that
    upgrading an existing deployment — or anyone running this image without the
    Helm chart — does not silently lose browser callers. A wildcard is still
    the wrong answer for a multi-tenant hub: native clients send no ``Origin``
    and the server's own pages are same-origin, so the grant buys nothing and
    hands every site on the internet a cross-origin call path. The chart
    therefore narrows it to ``[]`` for standalone/ingress exposure, which is
    new and has no installed base; see ``security.cors.allowedOrigins``.
    """

    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    allowed_headers: list[str] = Field(default_factory=lambda: ["Authorization", "Content-Type"])
    allow_credentials: bool = False

    @model_validator(mode="after")
    def _reject_wildcard_with_credentials(self) -> Self:
        # Starlette echoes the request Origin when allow_origins is ["*"] and
        # credentials are allowed, so the combination is not "wildcard without
        # cookies" — it is "every site may make credentialed calls".
        if "*" in self.allowed_origins and self.allow_credentials:
            raise ValueError(
                "security.cors.allowed_origins may not contain '*' when "
                "allow_credentials is true; name the origins explicitly"
            )
        return self


PathAccess = Literal["public", "authenticated"]
"""How a request path is protected.

``public`` reaches the route with no credentials. ``authenticated`` requires a
verified IdToken cookie or bearer token (the same check the API routes use)
before the request is routed at all.

Role-scoped levels (operator-only ``/admin``, owner-only ``/org``) belong with
the browser surface in issue #88; they are deliberately absent here rather than
accepted-and-ignored, so a map can never claim a protection the server does not
enforce.
"""


class PathRule(BaseModel):
    """One entry of the per-path protection map."""

    path: str
    match: Literal["prefix", "exact"] = "prefix"
    access: PathAccess = "authenticated"

    @model_validator(mode="after")
    def _check_path(self) -> Self:
        if not self.path.startswith("/"):
            raise ValueError(f"security.paths entry {self.path!r} must start with '/'")
        return self


def recommended_path_rules() -> list[PathRule]:
    """The map a hardened deployment runs, paired with ``default_access="authenticated"``.

    Deliberately *not* the process default. Enforcing protection by default
    would change how every already-running deployment behaves the moment it is
    upgraded: an in-cluster Prometheus scrape of ``/metrics`` would start
    answering 401, and so would the hub landing-page link to ``/`` unless that
    install happens to have usable IdToken JWKS settings. Protection is opted
    into instead, and the Helm chart opts in automatically for
    standalone/ingress exposure, which is new and has no installed base
    (``security.enforce``).

    ``/health`` and ``/health/db`` stay public because kubelet probes and
    uptime checks carry no credentials: a hardened map that drops them stops
    the pod passing its own probes. ``/v1/auth/cli`` stays public because a
    command-line client asks it how to sign in before it holds any credential;
    it names only what the realm's own discovery document publishes.
    """

    return [
        PathRule(path="/health", match="exact", access="public"),
        PathRule(path="/health/db", match="exact", access="public"),
        # Where the collab-hub CLI learns how to sign in, asked before it holds a token.
        PathRule(path="/v1/auth/cli", match="exact", access="public"),
        PathRule(path="/", match="exact", access="authenticated"),
        PathRule(path="/metrics", match="exact", access="authenticated"),
    ]


class SecurityConfig(BaseModel):
    cors: CORSConfig = Field(default_factory=CORSConfig)
    # The protection map is data, not code: the server enforces whatever the
    # map says, so a new public page (issue #88's /invite/accept) or an
    # in-cluster metrics scrape is a values change, not a code change.
    #
    # Empty, with default_access "public", so an unconfigured server behaves
    # exactly as it did before the map existed — route dependencies protect the
    # API and nothing else is enforced here. recommended_path_rules() is what a
    # hardened deployment sets; the chart renders it for ingress exposure.
    paths: list[PathRule] = Field(default_factory=list)
    # Applied to any path no rule matches.
    default_access: PathAccess = "public"


WEB_SESSION_LIFETIME_CEILING_SECONDS = 8 * 3600
"""Hard ceiling on a web session's absolute lifetime, and its default.

Not a suggestion: the session cookie is stateless and cannot be revoked
server-side before it expires, so this number *is* the window in which a
cookie captured before sign-out keeps asserting its holder's identity. The
documented risk argument rests on it, so the configuration may lower it and
may not raise it.
"""

WEB_SESSION_SECRET_MIN_LENGTH = 32
WEB_SESSION_SECRET_MIN_DISTINCT = 16
"""Floors for the signing secret.

Length alone admits ``"a" * 32`` and 32 spaces, which carry no entropy at all
while satisfying a "32 characters" rule. Counting distinct characters is a
crude proxy for entropy and is not a substitute for generating the value with
``secrets.token_urlsafe(32)``, but it does refuse the degenerate values a
human types when a config demands a long string. Whitespace is stripped first,
so padding cannot buy either floor.
"""


def _check_session_secret(secret: str) -> None:
    """Refuse a signing secret that is short or visibly patterned."""

    stripped = secret.strip()
    if len(stripped) < WEB_SESSION_SECRET_MIN_LENGTH:
        raise ValueError(
            f"web.session_secret must be at least {WEB_SESSION_SECRET_MIN_LENGTH} characters"
            " (whitespace stripped); it signs every browser session cookie. Generate it with"
            " `python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
        )
    if len(set(stripped)) < WEB_SESSION_SECRET_MIN_DISTINCT:
        raise ValueError(
            "web.session_secret has too few distinct characters to be a random secret; it"
            " signs every browser session cookie. Generate it with"
            " `python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
        )


class WebConfig(BaseModel):
    """The server-side browser surface (issue #88).

    Setting ``client_id`` enables the surface: it names the **confidential**
    OIDC client (``collab-web`` or similar) registered in the same Keycloak
    realm the bearer verifier trusts, distinct from the desktop's public
    ``apollo-desktop`` client. ``issuer_url`` may be left empty to inherit the
    bearer issuer (``FRAMES_BEARER_ISSUER``) — inheriting the *realm* is safe
    and deliberate; the token **audience** is never inherited and is always
    this client's own id (the issue #83 lesson).

    ``session_secret`` signs the session and transient cookies; it must be
    high-entropy and identical on every replica. ``public_base_url`` overrides
    request-derived redirect-URI construction for deployments whose forwarded
    headers are not trusted.
    """

    client_id: str = ""
    client_secret: str = ""
    issuer_url: str = ""
    scope: str = "openid email profile"
    session_secret: str = ""
    session_lifetime_seconds: int = Field(
        default=WEB_SESSION_LIFETIME_CEILING_SECONDS,
        gt=0,
        le=WEB_SESSION_LIFETIME_CEILING_SECONDS,
    )
    public_base_url: str = ""
    admin_ui_dist: str = ""
    """Where the built admin panel lives, if this deployment ships one.

    Empty, or a directory with no ``index.html``, mounts no panel routes at
    all: ``/admin`` then answers exactly as it did before the panel existed.
    That is deliberate -- a route that exists but cannot find its own index
    would turn a missing build step into a 500 on somebody's first visit.
    """
    admin_group: str = ""
    """The identity-provider group whose members hold the operator role.

    Unset means no sync: roles come from ``collab_platform_roles`` and only
    from there, exactly as they did before the sync existed. That is the
    default on purpose -- an empty group name would match nothing, and a sync
    that matched nothing would revoke every synced row on first sign-in.

    Carrying the group into the token is realm configuration, not a setting
    here: the client needs a groups mapper (or the ``groups`` client scope) so
    that the ID token actually holds the claim. Without it the claim is absent,
    and sign-in skips the sync entirely (logging
    ``platform_role_sync_no_groups_claim``): nobody is granted and nobody is
    revoked.
    """

    @field_validator(
        "client_id",
        "client_secret",
        "issuer_url",
        "session_secret",
        "public_base_url",
        "admin_ui_dist",
        "admin_group",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: Any) -> Any:
        """Normalize at the boundary so validation and use see the same value.

        These arrive from environment variables and YAML, both of which
        acquire stray whitespace easily. Validating a stripped value while
        *forwarding* the raw one is the worst of both: ``" secret "`` passed
        the "is it non-empty" check and was then POSTed to Keycloak with its
        spaces intact, which fails authentication with an error that names
        nothing useful. Stripping here means there is only one value.
        """

        return value.strip() if isinstance(value, str) else value

    @property
    def enabled(self) -> bool:
        return bool(self.client_id)

    @model_validator(mode="after")
    def _check_enabled_shape(self) -> Self:
        # Only the checks that need no environment: env fallbacks, transport
        # security, and the protection-map coherence check live in
        # web.surface.enforce_web_surface_preconditions, called by make_app.
        if not self.enabled:
            return self
        if not self.client_secret:
            raise ValueError(
                "web.client_id names a confidential client, which requires web.client_secret"
                " (a whitespace-only value is not a secret)"
            )
        _check_session_secret(self.session_secret)
        if "openid" not in self.scope.split():
            raise ValueError(
                f"web.scope must request the 'openid' scope, got {self.scope!r}: without it"
                " the token response carries no id_token and no sign-in can complete"
            )
        return self


class StorageConfig(BaseModel):
    frames_path: str = "/var/frames"


class FramesS3Config(BaseModel):
    bucket: str = ""
    prefix: str = "frames"
    endpoint_url: str = ""
    region: str = ""


class FramesPostgresPoolConfig(BaseModel):
    """Sizing, wait timeout, and waiter bound for the shared psycopg pools.

    One setting covers every pool the app creates (normally just the shared
    ``frames.postgres`` one). ``timeout_seconds`` bounds how long a request
    waits for a connection when the pool is exhausted or Postgres is down
    before failing with a 503; ``max_waiting`` bounds how many callers may be
    queued for a connection at once (0 disables the bound).

    The bounds are enforced here rather than left to psycopg: a pod that comes
    up with an unusable pool (``max_size`` below ``min_size``, a zero or
    negative timeout, an accidental ``max_size: 0``) is worse than one that
    refuses to start with a named configuration error.
    """

    min_size: int = Field(default=DEFAULT_MIN_SIZE, ge=0, le=POOL_SIZE_LIMIT)
    max_size: int = Field(default=DEFAULT_MAX_SIZE, ge=1, le=POOL_SIZE_LIMIT)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=TIMEOUT_SECONDS_LIMIT)
    max_waiting: int = Field(default=DEFAULT_MAX_WAITING, ge=0, le=MAX_WAITING_LIMIT)

    @model_validator(mode="after")
    def _check_pool_sizes(self) -> Self:
        if self.max_size < self.min_size:
            raise ValueError(
                f"frames.postgres.pool.max_size ({self.max_size}) must be greater than "
                f"or equal to min_size ({self.min_size})"
            )
        return self


class FramesPostgresConfig(BaseModel):
    """The single shared Postgres URL backing the relational frames features.

    Setting ``url`` lights up history and groups (required features) and is the
    fallback for active-state's Postgres backend. There is no per-feature
    ``disabled`` toggle — the only off state for history/groups is "no DB".
    """

    url: str = ""
    auto_migrate: bool = False
    pool: FramesPostgresPoolConfig = Field(default_factory=FramesPostgresPoolConfig)


class FramesActiveStatePostgresConfig(BaseModel):
    url: str = ""
    auto_migrate: bool = False


class FramesActiveStateConfig(BaseModel):
    backend: str = "disabled"
    postgres: FramesActiveStatePostgresConfig = Field(default_factory=FramesActiveStatePostgresConfig)


class FramesHistoryConfig(BaseModel):
    # Optional test/dev override only: "memory" forces the in-memory store.
    # Otherwise history rides the shared frames.postgres (503 when no URL is set).
    backend: str = ""


class FramesGroupsConfig(BaseModel):
    # Same override semantics as history: "memory" for tests/dev, else shared
    # frames.postgres (503 when no URL is set). No per-feature "disabled".
    backend: str = ""


class FramesUsageConfig(BaseModel):
    # Same override semantics as history: "memory" for tests/dev, else shared
    # frames.postgres (503 when no URL is set). No per-feature "disabled".
    backend: str = ""


class FramesOrgsConfig(BaseModel):
    # Same override semantics as history: "memory" is a test/dev override,
    # otherwise organization membership rides the shared frames.postgres. There
    # is deliberately no chart value for this: an in-memory membership table is
    # process-local, so on a deployment it would authorize differently on every
    # replica and forget every removal on restart.
    backend: str = ""


class FramesEmailSesConfig(BaseModel):
    sender_address: str = ""
    region: str = ""
    configuration_set: str = ""
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class FramesEmailConfig(BaseModel):
    provider: Literal["disabled", "ses"] = "disabled"
    accept_url: str = ""
    app_instructions: str = ""
    """How an invitee gets the desktop app, in their own words in the email.

    Required once a provider is set, and validated below with the rest. The
    approved copy (#153) has a step telling the reader to install the app, and
    nothing in the service can know how a given deployment distributes its
    build — so there is no truthful default, and a deployment that enables mail
    without setting this would send a placeholder to a real person. Failing at
    startup is the alternative, which is why it is validated rather than
    defaulted.
    """

    ses: FramesEmailSesConfig = Field(default_factory=FramesEmailSesConfig)

    @model_validator(mode="after")
    def _validate_enabled_provider(self) -> Self:
        if self.provider == "disabled":
            return self

        missing = [
            name
            for name, value in {
                "accept_url": self.accept_url,
                "app_instructions": self.app_instructions,
                "ses.sender_address": self.ses.sender_address,
                "ses.region": self.ses.region,
                "ses.configuration_set": self.ses.configuration_set,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"Missing required invitation email config: {', '.join(missing)}")
        validate_accept_url(self.accept_url)
        validate_mailbox(self.ses.sender_address)
        return self


class ServiceAccessKeycloakConfig(BaseModel):
    """Where the membership-writing credential lives (#180).

    Its own block rather than reuse of ``user_directory.keycloak``, because
    they are different credentials on purpose: that one is read-only and this
    one writes. Sharing the configuration would invite sharing the secret, and
    the read credential's whole value is that it cannot write.
    """

    issuer_url: str = ""
    token_url: str = ""
    admin_api_base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    group_ids: dict[str, str] = {}
    """Group path to Keycloak group id, for paths this credential cannot look up.

    Keycloak's membership endpoint needs an id. Resolving a path to one requires
    *reading* groups, and the credential this block configures is deliberately
    write-only: on collab-hub its measured boundary is 403 on every read,
    ``GET /groups`` included. Supplying the id keeps it that narrow, and means
    startup makes no call to the identity provider at all.

    A path absent from this mapping is looked up instead, which works only where
    the credential holds group-read authority. Both are supported because both
    are legitimate; only one of them needs a wider credential.
    """


class FramesServiceAccessConfig(BaseModel):
    """Which identity-provider groups an accepted invitation grants (#180).

    A list rather than a boolean, and **empty by default**, because the two
    questions this answers change independently: whether a deployment grants
    anything automatically, and what it grants. A deployment that has not
    decided grants nothing, which is also the only safe default -- the
    behaviour this replaced granted membership at account creation and
    therefore reached anyone who self-registered
    (an internal issue).

    Configuration rather than code because the current answer is temporary.
    The initial invites grant everyone who accepts; narrowing that later must
    be a values change, not a release.

    Names are group paths as the identity provider spells them (``/llm``, or
    ``/services/llm`` if service groups are nested). Nesting does not change
    what appears in a token on this deployment -- the group-membership mapper
    is configured ``full.path: false``, so the claim carries bare names either
    way -- but the *grant* addresses the group by path.
    """

    grant_on_acceptance: list[str] = Field(default_factory=list)
    keycloak: ServiceAccessKeycloakConfig = Field(default_factory=ServiceAccessKeycloakConfig)

    @model_validator(mode="after")
    def _validate(self) -> "FramesServiceAccessConfig":
        for name in self.grant_on_acceptance:
            if not name or not name.strip():
                raise ValueError("frames.service_access.grant_on_acceptance entries must not be blank")
            if name != name.strip():
                raise ValueError(
                    f"frames.service_access.grant_on_acceptance entry {name!r} has surrounding whitespace: "
                    "a group path is matched exactly, and a stray space fails at grant time rather than here"
                )
        duplicates = {name for name in self.grant_on_acceptance if self.grant_on_acceptance.count(name) > 1}
        if duplicates:
            raise ValueError(f"frames.service_access.grant_on_acceptance lists {sorted(duplicates)} more than once")
        return self


class ModelAccessKeycloakConfig(BaseModel):
    """The credential the admin panel uses to change group membership.

    Separate from ``frames.service_access``'s credential on purpose: that one
    can only add a new account to a group and deliberately cannot read
    anything, while this one must list members and remove them. Widening the
    first would grow the invitation path's authority because an admin screen
    needed something.

    Whatever client this names must hold ``Groups/view-members``,
    ``Groups/manage-membership`` and ``Users/manage-group-membership``, and
    must **not** hold ``Groups/manage-members``: over a member of a group that
    scope also permits password reset and deletion, and membership control plus
    account control compose into account takeover. See
    :mod:`.frames.group_membership`.
    """

    token_url: str = ""
    admin_api_base_url: str = ""
    client_id: str = ""
    client_secret: str = ""


class ModelAccessConfig(BaseModel):
    """The models the hub offers and the groups that gate them."""

    catalog_base_url: str = ""
    """The serving layer's origin, read for ``GET /v1/models``. Empty disables
    the catalogue view rather than failing the panel."""

    keycloak: ModelAccessKeycloakConfig = Field(default_factory=ModelAccessKeycloakConfig)

    group_ids: dict[str, str] = Field(default_factory=dict)
    """Group path to Keycloak group id, and the managed set.

    Both a lookup table and a boundary: a group absent from this map is refused
    before any request is made, so the panel can only ever touch groups a
    values file named. Ids rather than lookups for the reason
    ``frames.service_access`` documents -- resolving a path needs group-read
    authority, and startup should not depend on the identity provider.
    """

    model_groups: dict[str, str] = Field(default_factory=dict)
    """Model id to the group path gating it. A model with no entry is shown as
    ungated rather than hidden: the catalogue is the serving layer's, and a
    panel that silently dropped models would misrepresent it."""


class FramesInvitationsConfig(BaseModel):
    """What invitation acceptance requires of the accepter's identity."""

    require_verified_email: bool = True
    """Whether Gate B additionally requires the identity provider to have
    verified the address (#190).

    **True is the default and must stay the default.** Turning it off is a
    deliberate deployment trade, and a default that silently weakened an
    existing deployment on upgrade would be the wrong kind of convenient.

    Gate B is two checks: the accepter's address is verified, *and* it equals
    the invited address. Setting this false drops the first and keeps the
    second, on the argument that the invitation token is itself proof of
    mailbox control — it is a 256-bit secret delivered only to the invited
    address, which is exactly what a verification link proves. Requiring both
    is defence in depth rather than one necessary check.

    **What is given up, stated so a deployment chooses it knowingly.** Once the
    identity provider stops verifying addresses, the ``email`` claim is
    *self-asserted*: whoever holds the invitation link can register an account
    typing the invited address, or point an existing account at it, and redeem.
    So the invitation becomes usable by **anyone who obtains the link by any
    means** -- forwarding and shared mailboxes are the mundane cases, not the
    boundary.

    What they receive is the organization membership and role the invitation
    grants, plus anything ``frames.service_access.grant_on_acceptance`` grants
    at acceptance -- identity-provider group membership included. And the
    account they end up with carries the invited person's address permanently,
    which is an identity-confusion problem in the member list on top of the
    access one.

    **What the retained address match does and does not buy.** With verification
    required it is an access control: an accepter must prove control of the
    invited address. Without it, the match is a *labelling* property -- the
    address recorded on the membership row is the invited one -- and not a
    barrier, because the claim it compares is unverified. Keeping it
    unconditional is still right; it is simply not the thing standing between a
    link-holder and the grant.

    **When to leave it true.** Any deployment whose invitees arrive through an
    identity provider with ``trustEmail``: those accounts are already verified
    on creation, so the check costs nothing and the invitee never sees a
    verification step. Turning it off buys nothing there.

    **When false is reasonable.** A password-based deployment with no identity
    provider, a known invitee list, and a verification round trip that is
    friction rather than assurance.
    """

class FramesConfig(BaseModel):
    storage_backend: str = "local"
    s3: FramesS3Config = Field(default_factory=FramesS3Config)
    postgres: FramesPostgresConfig = Field(default_factory=FramesPostgresConfig)
    active_state: FramesActiveStateConfig = Field(default_factory=FramesActiveStateConfig)
    history: FramesHistoryConfig = Field(default_factory=FramesHistoryConfig)
    groups: FramesGroupsConfig = Field(default_factory=FramesGroupsConfig)
    usage: FramesUsageConfig = Field(default_factory=FramesUsageConfig)
    orgs: FramesOrgsConfig = Field(default_factory=FramesOrgsConfig)
    email: FramesEmailConfig = Field(default_factory=FramesEmailConfig)
    service_access: FramesServiceAccessConfig = Field(default_factory=FramesServiceAccessConfig)
    model_access: ModelAccessConfig = Field(default_factory=ModelAccessConfig)
    invitations: FramesInvitationsConfig = Field(default_factory=FramesInvitationsConfig)
    mcp_session_manager_enabled: bool = True


class GoogleConnectorConfig(BaseModel):
    broker_token_url: str = ""
    drive_api_base_url: str = "https://www.googleapis.com/drive/v3"
    gmail_api_base_url: str = "https://gmail.googleapis.com/gmail/v1"
    calendar_api_base_url: str = "https://www.googleapis.com/calendar/v3"
    static_access_token: str = ""
    request_timeout_seconds: float = 10.0


class SlackConnectorConfig(BaseModel):
    broker_token_url: str = ""
    api_base_url: str = "https://slack.com/api"
    static_access_token: str = ""
    request_timeout_seconds: float = 10.0


# GitHub login: alphanumeric characters with single hyphens between them, no
# leading/trailing/doubled hyphen (GitHub's own login rule). Deliberately
# stricter than routers/connectors.py's ``_GITHUB_OWNER_PATTERN`` -- that one
# only has to bound *request-time* free text, but an allowed_orgs entry is
# deploy config that gets spliced directly into GitHub search-query syntax as
# ``org:{org}``, so a value like "acme OR repo:other/private" would silently
# widen the search instead of narrowing it. No lookahead, so the same pattern
# also works as-is for the chart's values.schema.json (JSON Schema regexes
# run through Go's RE2, which does not support it); the length half of
# GitHub's rule is checked separately, mirroring the schema's ``maxLength``.
_GITHUB_LOGIN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_GITHUB_LOGIN_MAX_LENGTH = 39


class GitHubConnectorConfig(BaseModel):
    broker_token_url: str = ""
    api_base_url: str = "https://api.github.com"
    static_access_token: str = ""
    request_timeout_seconds: float = 10.0
    # Restrict the generic api_get read to these GitHub org logins. Empty = the
    # token's full visibility (personal repos + every approved org). Set this in
    # real deploys so the generic read cannot reach outside the allowlist. (The
    # curated search reads the SAME key once PR #76 lands its _build_query
    # enforcement; on this branch only api_get consults it.)
    allowed_orgs: list[str] = Field(default_factory=list)

    @field_validator("allowed_orgs")
    @classmethod
    def _check_allowed_orgs(cls, value: list[str]) -> list[str]:
        for org in value:
            if len(org) > _GITHUB_LOGIN_MAX_LENGTH or not _GITHUB_LOGIN_PATTERN.fullmatch(org):
                raise ValueError(
                    f"connectors.github.allowed_orgs entry {org!r} is not a valid GitHub org login: "
                    "alphanumeric characters and single hyphens only, no leading/trailing hyphen, "
                    f"max {_GITHUB_LOGIN_MAX_LENGTH} characters"
                )
        return value

    # Kill switch for the generic /api/get read, distinct from blanking
    # broker_token_url (which would also kill the curated reads). Defaults to
    # False (fail-closed): the long-tail tool ships opt-in, so an upgraded hub
    # gains full-token-visibility generic read only after an operator explicitly
    # flips this on (and sets allowed_orgs). The curated tools work regardless.
    api_get_enabled: bool = False
    # Bound concurrent generic reads per hub process so an injected agent can't
    # fan out unbounded outbound requests (each api_get opens its own client plus
    # a token-broker fetch). Curated tools are unaffected.
    api_get_max_concurrency: int = Field(default=8, ge=1)


class NotionConnectorConfig(BaseModel):
    # Option A: Keycloak generic-OAuth broker URL. Empty under Option B (the Hub
    # owns the Notion OAuth flow and its own encrypted token store).
    broker_token_url: str = ""
    api_base_url: str = "https://api.notion.com"
    # Pinned Notion API version, sent on every request. Do not float: newer
    # versions (2025-09-03+) split databases into data sources and move the query
    # endpoint, which is a migration, not a config change. See docs/notion-connector.md.
    notion_version: str = "2022-06-28"
    static_access_token: str = ""  # dev/CI only -- never in prod values
    request_timeout_seconds: float = 10.0


class ConnectorsConfig(BaseModel):
    google: GoogleConnectorConfig = Field(default_factory=GoogleConnectorConfig)
    slack: SlackConnectorConfig = Field(default_factory=SlackConnectorConfig)
    github: GitHubConnectorConfig = Field(default_factory=GitHubConnectorConfig)
    notion: NotionConnectorConfig = Field(default_factory=NotionConnectorConfig)


class KeycloakUserDirectoryConfig(BaseModel):
    issuer_url: str = ""
    token_url: str = ""
    admin_api_base_url: str = ""
    client_id: str = ""
    client_secret: str = ""


class UserDirectoryConfig(BaseModel):
    enabled: bool = False
    provider: str = "keycloak"
    keycloak: KeycloakUserDirectoryConfig = Field(default_factory=KeycloakUserDirectoryConfig)


class TasksConfig(BaseModel):
    backend: str = "memory"
    postgres_url: str = ""
    auto_migrate: bool = False


COGS_INDEX_INTERVAL_MIN_SECONDS = 10
COGS_INDEX_INTERVAL_MAX_SECONDS = 24 * 3600


class CogIndexConfig(BaseModel):
    """How often the Cog indexer sweeps the configured registry sources (#84).

    ``enabled`` is the only switch that changes what the deployment *does*:
    off, the read API stays up and serves whatever the index already holds,
    and no registry is contacted. The interval floor exists because a sweep
    lists every repository of every source; a one-second loop against a
    real registry is a self-inflicted outage, not a tuning choice.
    """

    # extra="forbid" mirrors values.schema.json's additionalProperties: false.
    # Without it a misspelled key (``interval_secs``) is dropped and the
    # default runs, which is a silent misconfiguration of exactly the kind
    # the chart refuses at render time.
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_seconds: int = Field(default=300, ge=COGS_INDEX_INTERVAL_MIN_SECONDS, le=COGS_INDEX_INTERVAL_MAX_SECONDS)
    run_on_startup: bool = True


class CogCatalogConfig(BaseModel):
    """Where the Cog catalog lives (#85).

    ``backend`` is a development override only, like ``frames.groups.backend``:
    ``"memory"`` keeps the catalog in the process (``make api``, level 1, so
    the read API answers without a database); empty -- the default -- rides
    the shared ``frames.postgres``, or refuses with 503 when there is none.
    Deliberately no chart value: an in-memory catalog is process-local, so on
    a deployment every replica would list something different and forget it
    all on restart.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["", "memory"] = ""


COGS_SOURCE_SECRET_ENV_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("username_env", "username", False),
    ("password_env", "password", True),
)
"""``credentials`` indirections: ``<name>_env`` names the environment variable holding ``<name>``.

The third element says whether the value is a secret (wrapped in ``SecretStr``
on insertion) or plain data like a robot account name.
"""

COGS_SOURCE_WEBHOOK_SECRET_ENV_FIELD = ("webhook_secret_env", "webhook_secret", True)


def _pull_secret_from_env(
    container: dict[str, Any],
    env_field: str,
    field: str,
    *,
    secret: bool,
    label: str,
    environ: Mapping[str, str],
) -> None:
    """Replace ``container[env_field]`` (an env var name) with ``container[field]`` (its value).

    Refuses an ambiguous entry that carries both, and a name whose variable is
    unset or blank — which is exactly what a missing Secret mount, a wrong
    ``key`` in ``existingSecret``, or an empty Secret value looks like from
    inside the pod. Failing here names the variable, so the fix is a values
    change and not a search through adapter 401s on the first sweep.

    A secret is inserted as a ``SecretStr``, never a plain string. Validation
    runs *after* this and pydantic quotes the offending input in its error
    text; a source whose username is missing would otherwise put the resolved
    password into a ValidationError that ``__main__`` lets reach the startup
    log. ``SecretStr`` reprs as ``**********`` wherever it is echoed, so no
    downstream error path — this model's, a nested one's, or the settings
    class's — can carry the value.

    The value is stored with surrounding whitespace removed, deliberately and
    on purpose rather than by accident: inline values already are (the
    ``_strip`` validators on ``CogRegistryCredentials`` and ``WebConfig``), a
    Secret created with ``--from-file`` carries the file's trailing newline,
    and forwarding that verbatim fails authentication with an error that
    names nothing useful. A ``SecretStr`` bypasses the model's own strip, so
    the policy is applied here, once, for the environment route — both
    routes yield the same bytes for string input (a ``SecretStr`` handed to
    ``Config.parse`` programmatically is taken as is; no operator route does
    that). A credential whose surrounding whitespace is significant is not
    supported; docs/cog-registry.md says so.
    """

    if env_field not in container:
        return
    name = container.pop(env_field)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label}.{env_field} must name an environment variable, got {name!r}")
    name = name.strip()
    if container.get(field):
        raise ValueError(
            f"{label} sets both {field} and {env_field}: the inline value and the environment "
            f"indirection disagree about where the secret lives, so neither is trusted. Keep one."
        )
    value = environ.get(name)
    if value is None or not value.strip():
        state = "not set" if value is None else "empty"
        raise ValueError(
            f"{label}.{env_field} names {name}, which is {state} in the environment. With the Helm "
            f"chart this means the Secret named in the source's existingSecret is missing or has no "
            f"value under the configured key; the API refuses to start rather than run unauthenticated."
        )
    value = value.strip()
    container[field] = SecretStr(value) if secret else value


def resolve_cogs_source_secrets(raw: Any, index: int, environ: Mapping[str, str]) -> Any:
    """Resolve one raw registry-source mapping's ``*_env`` indirections in place of the secrets.

    Why indirection at all: the source list travels to the process as one JSON
    environment variable (pydantic-settings parses complex fields that way),
    while passwords and webhook secrets must come from Kubernetes Secrets and
    never be rendered into values or that JSON. pydantic-settings does not
    layer ``..._SOURCES__0__CREDENTIALS__PASSWORD`` over a JSON list — measured
    on 2.14: the index form is silently ignored when the list is JSON, and
    rejected as ``list_type`` when it is not — so each source instead names the
    variable to read. The chart derives the names deterministically from the
    source id (``COLLAB_HUB_COGS_SOURCE_<ID>_PASSWORD`` and friends, see
    values.yaml) and mounts the Secret keys under them.

    Anything that is not a mapping is returned untouched: an already-built
    :class:`CogRegistrySourceConfig` has nothing to resolve, and a wrong type
    is pydantic's error to report.
    """

    if not isinstance(raw, dict):
        return raw
    raw = dict(raw)
    label = f"cogs.registry_sources[{index}] ({raw.get('id')!r})"
    credentials = raw.get("credentials")
    if isinstance(credentials, dict):
        credentials = dict(credentials)
        raw["credentials"] = credentials
        for env_field, field, secret in COGS_SOURCE_SECRET_ENV_FIELDS:
            _pull_secret_from_env(
                credentials, env_field, field, secret=secret, label=f"{label}.credentials", environ=environ
            )
    env_field, field, secret = COGS_SOURCE_WEBHOOK_SECRET_ENV_FIELD
    _pull_secret_from_env(raw, env_field, field, secret=secret, label=label, environ=environ)
    return raw


class CogsConfig(BaseModel):
    """The Cog registry block (#87): which registries are indexed, and how.

    Per-source rules (kind shape, URL grammar, credentials both-or-neither)
    belong to :class:`~.cogs.registry.CogRegistrySourceConfig` and run when
    each entry is built. What lives here is what no single source can see:
    duplicate ids across the list, and an indexer that is switched on with
    nothing to index. ``build_registry_sources`` re-checks duplicates when the
    adapters are instantiated; this copy exists so the failure happens at
    settings load with the rest of the configuration errors, not one step
    later with a different exception type.
    """

    # extra="forbid": the chart's values.schema.json refuses unknown ``cogs``
    # keys, and a bare-process deployment deserves the same — ``indx:`` would
    # otherwise leave the indexer silently off. scripts/testdata/chart/
    # cogs-negative-cases.yaml holds this rule once for both sides.
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")

    registry_sources: list[CogRegistrySourceConfig] = Field(default_factory=list)
    index: CogIndexConfig = Field(default_factory=CogIndexConfig)
    catalog: CogCatalogConfig = Field(default_factory=CogCatalogConfig)

    @field_validator("registry_sources", mode="before")
    @classmethod
    def _resolve_secret_env(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [resolve_cogs_source_secrets(raw, index, os.environ) for index, raw in enumerate(value)]

    @model_validator(mode="after")
    def _check_across_sources(self) -> Self:
        seen: dict[str, int] = {}
        for index, source in enumerate(self.registry_sources):
            if source.id in seen:
                raise ValueError(
                    f"cogs.registry_sources[{index}] reuses id {source.id!r}, already taken by "
                    f"registry_sources[{seen[source.id]}]: the id is stored with every indexed row, "
                    "so two sources sharing one would make their artifacts indistinguishable"
                )
            seen[source.id] = index
        if self.index.enabled and not self.registry_sources:
            raise ValueError(
                "cogs.index.enabled is true but cogs.registry_sources is empty: an indexer with nothing "
                "to index is a misconfiguration, not an idle worker. Add a source or set enabled=false "
                "(the read API stays up either way)."
            )
        return self


class RunsConfig(BaseModel):
    """Where the run API (``/v1/runs``) records and reads runs.

    The API writes intent to the Track and reads a run's status from it; a run
    controller, a separate process over the same Track, advances the runs
    (ADR-0002 D4). The API constructs no executor. ``backend`` and ``location``
    are what the controller beside this API is run with: the API only names
    them in a submission's response, so a client never assumes a run survives a
    restart or that its worker is isolated.
    """

    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")

    track_path: str | None = None
    """A SQLite Track file shared with the run controller, for a hub without Postgres (dev level 1).
    Unset, the Track is the hub's own database (``frames.postgres.url``), whose tables come from the
    hub's migrations; with neither, the run routes answer 503."""
    packages: list[str] | str = Field(default_factory=list)
    """The directories the controller finds Cog packages under: a step's ``cog`` must name one of them.
    A list, or from the environment one string of directories joined by ``os.pathsep``."""
    allow: list[str] | None = None
    """The package names that may be launched; every package under ``packages`` when unset."""
    backend: Literal["none", "dbos", "temporal"] = "none"
    location: Literal["local", "remote"] = "local"

    @field_validator("packages", mode="before")
    @classmethod
    def _split_packages(cls, value: Any) -> Any:
        """Directories as a list, or as one ``os.pathsep``-separated string from the environment."""
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return [part for part in value.split(os.pathsep) if part]
        return value


FEATURE_FLAGS: dict[str, str] = {}
"""Every feature flag this hub knows: name to one line on what it exposes.

Adding a flag starts here. A name that is not listed is refused wherever it
appears: set in the environment or the chart, it stops startup; passed to
:meth:`FeaturesConfig.enabled`, it raises. So a misspelled flag fails loudly
instead of silently reading as off.
"""

RETIRED_FEATURE_FLAGS: frozenset[str] = frozenset({
    "cog_runs",  # the run API (/v1/runs) is always served since the hub ships the execution package
})
"""Flags that have been removed but that deployments may still set.

When a flag's feature ships, move its name here from :data:`FEATURE_FLAGS`.
A retired name is ignored, and the app logs a warning naming it, instead of
stopping startup, so the chart
that drops a flag can roll out before every deployment's values drop it.
"""

FEATURE_FLAG_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
"""A flag name: lowercase snake_case, the form environment keys arrive in.

The same pattern as ``features.propertyNames`` in the chart's
values.schema.json, so a name the registry accepts is one the chart accepts.
"""


def check_feature_flag_registry(flags: Mapping[str, str], retired: frozenset[str]) -> None:
    """Refuse a registry whose names could never be set, or that is both live and retired."""
    bad = sorted(name for name in (*flags, *retired) if not FEATURE_FLAG_NAME.fullmatch(name))
    if bad:
        raise ValueError(f"feature flag names must be lowercase snake_case: {', '.join(bad)}")
    both = sorted(set(flags) & retired)
    if both:
        raise ValueError(f"feature flags both registered and retired: {', '.join(both)}")


check_feature_flag_registry(FEATURE_FLAGS, RETIRED_FEATURE_FLAGS)

_FLAG_VALUE = TypeAdapter(bool)


class FeaturesConfig(BaseModel):
    """Per-deployment switches for features that are on ``main`` but not yet exposed.

    The mechanism is always on. Each flag is declared once in
    :data:`FEATURE_FLAGS` and is off until a deployment sets it to true,
    either in the chart's ``features`` values or as the environment variable
    ``COLLAB_HUB_API__FEATURES__<NAME>``, following pydantic-settings' nested
    variables:
    https://docs.pydantic.dev/latest/concepts/pydantic_settings/#parsing-environment-variable-values

    Values are parsed with pydantic's boolean rules, so ``true``, ``1``,
    ``yes`` and ``on`` (any case) turn a flag on, and a value those rules
    cannot parse stops startup instead of reading as off:
    https://docs.pydantic.dev/latest/api/standard_library_types/#booleans

    Code reads flags only through :meth:`enabled`: routes through the
    ``get_features`` dependency, the admin UI through the ``features`` list in
    its session payload. Flags are for work in progress and are removed once
    the feature ships; a permanent operational switch belongs in its own
    section as a documented ``bool`` field.
    """

    model_config = ConfigDict(extra="allow")

    _retired: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def _parse_flags(cls, data: Any, handler: Any) -> Any:
        if not isinstance(data, Mapping):
            return handler(data)
        flags: dict[str, bool] = {}
        retired: list[str] = []
        for name in data:
            # pydantic-settings lowercases environment keys; the chart's
            # schema admits only lowercase names. Anything else is a typo.
            flag = str(name)
            if flag in RETIRED_FEATURE_FLAGS:
                retired.append(flag)
                continue
            if flag not in FEATURE_FLAGS:
                known = ", ".join(sorted(FEATURE_FLAGS)) or "none"
                raise ValueError(f"unknown feature flag {name!r}; registered flags: {known}")
            try:
                flags[flag] = _FLAG_VALUE.validate_python(data[name])
            except ValidationError:
                # Name only the flag: Config hides input values in errors.
                raise ValueError(f"feature flag {name!r} must be a boolean") from None
        features = handler(flags)
        features._retired = sorted(retired)
        return features

    def enabled(self, name: str) -> bool:
        """Whether the named flag is on for this deployment. An unregistered name raises ``KeyError``."""
        if name not in FEATURE_FLAGS:
            raise KeyError(f"feature flag {name!r} is not registered in FEATURE_FLAGS")
        return bool((self.__pydantic_extra__ or {}).get(name, False))

    @property
    def retired_names(self) -> list[str]:
        """The retired flags this deployment still sets, sorted. The app factory logs them."""
        return list(self._retired)

    @property
    def enabled_names(self) -> list[str]:
        """The flags that are on for this deployment, sorted."""
        return sorted(name for name, on in (self.__pydantic_extra__ or {}).items() if on)


class BaseConfig(BaseSettings):
    server: ServerConfig = Field(default_factory=ServerConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    frames: FramesConfig = Field(default_factory=FramesConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    user_directory: UserDirectoryConfig = Field(default_factory=UserDirectoryConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    cogs: CogsConfig = Field(default_factory=CogsConfig)
    runs: RunsConfig = Field(default_factory=RunsConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)


class Config(BaseConfig):
    # hide_input_in_errors at the outermost model: it is this class's config,
    # not a nested model's, that decides whether pydantic quotes the offending
    # input in a ValidationError, and the input here is the whole settings
    # tree — Postgres URLs with passwords, client secrets, resolved registry
    # credentials. __main__ lets that error reach the startup log. Field
    # names and constraint messages remain; only the echoed value goes.
    model_config = SettingsConfigDict(
        env_prefix="COLLAB_HUB_API__", env_nested_delimiter="__", hide_input_in_errors=True
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings

    @classmethod
    def parse(cls, obj: dict[str, Any] | None = None) -> Self:
        if obj is None:
            obj = {}
        return cls.model_validate(obj)


def build_frames_store(config: BaseConfig) -> FrameStore:
    if config.frames.storage_backend == "local":
        return LocalFsFrameStore(config.storage.frames_path)
    if config.frames.storage_backend == "s3":
        if not config.frames.s3.bucket:
            raise RuntimeError("COLLAB_HUB_API__FRAMES__S3__BUCKET is required for S3 frame storage")
        return S3FrameStore(
            bucket=config.frames.s3.bucket,
            prefix=config.frames.s3.prefix,
            endpoint_url=config.frames.s3.endpoint_url or None,
            region_name=config.frames.s3.region or None,
        )
    raise RuntimeError(f"Unsupported frame storage backend: {config.frames.storage_backend}")


def build_postgres_pools(config: BaseConfig) -> PostgresPools:
    """Create the app-wide pool registry all Postgres stores draw from.

    Stores sharing a database URL share one pool; the registry is opened at
    app startup and closed at shutdown by the app lifespan.
    """

    pool = config.frames.postgres.pool
    return PostgresPools(
        min_size=pool.min_size,
        max_size=pool.max_size,
        timeout_seconds=pool.timeout_seconds,
        max_waiting=pool.max_waiting,
    )


def build_active_frame_store(config: BaseConfig, pools: PostgresPools) -> ActiveFrameStore:
    backend = config.frames.active_state.backend
    if backend == "disabled":
        return DisabledActiveFrameStore()
    if backend == "memory":
        return InMemoryActiveFrameStore()
    if backend == "postgres":
        # Use active-state's own URL if given, else fall back to the shared
        # frames.postgres URL — configuring one shared URL lights up
        # active-state, history, and groups together.
        own = config.frames.active_state.postgres
        url = own.url or config.frames.postgres.url
        if not url:
            raise RuntimeError(
                "active_state.backend=postgres requires COLLAB_HUB_API__FRAMES__ACTIVE_STATE__POSTGRES__URL "
                "or the shared COLLAB_HUB_API__FRAMES__POSTGRES__URL"
            )
        auto_migrate = own.auto_migrate if own.url else config.frames.postgres.auto_migrate
        return PostgresActiveFrameStore(pools.database(url), auto_migrate=auto_migrate)
    raise RuntimeError(f"Unsupported active Frame state backend: {backend}")


def build_history_store(config: BaseConfig, pools: PostgresPools) -> FrameHistoryStore:
    # No per-feature "disabled": "memory" is a test/dev override; otherwise ride
    # the shared frames.postgres, or an unavailable store (→ 503) when no DB.
    if config.frames.history.backend == "memory":
        return InMemoryFrameHistoryStore()
    url = config.frames.postgres.url
    if url:
        return PostgresFrameHistoryStore(pools.database(url), auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableFrameHistoryStore()


def build_usage_store(config: BaseConfig, pools: PostgresPools) -> UsageStore:
    # Mirrors build_history_store: "memory" override, else shared frames.postgres,
    # else an unavailable store (→ 503 on reads/events). No per-feature "disabled".
    if config.frames.usage.backend == "memory":
        return InMemoryUsageStore()
    url = config.frames.postgres.url
    if url:
        return PostgresUsageStore(pools.database(url), auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableUsageStore()


def build_group_store(config: BaseConfig, pools: PostgresPools) -> FrameGroupStore:
    # Mirrors build_history_store: "memory" override, else shared frames.postgres,
    # else an unavailable store (→ 503). No per-feature "disabled".
    if config.frames.groups.backend == "memory":
        return InMemoryFrameGroupStore()
    url = config.frames.postgres.url
    if url:
        return PostgresFrameGroupStore(pools.database(url), auto_migrate=config.frames.postgres.auto_migrate)
    return UnavailableFrameGroupStore()


def build_org_store(config: BaseConfig, pools: PostgresPools) -> OrgStore:
    # Mirrors build_history_store: "memory" test/dev override, else the shared
    # frames.postgres, else a store that fails closed. Unlike the other
    # features there is no 503-and-carry-on story here — membership is an
    # authorization input, so make_app refuses to *start* a membership-
    # resolving deployment whose store is the unavailable one.
    if config.frames.orgs.backend == "memory":
        return InMemoryOrgStore()
    url = config.frames.postgres.url
    if url:
        return PostgresOrgStore(pools.database(url))
    return UnavailableOrgStore()


def build_cog_catalog_store(config: BaseConfig, pools: PostgresPools) -> CogCatalogStore:
    """The Cog catalog (issue #84): the shared frames.postgres, else a store that refuses (503).

    Always built, whether or not indexing is enabled -- the catalog read API
    stays up on a deployment that only reads a catalog another replica or an
    out-of-band job fills. The table is created by the ``collab_`` migration
    runner (version 11), so there is no ``auto_migrate`` argument here.
    ``cogs.catalog.backend=memory`` is the development override (#85) that
    lets level 1 of ``dev/`` serve the read API with no database.
    """

    if config.cogs.catalog.backend == "memory":
        return InMemoryCogCatalogStore()
    url = config.frames.postgres.url
    if url:
        return PostgresCogCatalogStore(pools.database(url))
    return UnavailableCogCatalogStore()


class CogIndexing:
    """A built indexer plus the loop parameters the app lifespan runs it with."""

    def __init__(self, indexer: CogIndexer, *, interval_seconds: float, run_on_startup: bool) -> None:
        self.indexer = indexer
        self.interval_seconds = interval_seconds
        self.run_on_startup = run_on_startup


def build_cog_indexing(config: BaseConfig, store: CogCatalogStore) -> CogIndexing | None:
    """The reconciliation indexer (issue #84) when ``cogs.index.enabled``, else ``None``.

    Reads the ``cogs`` block (issue #87): ``cogs.registry_sources`` and
    ``cogs.index`` (``enabled``, ``interval_seconds``, ``run_on_startup``).
    ``CogsConfig`` has already refused an enabled index with no sources and
    resolved every credential indirection, so what arrives here is complete.

    Sources are constructed here, once, so a duplicate id or an unsupported
    kind fails the rollout rather than the first sweep, and are not
    constructed at all when indexing is disabled. Indexing into the
    unavailable store is refused: the store is what a sweep writes, and a
    deployment that enables sweeping without a database would otherwise fail
    every interval for as long as the pod lived.
    """

    cogs = config.cogs
    index = cogs.index
    if not index.enabled:
        return None
    if isinstance(store, UnavailableCogCatalogStore):
        raise RuntimeError(
            "cogs.index.enabled requires the Cog catalog store: set the shared "
            "COLLAB_HUB_API__FRAMES__POSTGRES__URL (frames.postgres.url), or disable indexing."
        )
    pool = config.frames.postgres.pool
    if pool.max_size < 2:
        # A sweep occupies one pooled connection for its whole duration: the
        # session-level advisory lock is held on it, and the sweep's reads and
        # writes ride it too (issue #128). With max_size=1 that is the pool's
        # only connection gone for minutes at a time -- every API read and
        # every webhook write would wait on the sweep and time out, silently,
        # at runtime. Refuse the rollout instead.
        raise RuntimeError(
            "cogs.index.enabled requires frames.postgres.pool.max_size >= 2 "
            f"(configured: {pool.max_size}): the indexer's sweep occupies one pooled "
            "connection for the whole sweep while everything else needs another."
        )
    sources = build_registry_sources(list(cogs.registry_sources))
    return CogIndexing(
        CogIndexer(store, sources),
        interval_seconds=float(index.interval_seconds),
        run_on_startup=index.run_on_startup,
    )


def build_model_catalog(config: BaseConfig):
    """The serving layer's catalogue reader, configured or not."""

    from .frames.model_catalog import ModelCatalogClient

    return ModelCatalogClient(base_url=config.frames.model_access.catalog_base_url)


def build_model_access(config: BaseConfig, pools: PostgresPools):
    """The membership writer and its audited service, or ``None``.

    ``None`` when the credential or the database is missing, and the panel then
    reports model access as unavailable. A half-built service that could change
    Keycloak but not record it would be worse than none at all.
    """

    from .frames.group_membership import GroupMembershipClient
    from .frames.model_access import ModelAccessService

    access = config.frames.model_access
    keycloak = access.keycloak
    url = config.frames.postgres.url
    if not (keycloak.token_url and keycloak.admin_api_base_url and keycloak.client_id and access.group_ids):
        return None
    if not url:
        return None
    membership = GroupMembershipClient(
        token_url=keycloak.token_url,
        admin_api_base_url=keycloak.admin_api_base_url,
        client_id=keycloak.client_id,
        client_secret=keycloak.client_secret,
        group_ids=access.group_ids,
    )
    return ModelAccessService(db=pools.database(url), membership=membership)


def build_connector_store(config: BaseConfig, pools: PostgresPools):
    """The connector switches, or ``None`` without a database.

    ``None`` means nothing is switched off and nothing can be, which is how
    every deployment behaved before the switch existed.
    """

    from .frames.connector_store import PostgresConnectorStore

    url = config.frames.postgres.url
    return PostgresConnectorStore(pools.database(url)) if url else None


def build_platform_role_admin(config: BaseConfig, pools: PostgresPools):
    """Operator-role changes made from the panel, or ``None`` without a database.

    ``None`` rather than a refusing stand-in, because the panel asks whether
    roles are manageable before it renders the controls: a deployment with no
    database shows no buttons rather than buttons that always fail.
    """

    from .frames.platform_role_admin import PostgresPlatformRoleAdmin

    url = config.frames.postgres.url
    return PostgresPlatformRoleAdmin(pools.database(url)) if url else None


def build_audit_log(config: BaseConfig, pools: PostgresPools):
    """The audit reader, or the one that refuses.

    No ``"memory"`` override, for the reason ``build_invitation_service`` has
    none: there is no in-memory audit log, because ``audited()`` writes to
    Postgres inside the transaction it is auditing. A stand-in would answer an
    empty page to somebody investigating an incident, which is the worst
    possible lie for this particular store to tell.
    """

    from .frames.audit_log import PostgresAuditLog, UnavailableAuditLog

    url = config.frames.postgres.url
    return PostgresAuditLog(pools.database(url)) if url else UnavailableAuditLog()


def build_platform_role_sync(config: BaseConfig, pools: PostgresPools, org_store: OrgStore):
    """The sign-in reconcile, paired to whatever backend holds the roles.

    Returns the refusing-to-act implementation unless a deployment has named an
    admin group, so this is inert on every deployment that has not opted in.
    """

    from .frames.platform_role_sync import (
        DisabledPlatformRoleSync,
        InMemoryPlatformRoleSync,
        PostgresPlatformRoleSync,
    )

    admin_group = config.web.admin_group
    if not admin_group:
        return DisabledPlatformRoleSync()
    if isinstance(org_store, InMemoryOrgStore):
        return InMemoryPlatformRoleSync(org_store, admin_group=admin_group)
    url = config.frames.postgres.url
    if url:
        return PostgresPlatformRoleSync(pools.database(url), admin_group=admin_group)
    # No store to reconcile against. Refusing to sync is the correct state for
    # such a deployment, and make_app already refuses to start a
    # membership-resolving one whose store is the unavailable store.
    return DisabledPlatformRoleSync()


def build_invitation_service(config: BaseConfig, pools: PostgresPools) -> InvitationService:
    """Build the invitation lifecycle service, or the one that refuses (503).

    Deliberately **not** mirroring the other builders' ``"memory"`` override:
    there is no in-memory invitation backend and there will not be one. What
    this service promises — a token that cannot be redeemed twice, exactly one
    organization under concurrent acceptance, a mutation that commits with its
    audit row or not at all — is the ``FOR UPDATE`` lock, the membership
    primary key, and the transaction. An in-memory stand-in could reproduce
    the method signatures and none of the guarantees, and a dev deployment
    that appeared to work would be the worst possible place to discover that.
    """

    url = config.frames.postgres.url
    if url:
        return PostgresInvitationService(
            pools.database(url),
            require_verified_email=config.frames.invitations.require_verified_email,
        )
    return UnavailableInvitationService()


def build_invitation_email_delivery(
    config: BaseConfig,
) -> ConfiguredInvitationEmailDelivery | DisabledInvitationEmailDelivery:
    """Build the provider seam without making a network or credential call."""

    email = config.frames.email
    if email.provider == "disabled":
        return DisabledInvitationEmailDelivery()
    if email.provider == "ses":
        provider = SesInvitationEmailProvider(
            sender_address=email.ses.sender_address,
            region=email.ses.region,
            configuration_set=email.ses.configuration_set,
            request_timeout_seconds=email.ses.request_timeout_seconds,
        )
        return ConfiguredInvitationEmailDelivery(
            provider,
            accept_url=email.accept_url,
            app_instructions=email.app_instructions,
            # The same value the acceptance check reads, so the copy and the
            # rule it describes cannot disagree.
            require_verified_email=config.frames.invitations.require_verified_email,
        )
    raise RuntimeError(f"Unsupported invitation email provider: {email.provider}")


def migrate_collab_schema(config: BaseConfig, pools: PostgresPools) -> bool:
    """Bring the ``collab_`` tenancy tables up to date, if configured to.

    Unlike the frames features there is no per-store ``_ensure_schema`` to hang
    this on: the ``collab_`` tables are migrated by one versioned, advisory-lock
    guarded runner (see :mod:`.frames.collab_schema`). The trigger is the same
    pair of settings the stores use — the shared ``frames.postgres.url`` plus
    ``frames.postgres.auto_migrate`` — so a deployment that already opts into
    auto-migration gets these tables with no new switch, and one that migrates
    out of band still does.

    Returns whether the migration ran, which is what the caller can meaningfully
    assert; failures propagate exactly as the stores' ``auto_migrate`` does.
    """

    url = config.frames.postgres.url
    if not url or not config.frames.postgres.auto_migrate:
        return False
    run_collab_schema_migrations(pools.database(url))
    return True


def preflight_collab_schema(config: BaseConfig, pools: PostgresPools) -> int | None:
    """Refuse to start against a ``collab_`` schema older than this build needs.

    Issue #96, landed with its first consumer: until something reads these
    tables a version check has nothing to protect, and once something does, the
    alternative to checking is a ``relation does not exist`` traceback from
    whichever authenticated request touched them first. Called by ``make_app``
    only when membership resolution is on, and only when a shared Postgres URL
    is configured — with neither, no ``collab_`` table is ever read.

    Returns the applied version, ``None`` when the database was unreachable
    (not fatal, see :func:`~.frames.collab_schema.check_collab_schema_version`),
    and raises when the schema is behind.
    """

    url = config.frames.postgres.url
    if not url:
        return None
    return check_collab_schema_version(
        pools.database(url),
        auto_migrate=config.frames.postgres.auto_migrate,
    )


def build_service_access_granter(config: BaseConfig) -> ServiceAccessGranter:
    """The membership-writing seam, or the one that refuses (#180).

    Three states, and only the third does anything:

    * **nothing configured to grant** -- the default. Returns the disabled
      granter. Nothing should hand out service access because a deployment
      forgot to say otherwise; that is the shape of the behaviour this
      replaced (an internal issue);
    * **groups configured, credential incomplete** -- refuses to start. A
      deployment that says "grant `/llm` on acceptance" and cannot is
      misconfigured, and the invitees it would accept meanwhile are exactly
      who pays for discovering that later;
    * **both** -- builds the granter and **resolves every configured group path
      to an id immediately**, so a typo fails in front of whoever deployed it
      rather than once per invitee, silently, at acceptance time.
    """

    groups = tuple(config.frames.service_access.grant_on_acceptance)
    if not groups:
        return DisabledServiceAccessGranter()

    keycloak = config.frames.service_access.keycloak
    token_url = keycloak.token_url or _keycloak_token_url(keycloak.issuer_url)
    missing = [
        name
        for name, value in {
            "issuer_url": keycloak.issuer_url,
            "token_url": token_url,
            "admin_api_base_url": keycloak.admin_api_base_url,
            "client_id": keycloak.client_id,
            "client_secret": keycloak.client_secret,
        }.items()
        if not value
    ]
    if missing:
        fields = ", ".join(f"frames.service_access.keycloak.{name}" for name in missing)
        raise RuntimeError(
            f"frames.service_access.grant_on_acceptance names {list(groups)} but the credential "
            f"to grant them is incomplete: {fields}. Either configure the credential or clear the "
            f"group list -- a deployment that promises service access and cannot grant it strands "
            f"every invitee it accepts."
        )

    unknown = sorted(set(keycloak.group_ids) - set(groups))
    if unknown:
        raise RuntimeError(
            f"frames.service_access.keycloak.group_ids names {unknown}, which "
            f"frames.service_access.grant_on_acceptance does not grant. A mapping for a path "
            f"nothing grants is a typo in one of the two lists, and the harmless-looking "
            f"reading -- an unused entry -- is the one that leaves the real path unmapped."
        )
    malformed = sorted(path for path, group_id in keycloak.group_ids.items() if not group_id.strip() or "/" in group_id)
    if malformed:
        raise RuntimeError(
            f"frames.service_access.keycloak.group_ids has no usable id for {malformed}. "
            f"The value must be Keycloak's group id, not the path again -- checked only for "
            f"emptiness and for looking like a path, because the id's format is Keycloak's to "
            f"choose and a stricter rule here would be this file inventing a contract."
        )

    granter = KeycloakServiceAccessGranter(
        token_url=token_url,
        admin_api_base_url=keycloak.admin_api_base_url,
        client_id=keycloak.client_id,
        client_secret=keycloak.client_secret,
        group_paths=groups,
        group_ids=keycloak.group_ids,
    )
    # Resolved now rather than on first use: this call is the startup gate. A
    # configured group that does not exist raises, naming the path.
    #
    # When every path has a configured id this makes **no request** -- which is
    # the point of allowing them. A write-only credential cannot perform the
    # lookup at all (403 on every read), and even where it could, making startup
    # depend on the identity provider means a Keycloak blip during a rollout
    # leaves no pods rather than degraded ones.
    #
    # Closed on that path, because the granter already owns an `httpx.Client` by
    # the time it is asked to resolve. Refusing to start is the intended
    # outcome; leaking a connection pool on the way out is not, and a
    # misconfigured deployment restarting in a loop would leak one per attempt
    # (raised in review of #183).
    try:
        granter.resolve_groups()
    except Exception:
        granter.close()
        raise
    return granter


def build_user_directory_client(config: BaseConfig) -> UserDirectoryClient:
    if not config.user_directory.enabled:
        return DisabledUserDirectoryClient()
    if config.user_directory.provider != "keycloak":
        raise RuntimeError(f"Unsupported user directory provider: {config.user_directory.provider}")

    keycloak = config.user_directory.keycloak
    token_url = keycloak.token_url or _keycloak_token_url(keycloak.issuer_url)
    missing = [
        name
        for name, value in {
            "issuer_url": keycloak.issuer_url,
            "token_url": token_url,
            "admin_api_base_url": keycloak.admin_api_base_url,
            "client_id": keycloak.client_id,
            "client_secret": keycloak.client_secret,
        }.items()
        if not value
    ]
    if missing:
        fields = ", ".join(f"user_directory.keycloak.{name}" for name in missing)
        raise RuntimeError(f"Missing required Keycloak user directory config: {fields}")

    return KeycloakUserDirectoryClient(
        token_url=token_url,
        admin_api_base_url=keycloak.admin_api_base_url,
        client_id=keycloak.client_id,
        client_secret=keycloak.client_secret,
    )


def build_task_store(config: BaseConfig, pools: PostgresPools) -> InMemoryTaskStore | PostgresTaskStore:
    if config.tasks.backend == "memory":
        return InMemoryTaskStore()
    if config.tasks.backend != "postgres":
        raise RuntimeError(f"Unsupported task storage backend: {config.tasks.backend}")
    url = config.tasks.postgres_url or config.frames.postgres.url
    if not url:
        raise RuntimeError(
            "tasks.backend=postgres requires COLLAB_HUB_API__TASKS__POSTGRES_URL "
            "or the shared COLLAB_HUB_API__FRAMES__POSTGRES__URL. "
            "Set tasks.backend=memory for explicit non-durable local development."
        )
    return PostgresTaskStore(
        pools.database(url), auto_migrate=config.tasks.auto_migrate or config.frames.postgres.auto_migrate
    )


def _keycloak_token_url(issuer_url: str) -> str:
    issuer_url = issuer_url.rstrip("/")
    if not issuer_url:
        return ""
    return f"{issuer_url}/protocol/openid-connect/token"
