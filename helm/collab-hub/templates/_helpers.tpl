{{- define "collab-hub.name" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- $name | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "collab-hub.fullname" -}}
{{- $fullname := "" -}}
{{- if .Values.fullnameOverride -}}
{{- $fullname = .Values.fullnameOverride -}}
{{- else -}}
{{- $name := include "collab-hub.name" . -}}
{{- if contains $name .Release.Name -}}
{{- $fullname = .Release.Name -}}
{{- else -}}
{{- $fullname = printf "%s-%s" .Release.Name $name -}}
{{- end -}}
{{- end -}}
{{- $fullname | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "collab-hub.component-name" -}}
{{- $componentName := printf "%s-%s" (include "collab-hub.fullname" .top) .component -}}
{{- $componentName | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "collab-hub.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .top.Chart.Name .top.Chart.Version | replace "+" "-" | quote }}
app.kubernetes.io/name: {{ include "collab-hub.name" .top }}
app.kubernetes.io/instance: {{ .top.Release.Name }}
app.kubernetes.io/managed-by: {{ .top.Release.Service }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "collab-hub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "collab-hub.name" .top }}
app.kubernetes.io/instance: {{ .top.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "collab-hub.frames-storage-claim" -}}
{{- if .Values.frames.storage.persistence.existingClaim -}}
{{- .Values.frames.storage.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-frames" (include "collab-hub.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "collab-hub.api-service-account-name" -}}
{{- if .Values.api.serviceAccount.create -}}
{{- default (include "collab-hub.component-name" (dict "top" . "component" "api")) .Values.api.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.api.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Whether the app enforces the protection map and the restricted CORS default.
Returns the string "true" or "false".

`security.enforce` null follows `api.ingress.enabled`: standalone exposure is
new and is hardened from the start, while an install behind the Nebari gateway
keeps the behavior it has today until its operator opts in explicitly.
*/}}
{{- define "collab-hub.security-enforced" -}}
{{- if kindIs "invalid" .Values.security.enforce -}}
{{- ternary "true" "false" .Values.api.ingress.enabled -}}
{{- else -}}
{{- ternary "true" "false" .Values.security.enforce -}}
{{- end -}}
{{- end -}}

{{/*
Whether uvicorn trusts X-Forwarded-*. Returns "true" or "false".
Null follows the exposure mode, so gateway installs keep today's behavior
(the application default, off) and only ingress exposure turns it on.
*/}}
{{- define "collab-hub.proxy-headers-enabled" -}}
{{- if kindIs "invalid" .Values.server.proxyHeaders -}}
{{- ternary "true" "false" .Values.api.ingress.enabled -}}
{{- else -}}
{{- ternary "true" "false" .Values.server.proxyHeaders -}}
{{- end -}}
{{- end -}}

{{/*
The protection map the app is given: the configured entries plus the
/metrics rule, appended last so it wins by the documented last-equally-specific
precedence and an operator never has to restate the map to open one path.
*/}}
{{- define "collab-hub.security-paths" -}}
{{- $paths := .Values.security.paths | default list -}}
{{- $paths = append $paths (dict "path" "/metrics" "match" "exact" "access" .Values.security.metricsAccess) -}}
{{- toJson $paths -}}
{{- end -}}

{{- define "collab-hub.api-tls-secret-name" -}}
{{- if .Values.api.ingress.tls.secretName -}}
{{- .Values.api.ingress.tls.secretName -}}
{{- else -}}
{{- printf "%s-tls" (include "collab-hub.component-name" (dict "top" . "component" "api")) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
NebariApp helper template.
Expects a dict with keys: top, component, service, nebariapp
*/}}
{{- define "collab-hub.nebariapp" -}}
{{- $top := .top -}}
{{- $component := .component -}}
{{- $service := .service -}}
{{- $nebariapp := .nebariapp -}}
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
metadata:
  name: {{ include "collab-hub.component-name" (dict "top" $top "component" $component) }}
  namespace: {{ $top.Release.Namespace }}
  labels:
    {{- include "collab-hub.labels" (dict "top" $top "component" $component) | nindent 4 }}
spec:
  hostname: {{ required (printf "%s.nebariapp.hostname is required" $component) $nebariapp.hostname }}
  service:
    name: {{ $service.name }}
    port: {{ $service.port }}
    {{- with $service.namespace }}
    namespace: {{ . }}
    {{- end }}
  {{- with $nebariapp.serviceAccountName }}
  serviceAccountName: {{ . }}
  {{- end }}
  {{- with $nebariapp.routing }}
  routing:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with $nebariapp.auth }}
  auth:
    enabled: {{ .enabled | default false }}
    provider: {{ .provider | default "keycloak" }}
    provisionClient: {{ .provisionClient | default true }}
    {{- if hasKey . "enforceAtGateway" }}
    enforceAtGateway: {{ .enforceAtGateway }}
    {{- end }}
    {{- with .redirectURI }}
    redirectURI: {{ . }}
    {{- end }}
    {{- with .clientSecretRef }}
    clientSecretRef: {{ . }}
    {{- end }}
    {{- with .scopes }}
    scopes:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .groups }}
    groups:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .forwardAccessToken }}
    forwardAccessToken: {{ . }}
    {{- end }}
    {{- with .denyRedirect }}
    denyRedirect:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .issuerURL }}
    issuerURL: {{ . }}
    {{- end }}
    {{- with .spaClient }}
    spaClient:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .deviceFlowClient }}
    deviceFlowClient:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .keycloakConfig }}
    keycloakConfig:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .tokenExchange }}
    tokenExchange:
      {{- toYaml . | nindent 6 }}
    {{- end }}
  {{- end }}
  {{- with $nebariapp.gateway }}
  gateway: {{ . }}
  {{- end }}
  {{- with $nebariapp.landingPage }}
  landingPage:
    enabled: {{ .enabled | default false }}
    {{- with .displayName }}
    displayName: {{ . | quote }}
    {{- end }}
    {{- with .description }}
    description: {{ . | quote }}
    {{- end }}
    {{- with .icon }}
    icon: {{ . | quote }}
    {{- end }}
    {{- with .category }}
    category: {{ . | quote }}
    {{- end }}
    {{- if .priority }}
    priority: {{ .priority }}
    {{- end }}
    {{- with .externalUrl }}
    externalUrl: {{ . | quote }}
    {{- end }}
    {{- with .healthCheck }}
    healthCheck:
      enabled: {{ .enabled | default false }}
      {{- with .path }}
      path: {{ . | quote }}
      {{- end }}
      {{- if .intervalSeconds }}
      intervalSeconds: {{ .intervalSeconds }}
      {{- end }}
      {{- if .timeoutSeconds }}
      timeoutSeconds: {{ .timeoutSeconds }}
      {{- end }}
      {{- if .port }}
      port: {{ .port }}
      {{- end }}
    {{- end }}
  {{- end }}
{{- end -}}

{{/*
Cog registry (issue #87).

Where the CA bundle ConfigMap is mounted. Fixed rather than configurable: the
path is an implementation detail shared by the volumeMount and every source's
ca_bundle_path, and nothing outside the pod needs to know it.
*/}}
{{- define "collab-hub.cogs-ca-bundle-mount-path" -}}
/etc/collab-hub/cogs-ca
{{- end -}}

{{- define "collab-hub.cogs-ca-bundle-path" -}}
{{- if .Values.cogs.caBundle.configMap -}}
{{- printf "%s/%s" (include "collab-hub.cogs-ca-bundle-mount-path" .) .Values.cogs.caBundle.key -}}
{{- end -}}
{{- end -}}

{{/*
The environment variable a source's Secret key is mounted under. Takes
(dict "id" <source id> "suffix" <USERNAME|PASSWORD|WEBHOOK_SECRET>). The id is
upper-cased and every character outside [A-Z0-9] becomes "_", so the name is
a valid POSIX identifier; cogs-validations.yaml fails the render if two ids
collapse to the same name. config.py reads exactly the names rendered here
(the JSON carries them as credentials.username_env / password_env and
webhook_secret_env), so this template is the single source of the convention.
*/}}
{{- define "collab-hub.cogs-source-env-name" -}}
{{- printf "COLLAB_HUB_COGS_SOURCE_%s_%s" (regexReplaceAll "[^A-Z0-9]" (upper .id) "_") .suffix -}}
{{- end -}}

{{/*
The value of COLLAB_HUB_API__COGS__REGISTRY_SOURCES: the source list as JSON
in the API's snake_case shape, with every empty optional field omitted and
NO secret values — only the env var names the API resolves them from.
pydantic-settings parses list-valued settings from the environment as JSON;
toJson sorts keys, so the rendering is deterministic.
*/}}
{{- define "collab-hub.cogs-registry-sources" -}}
{{- $top := . -}}
{{- $caBundlePath := include "collab-hub.cogs-ca-bundle-path" . -}}
{{- $out := list -}}
{{- range .Values.cogs.registry.sources -}}
{{- $source := dict "id" .id "kind" .kind "url" .url -}}
{{- with .apiUrl }}{{ $_ := set $source "api_url" . }}{{ end -}}
{{- with .tokenUrl }}{{ $_ := set $source "token_url" . }}{{ end -}}
{{- with .projects }}{{ $_ := set $source "projects" . }}{{ end -}}
{{- with .repositories }}{{ $_ := set $source "repositories" . }}{{ end -}}
{{- with .indexUrl }}{{ $_ := set $source "index_url" . }}{{ end -}}
{{- with (default $caBundlePath .caBundlePath) }}{{ $_ := set $source "ca_bundle_path" . }}{{ end -}}
{{- if hasKey . "requestTimeoutSeconds" }}{{ $_ := set $source "request_timeout_seconds" .requestTimeoutSeconds }}{{ end -}}
{{- $credentials := .credentials | default dict -}}
{{- if $credentials.existingSecret -}}
{{- $_ := set $source "credentials" (dict
      "username_env" (include "collab-hub.cogs-source-env-name" (dict "id" .id "suffix" "USERNAME"))
      "password_env" (include "collab-hub.cogs-source-env-name" (dict "id" .id "suffix" "PASSWORD"))) -}}
{{- end -}}
{{- $webhook := .webhook | default dict -}}
{{- if $webhook.existingSecret -}}
{{- $_ := set $source "webhook_secret_env" (include "collab-hub.cogs-source-env-name" (dict "id" .id "suffix" "WEBHOOK_SECRET")) -}}
{{- end -}}
{{- $out = append $out $source -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/*
The API container's environment, shared by the API Deployment and the Cog
indexer Deployment (issue #148): one process image, one settings surface,
rendered once. Expects a dict with keys: top (the chart root) and indexer
(bool: whether this container is the sweeping process). The only difference
between the two renders is the cogs index switch and its tuning -- see the
"Cog registry" comment below. Emits a list of env entries at column 0;
include with nindent.
*/}}
{{- define "collab-hub.api-container-env" -}}
{{- $top := .top -}}
{{- $indexer := .indexer -}}
{{- with $top -}}
{{- $enforced := eq (include "collab-hub.security-enforced" .) "true" }}
{{- $proxyHeaders := eq (include "collab-hub.proxy-headers-enabled" .) "true" -}}
- name: COLLAB_HUB_API__SERVER__PROXY_HEADERS
  value: {{ $proxyHeaders | quote }}
{{- if $proxyHeaders }}
- name: COLLAB_HUB_API__SERVER__FORWARDED_ALLOW_IPS
  value: {{ toJson (ternary (list "*") .Values.server.forwardedAllowIps (and .Values.server.trustAnyProxy (not .Values.server.forwardedAllowIps))) | quote }}
{{- end }}
- name: COLLAB_HUB_API__SERVER__ROOT_PATH
  value: {{ .Values.server.rootPath | quote }}
# Lists and the protection map travel as JSON: pydantic-settings
# parses complex fields from the environment that way, which is
# what keeps the map data in values instead of code.
{{- if not (kindIs "invalid" .Values.security.cors.allowedOrigins) }}
- name: COLLAB_HUB_API__SECURITY__CORS__ALLOWED_ORIGINS
  value: {{ toJson .Values.security.cors.allowedOrigins | quote }}
{{- else if $enforced }}
# Hardened exposure: no cross-origin grant. Left unset otherwise,
# so an existing install keeps the application's ["*"] default
# rather than losing browser callers on upgrade.
- name: COLLAB_HUB_API__SECURITY__CORS__ALLOWED_ORIGINS
  value: "[]"
{{- end }}
- name: COLLAB_HUB_API__SECURITY__CORS__ALLOWED_HEADERS
  value: {{ toJson .Values.security.cors.allowedHeaders | quote }}
- name: COLLAB_HUB_API__SECURITY__CORS__ALLOW_CREDENTIALS
  value: {{ .Values.security.cors.allowCredentials | quote }}
{{- if $enforced }}
- name: COLLAB_HUB_API__SECURITY__PATHS
  value: {{ include "collab-hub.security-paths" . | quote }}
- name: COLLAB_HUB_API__SECURITY__DEFAULT_ACCESS
  value: {{ .Values.security.defaultAccess | quote }}
{{- else }}
# Gateway exposure keeps the behavior it has today: the map is not
# enforced, so route dependencies remain the only auth and an
# in-cluster /metrics scrape keeps working. Set security.enforce=true
# to opt in.
- name: COLLAB_HUB_API__SECURITY__PATHS
  value: "[]"
- name: COLLAB_HUB_API__SECURITY__DEFAULT_ACCESS
  value: "public"
{{- end }}
- name: COLLAB_HUB_API__OBSERVABILITY__LOGGING__LEVEL
  value: {{ .Values.observability.logging.level | quote }}
- name: COLLAB_HUB_API__OBSERVABILITY__LOGGING__AS_JSON
  value: {{ .Values.observability.logging.asJson | quote }}
- name: COLLAB_HUB_API__STORAGE__FRAMES_PATH
  value: {{ .Values.frames.storage.mountPath | quote }}
- name: COLLAB_HUB_API__FRAMES__STORAGE_BACKEND
  value: {{ .Values.frames.storage.backend | quote }}
- name: COLLAB_HUB_API__FRAMES__S3__BUCKET
  value: {{ .Values.frames.s3.bucket | quote }}
- name: COLLAB_HUB_API__FRAMES__S3__PREFIX
  value: {{ .Values.frames.s3.prefix | quote }}
- name: COLLAB_HUB_API__FRAMES__S3__ENDPOINT_URL
  value: {{ .Values.frames.s3.endpointUrl | quote }}
- name: COLLAB_HUB_API__FRAMES__S3__REGION
  value: {{ .Values.frames.s3.region | quote }}
{{- with .Values.frames.s3.existingSecret }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: {{ $top.Values.frames.s3.accessKeyIdKey | quote }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: {{ $top.Values.frames.s3.secretAccessKeyKey | quote }}
- name: AWS_SESSION_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: {{ $top.Values.frames.s3.sessionTokenKey | quote }}
      optional: true
{{- end }}
{{- with .Values.frames.s3.region }}
- name: AWS_REGION
  value: {{ . | quote }}
{{- end }}
- name: COLLAB_HUB_API__FRAMES__ACTIVE_STATE__BACKEND
  value: {{ .Values.frames.activeState.backend | quote }}
- name: COLLAB_HUB_API__FRAMES__ACTIVE_STATE__POSTGRES__URL
  {{- if .Values.frames.activeState.postgres.existingSecret }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.activeState.postgres.existingSecret | quote }}
      key: {{ .Values.frames.activeState.postgres.databaseUrlKey | quote }}
  {{- else }}
  value: {{ .Values.frames.activeState.postgres.url | quote }}
  {{- end }}
- name: COLLAB_HUB_API__FRAMES__ACTIVE_STATE__POSTGRES__AUTO_MIGRATE
  value: {{ .Values.frames.activeState.postgres.autoMigrate | quote }}
# Single shared Postgres URL: lights up history + groups and is the
# active-state fallback. Unset ⇒ history/group endpoints return 503.
- name: COLLAB_HUB_API__FRAMES__POSTGRES__URL
  {{- if .Values.frames.postgres.existingSecret }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.postgres.existingSecret | quote }}
      key: {{ .Values.frames.postgres.databaseUrlKey | quote }}
  {{- else }}
  value: {{ .Values.frames.postgres.url | quote }}
  {{- end }}
- name: COLLAB_HUB_API__FRAMES__POSTGRES__AUTO_MIGRATE
  value: {{ .Values.frames.postgres.autoMigrate | quote }}
- name: COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MIN_SIZE
  value: {{ .Values.frames.postgres.pool.minSize | quote }}
- name: COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MAX_SIZE
  value: {{ .Values.frames.postgres.pool.maxSize | quote }}
- name: COLLAB_HUB_API__FRAMES__POSTGRES__POOL__TIMEOUT_SECONDS
  value: {{ .Values.frames.postgres.pool.timeoutSeconds | quote }}
- name: COLLAB_HUB_API__FRAMES__POSTGRES__POOL__MAX_WAITING
  value: {{ .Values.frames.postgres.pool.maxWaiting | quote }}
- name: COLLAB_HUB_API__TASKS__BACKEND
  value: {{ .Values.tasks.backend | quote }}
- name: COLLAB_HUB_API__TASKS__POSTGRES_URL
  {{- if .Values.tasks.postgres.existingSecret }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.tasks.postgres.existingSecret | quote }}
      key: {{ .Values.tasks.postgres.databaseUrlKey | quote }}
  {{- else }}
  value: {{ .Values.tasks.postgres.url | quote }}
  {{- end }}
- name: COLLAB_HUB_API__TASKS__AUTO_MIGRATE
  value: {{ .Values.tasks.postgres.autoMigrate | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GOOGLE__BROKER_TOKEN_URL
  value: {{ .Values.connectors.google.brokerTokenUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GOOGLE__DRIVE_API_BASE_URL
  value: {{ .Values.connectors.google.driveApiBaseUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GOOGLE__GMAIL_API_BASE_URL
  value: {{ .Values.connectors.google.gmailApiBaseUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GOOGLE__CALENDAR_API_BASE_URL
  value: {{ .Values.connectors.google.calendarApiBaseUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GOOGLE__REQUEST_TIMEOUT_SECONDS
  value: {{ .Values.connectors.google.requestTimeoutSeconds | quote }}
- name: COLLAB_HUB_API__CONNECTORS__SLACK__BROKER_TOKEN_URL
  value: {{ .Values.connectors.slack.brokerTokenUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__SLACK__API_BASE_URL
  value: {{ .Values.connectors.slack.apiBaseUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__SLACK__REQUEST_TIMEOUT_SECONDS
  value: {{ .Values.connectors.slack.requestTimeoutSeconds | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GITHUB__BROKER_TOKEN_URL
  value: {{ .Values.connectors.github.brokerTokenUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GITHUB__API_BASE_URL
  value: {{ .Values.connectors.github.apiBaseUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GITHUB__REQUEST_TIMEOUT_SECONDS
  value: {{ .Values.connectors.github.requestTimeoutSeconds | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GITHUB__ALLOWED_ORGS
  value: {{ toJson .Values.connectors.github.allowedOrgs | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GITHUB__API_GET_ENABLED
  value: {{ .Values.connectors.github.apiGetEnabled | quote }}
- name: COLLAB_HUB_API__CONNECTORS__GITHUB__API_GET_MAX_CONCURRENCY
  value: {{ .Values.connectors.github.apiGetMaxConcurrency | quote }}
- name: COLLAB_HUB_API__CONNECTORS__NOTION__BROKER_TOKEN_URL
  value: {{ .Values.connectors.notion.brokerTokenUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__NOTION__API_BASE_URL
  value: {{ .Values.connectors.notion.apiBaseUrl | quote }}
- name: COLLAB_HUB_API__CONNECTORS__NOTION__NOTION_VERSION
  value: {{ .Values.connectors.notion.notionVersion | quote }}
- name: COLLAB_HUB_API__CONNECTORS__NOTION__REQUEST_TIMEOUT_SECONDS
  value: {{ .Values.connectors.notion.requestTimeoutSeconds | quote }}
{{- with .Values.frames.auth.idToken.jwksUrl }}
- name: FRAMES_IDTOKEN_JWKS_URL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.idToken.issuer }}
- name: FRAMES_IDTOKEN_ISSUER
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.idToken.audience }}
- name: FRAMES_IDTOKEN_AUDIENCE
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.bearer.jwksUrl }}
- name: FRAMES_BEARER_JWKS_URL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.bearer.issuer }}
- name: FRAMES_BEARER_ISSUER
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.bearer.audience }}
- name: FRAMES_BEARER_AUDIENCE
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.cliClientId }}
- name: COLLAB_HUB_CLI_CLIENT_ID
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.identityClaim }}
- name: FRAMES_AUTH_IDENTITY_CLAIM
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.orgSource }}
- name: FRAMES_AUTH_ORG_SOURCE
  value: {{ . | quote }}
{{- end }}
{{- if eq .Values.frames.auth.orgSource "single" }}
- name: FRAMES_AUTH_SINGLE_ORG_ID
  value: {{ .Values.frames.auth.singleOrg.id | quote }}
- name: FRAMES_AUTH_SINGLE_ORG_NAME
  value: {{ .Values.frames.auth.singleOrg.name | quote }}
- name: FRAMES_AUTH_SINGLE_ORG_MEMBER_SOURCES
  value: {{ join "," .Values.frames.auth.singleOrg.memberSources | quote }}
{{- end }}
{{- with .Values.frames.auth.defaults.orgId }}
- name: FRAMES_AUTH_DEFAULT_ORG
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.auth.defaults.workspaceId }}
- name: FRAMES_AUTH_DEFAULT_WORKSPACE
  value: {{ . | quote }}
{{- end }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__PROVIDER
  value: {{ .Values.frames.email.provider | quote }}
{{- with .Values.frames.email.acceptUrl }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__ACCEPT_URL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.frames.email.appInstructions }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__APP_INSTRUCTIONS
  value: {{ . | quote }}
{{- end }}
{{- if eq .Values.frames.email.provider "ses" }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__SES__SENDER_ADDRESS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.email.ses.existingSecret | quote }}
      key: {{ .Values.frames.email.ses.senderAddressKey | quote }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__SES__REGION
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.email.ses.existingSecret | quote }}
      key: {{ .Values.frames.email.ses.regionKey | quote }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__SES__CONFIGURATION_SET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.email.ses.existingSecret | quote }}
      key: {{ .Values.frames.email.ses.configurationSetKey | quote }}
- name: COLLAB_HUB_API__FRAMES__EMAIL__SES__REQUEST_TIMEOUT_SECONDS
  value: {{ .Values.frames.email.ses.requestTimeoutSeconds | quote }}
{{- end }}
{{- /* Two hazards, both the same reflowing mistake at different
       depths, and neither caught by the schema because the key is
       gone before validation.

       `eq ... "false"` and not `if not`: a nil value is falsy, and
       Helm strips null-valued keys during coalescing rather than
       falling back to the chart default -- so `requireVerifiedEmail:`
       left empty rendered "false" and relaxed Gate B on a
       deployment that never asked.

       `dig` with `default dict`: commenting out the key and leaving
       the `invitations:` header made the whole submap nil, and
       indexing it was a template error rather than a fallback.
       Failing closed, but a deployment that cannot render at all is
       still a deployment down. Only an explicit false renders. */}}
{{- if eq (dig "requireVerifiedEmail" true (.Values.frames.invitations | default dict) | toString) "false" }}
{{- /*
  Rendered only when turned OFF, so the strict default needs no env
  var and cannot be weakened by an empty or malformed value. A
  deployment that wants the relaxation says so explicitly, and the
  rendered manifest shows it -- which is the point, since it is a
  security trade rather than a preference.
*/}}
- name: COLLAB_HUB_API__FRAMES__INVITATIONS__REQUIRE_VERIFIED_EMAIL
  value: "false"
{{- end }}
{{- if .Values.frames.serviceAccess.grantOnAcceptance }}
{{- /*
  Rendered only when there is something to grant. An empty list is
  the default, and it must stay absent rather than be rendered as
  "[]": the API treats "configured to grant" as the trigger for
  requiring a credential, and an explicitly empty value would be
  indistinguishable from the default while still occupying the
  env var.

  toJson because pydantic-settings parses complex types from the
  environment as JSON. A comma-joined string would be read as a
  single one-element list, and the group it named would not exist.
*/}}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__GRANT_ON_ACCEPTANCE
  value: {{ .Values.frames.serviceAccess.grantOnAcceptance | toJson | quote }}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__KEYCLOAK__ISSUER_URL
  value: {{ .Values.frames.serviceAccess.keycloak.issuerUrl | quote }}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__KEYCLOAK__TOKEN_URL
  value: {{ .Values.frames.serviceAccess.keycloak.tokenUrl | quote }}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__KEYCLOAK__ADMIN_API_BASE_URL
  value: {{ .Values.frames.serviceAccess.keycloak.adminApiBaseUrl | quote }}
{{- if .Values.frames.serviceAccess.keycloak.groupIds }}
{{- /*
  Rendered only when set: an empty mapping means "look every path
  up", and an env var holding `{}` would be indistinguishable from
  that while still occupying it -- the same reasoning as the group
  list above. toJson for the same reason too, since pydantic-settings
  parses a dict from the environment as JSON.
*/}}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__KEYCLOAK__GROUP_IDS
  value: {{ .Values.frames.serviceAccess.keycloak.groupIds | toJson | quote }}
{{- end }}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__KEYCLOAK__CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.serviceAccess.keycloak.existingSecret | quote }}
      key: {{ .Values.frames.serviceAccess.keycloak.clientIdKey | quote }}
- name: COLLAB_HUB_API__FRAMES__SERVICE_ACCESS__KEYCLOAK__CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.frames.serviceAccess.keycloak.existingSecret | quote }}
      key: {{ .Values.frames.serviceAccess.keycloak.clientSecretKey | quote }}
{{- end }}
{{- if .Values.userDirectory.enabled }}
- name: COLLAB_HUB_API__USER_DIRECTORY__ENABLED
  value: {{ .Values.userDirectory.enabled | quote }}
- name: COLLAB_HUB_API__USER_DIRECTORY__PROVIDER
  value: {{ .Values.userDirectory.provider | quote }}
- name: COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__ISSUER_URL
  value: {{ .Values.userDirectory.keycloak.issuerUrl | quote }}
- name: COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__TOKEN_URL
  value: {{ .Values.userDirectory.keycloak.tokenUrl | quote }}
- name: COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__ADMIN_API_BASE_URL
  value: {{ .Values.userDirectory.keycloak.adminApiBaseUrl | quote }}
- name: COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.userDirectory.keycloak.existingSecret | quote }}
      key: {{ .Values.userDirectory.keycloak.clientIdKey | quote }}
- name: COLLAB_HUB_API__USER_DIRECTORY__KEYCLOAK__CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.userDirectory.keycloak.existingSecret | quote }}
      key: {{ .Values.userDirectory.keycloak.clientSecretKey | quote }}
{{- end }}
{{- /*
  Cog registry (issue #87). The enabled flag always renders so the
  deployment states its intent; the tuning and the source list
  render only when there is something for them to govern, which
  keeps a deployment without Cogs free of cogs env beyond that one
  line. Secrets are never in the JSON: each source's Secret keys
  are mounted as their own env vars below, under the names the JSON
  points at (see _helpers.tpl "collab-hub.cogs-source-env-name").

  Which process sweeps is decided here, not by the operator (issue
  #148): the API replicas always render `enabled=false` and serve the
  catalog read API; the one indexer replica (indexer-deployment.yaml)
  renders `enabled=true` with the tuning. Single flight is therefore a property of the
  deployment shape, and the store's advisory lock is the belt under it.
*/}}
- name: COLLAB_HUB_API__COGS__INDEX__ENABLED
  value: {{ $indexer | quote }}
{{- if $indexer }}
- name: COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS
  value: {{ .Values.cogs.index.intervalSeconds | quote }}
- name: COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP
  value: {{ .Values.cogs.index.runOnStartup | quote }}
{{- end }}
{{- if .Values.cogs.registry.sources }}
- name: COLLAB_HUB_API__COGS__REGISTRY_SOURCES
  value: {{ include "collab-hub.cogs-registry-sources" . | quote }}
{{- range .Values.cogs.registry.sources }}
{{- $sourceId := .id }}
{{- $credentials := .credentials | default dict }}
{{- with $credentials.existingSecret }}
- name: {{ include "collab-hub.cogs-source-env-name" (dict "id" $sourceId "suffix" "USERNAME") }}
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: {{ $credentials.usernameKey | default "username" | quote }}
- name: {{ include "collab-hub.cogs-source-env-name" (dict "id" $sourceId "suffix" "PASSWORD") }}
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: {{ $credentials.passwordKey | default "password" | quote }}
{{- end }}
{{- $webhook := .webhook | default dict }}
{{- with $webhook.existingSecret }}
- name: {{ include "collab-hub.cogs-source-env-name" (dict "id" $sourceId "suffix" "WEBHOOK_SECRET") }}
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: {{ $webhook.secretKey | default "secret" | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- range $name, $on := .Values.features }}
- name: {{ printf "COLLAB_HUB_API__FEATURES__%s" ($name | upper) | quote }}
  value: {{ $on | toString | quote }}
{{- end }}
{{- with .Values.api.deployment.extraEnv }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end -}}
{{- end -}}
