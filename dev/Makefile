# Local development for the Collab Hub API.
#
# Start here:  make help
# Full guide:  dev/README.md
#
# Every api-* target starts the containers it needs (idempotently) before
# launching the API, so you never run `docker compose` by hand. `make api` is
# the exception: it starts nothing at all. See the table in dev/README.md.
#
# Prerequisites: uv, plus docker (with compose) for everything above `make api`.
# The kind-* targets additionally need kind, helm and kubectl.
#
# Keep this parseable by GNU Make 3.81, which is what macOS ships. In
# particular: no "#" inside a $(...) call -- 3.81 reads it as the start of a
# comment and truncates the line mid-call -- and nothing newer than 3.81.

SHELL := /bin/bash

ROOT        := $(abspath $(CURDIR)/..)
API_DIR     := $(ROOT)/api
CLI_DIR     := $(ROOT)/cli
CLI         := $(CLI_DIR)/.venv/bin/collab-hub
LOCAL       := $(CURDIR)/.local
COMPOSE     := docker compose -f $(CURDIR)/compose.yaml
ALL_PROFILES := --profile auth --profile fakes --profile desktop

# --- API process -----------------------------------------------------------
API_HOST    ?= 127.0.0.1
API_PORT    ?= 8000
API_URL     := http://$(API_HOST):$(API_PORT)

# --- Helpers ---------------------------------------------------------------
# Reads one field out of a JSON response, and decodes a JWT payload for
# `make sub`. Prefers the API's own virtualenv -- the interpreter pinned by
# api/.python-version, present as soon as any api-* target has run -- then
# whatever python is on PATH. Same precedence as resolve_python() in
# scripts/smoke_frames_common.sh. Deliberately not `uv run python`: that
# re-resolves the project on every call, and these run inside `$$(...)`
# substitutions throughout dev/README.md.
PYTHON      ?= $(shell [ -x "$(API_DIR)/.venv/bin/python" ] \
                 && printf '%s' "$(API_DIR)/.venv/bin/python" \
                 || command -v python3 || command -v python || printf 'python3')

# --- Keycloak --------------------------------------------------------------
KC_URL      ?= http://localhost:8080
REALM       ?= nebari
KC_ADMIN    ?= admin
KC_ADMIN_PW ?= admin
# Exported so compose seeds the container with the same credentials kcadm
# sends. Bootstrap credentials are only read against an empty database, so a
# change lands on the next `make destroy`, not the next restart.
export KC_ADMIN
export KC_ADMIN_PW
KC_USER     ?= dev
KC_PASS     ?= dev
KCADM       := $(COMPOSE) exec -T keycloak /opt/keycloak/bin/kcadm.sh

# --- Apollo Desktop front door -------------------------------------------
# The desktop derives every service origin from the issuer hostname, so all
# four must answer on one port. `make hub-proxy` puts them there.
DESKTOP_PORT ?= 9080

# Exported so docker compose -- and through it the Caddyfile -- sees the same
# values: the front door's listener, its published port and its upstream all
# derive from these, so an override moves the whole set rather than half of it.
# The exports MUST follow the assignments: a bare `export NAME` ahead of a
# `?=` defines the variable as empty, and the `?=` then does nothing.
export DESKTOP_PORT
export API_PORT

DESKTOP_KC   := http://keycloak.localhost:$(DESKTOP_PORT)
# curl needs these when /etc/hosts is not set up yet; harmless when it is.
DESKTOP_RESOLVE := --resolve keycloak.localhost:$(DESKTOP_PORT):127.0.0.1 \
                   --resolve frames.localhost:$(DESKTOP_PORT):127.0.0.1
DESKTOP_HOSTS := keycloak.localhost frames.localhost llm-internal.localhost


# --- Datastores ------------------------------------------------------------
PG_URL      ?= postgresql://collab:collab@127.0.0.1:5432/collab
S3_ENDPOINT ?= http://127.0.0.1:9000

# --- kind ------------------------------------------------------------------
CLUSTER_NAME ?= collab-hub-dev
NAMESPACE    ?= collab-hub
RELEASE      ?= collab-hub
IMAGE        ?= collab-hub-api:dev
# The chart takes repository and tag separately, so IMAGE has to be split or the
# install silently ignores it. Split on the LAST colon, and only when what
# follows looks like a tag rather than a registry port -- a port is followed by
# a path, so it contains a "/". `localhost:5000/collab-hub-api:dev` therefore
# splits after the second colon, not the first.
# The sed delimiter is "|", not "#": macOS ships GNU Make 3.81, which treats a
# "#" as the start of a comment even inside $(shell ...) and truncates the line
# mid-call ("unterminated call to function `shell'").
IMAGE_REPO   := $(shell printf '%s' '$(IMAGE)' | sed -E 's|:[^:/]+$$||')
IMAGE_TAG    := $(shell printf '%s' '$(IMAGE)' | sed -nE 's|.*:([^:/]+)$$|\1|p')
FORWARD_PORT ?= 18080
KUBECTL      := kubectl --context kind-$(CLUSTER_NAME)

# ---------------------------------------------------------------------------
# Environment blocks. Passed through `env` rather than exported, so each target
# states exactly what the process it starts can see.
# ---------------------------------------------------------------------------

# Server binding + on-disk frame content.
BASE_ENV := \
  COLLAB_HUB_API__SERVER__HOSTNAME=$(API_HOST) \
  COLLAB_HUB_API__SERVER__PORT=$(API_PORT) \
  COLLAB_HUB_API__STORAGE__FRAMES_PATH=$(LOCAL)/frames

# The dev-auth shortcut. All THREE are required: FRAMES_UNSAFE_AUTH_ENABLED
# opens the door, DEV_AUTH_ENABLED turns the shortcut on, DEV_AUTH_USER names
# the subject. Setting only DEV_AUTH_USER authenticates nothing.
DEV_AUTH_ENV := \
  FRAMES_UNSAFE_AUTH_ENABLED=true \
  DEV_AUTH_ENABLED=true \
  DEV_AUTH_USER=dev-user \
  DEV_AUTH_ORG=dev-org \
  DEV_AUTH_WORKSPACE=default

# Level 1 only: the Cog catalog in process memory, so the catalog read API
# (/v1/cogs) answers 200 with no database. Every other level rides Postgres,
# where the catalog is durable; a deployment has no such switch.
LEVEL1_ENV := \
  COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory

# Level 1 only: the run API (/v1/runs), behind its feature flag, over the SQLite
# Track `make controller` watches. PACKAGES are the directories a Cog package
# may be launched from, the same ones the controller is given.
COGS ?= $(CURDIR)/cogs:$(ROOT)/cogs:$(ROOT)/examples/cog-local/cogs
RUNS_ENV := \
  COLLAB_HUB_API__FEATURES__COG_RUNS=true \
  COLLAB_HUB_API__RUNS__TRACK_PATH=$(LOCAL)/track.sqlite \
  COLLAB_HUB_API__RUNS__PACKAGES=$(COGS)

# Real bearer verification against the local Keycloak realm.
#
# FRAMES_AUTH_IDENTITY_CLAIM=sub matches the production convention: the OIDC
# subject is the ACL principal. Without the pin the legacy precedence applies
# (preferred_username, then email, then sub), so frames would be owned by a
# mutable username and `make seed-org SUB=...` would key rows nobody matches.
#
# FRAMES_UNSAFE_AUTH_ENABLED=true is here because the local Keycloak's JWKS
# URL is plain http, which the API refuses at startup otherwise (issue #77).
# On its own it enables nothing else: the dev-auth shortcut still needs
# DEV_AUTH_ENABLED, and unsigned tokens still need FRAMES_BEARER_ALLOW_UNSIGNED.
OIDC_BASE_ENV := \
  FRAMES_UNSAFE_AUTH_ENABLED=true \
  FRAMES_BEARER_JWKS_URL=$(KC_URL)/realms/$(REALM)/protocol/openid-connect/certs \
  FRAMES_BEARER_ISSUER=$(KC_URL)/realms/$(REALM) \
  FRAMES_BEARER_AUDIENCE=apollo-desktop \
  FRAMES_AUTH_IDENTITY_CLAIM=sub

# Organization from token claims: one shared org, no database needed.
CLAIMS_ORG_ENV := \
  FRAMES_AUTH_DEFAULT_ORG=dev-org \
  FRAMES_AUTH_DEFAULT_WORKSPACE=default

OIDC_ENV := $(OIDC_BASE_ENV) $(CLAIMS_ORG_ENV)

# Organization from the server's own collab_org_members table. Needs Postgres,
# the sub pin, and BOTH defaults left unset -- the API refuses to start
# otherwise. Seed a membership row first: make seed-org SUB=$$(make -s sub)
MEMBERSHIP_ENV := \
  $(OIDC_BASE_ENV) \
  FRAMES_AUTH_ORG_SOURCE=membership

PG_ENV := \
  COLLAB_HUB_API__FRAMES__POSTGRES__URL=$(PG_URL) \
  COLLAB_HUB_API__FRAMES__POSTGRES__AUTO_MIGRATE=true \
  COLLAB_HUB_API__FRAMES__ACTIVE_STATE__BACKEND=postgres \
  COLLAB_HUB_API__TASKS__BACKEND=postgres

S3_ENV := \
  COLLAB_HUB_API__FRAMES__STORAGE_BACKEND=s3 \
  COLLAB_HUB_API__FRAMES__S3__BUCKET=frames \
  COLLAB_HUB_API__FRAMES__S3__ENDPOINT_URL=$(S3_ENDPOINT) \
  COLLAB_HUB_API__FRAMES__S3__REGION=us-east-1 \
  AWS_ACCESS_KEY_ID=devaccesskey \
  AWS_SECRET_ACCESS_KEY=devsecretkey

# The server-rendered /web and /admin pages.
WEB_ENV := \
  COLLAB_HUB_API__WEB__CLIENT_ID=collab-web \
  COLLAB_HUB_API__WEB__CLIENT_SECRET=collab-web-dev-secret \
  COLLAB_HUB_API__WEB__ISSUER_URL=$(KC_URL)/realms/$(REALM) \
  COLLAB_HUB_API__WEB__PUBLIC_BASE_URL=http://localhost:$(API_PORT) \
  COLLAB_HUB_API__WEB__SESSION_SECRET=local-dev-session-secret-change-me-1234

# Connectors pointed at the compose fakes.
FAKE_CONNECTOR_ENV := \
  COLLAB_HUB_API__CONNECTORS__GOOGLE__BROKER_TOKEN_URL=http://127.0.0.1:8081/broker/token \
  COLLAB_HUB_API__CONNECTORS__GOOGLE__DRIVE_API_BASE_URL=http://127.0.0.1:8081/drive/v3 \
  COLLAB_HUB_API__CONNECTORS__GOOGLE__GMAIL_API_BASE_URL=http://127.0.0.1:8081/gmail/v1 \
  COLLAB_HUB_API__CONNECTORS__GOOGLE__CALENDAR_API_BASE_URL=http://127.0.0.1:8081/calendar/v3 \
  COLLAB_HUB_API__CONNECTORS__SLACK__BROKER_TOKEN_URL=http://127.0.0.1:8082/broker/token \
  COLLAB_HUB_API__CONNECTORS__SLACK__API_BASE_URL=http://127.0.0.1:8082/api \
  COLLAB_HUB_API__CONNECTORS__GITHUB__BROKER_TOKEN_URL=http://127.0.0.1:8083/broker/token \
  COLLAB_HUB_API__CONNECTORS__GITHUB__API_BASE_URL=http://127.0.0.1:8083/api \
  COLLAB_HUB_API__CONNECTORS__SHAREPOINT__BROKER_TOKEN_URL=http://127.0.0.1:8084/broker/token \
  COLLAB_HUB_API__CONNECTORS__SHAREPOINT__API_BASE_URL=http://127.0.0.1:8084/v1.0

# Bearer verification for tokens minted through the desktop front door. The
# issuer must be the origin the token was ACTUALLY minted on -- the desktop
# reaches Keycloak at keycloak.localhost:$(DESKTOP_PORT), so that is the `iss`
# its tokens carry, and the API compares it exactly.
# FRAMES_UNSAFE_AUTH_ENABLED: same http-JWKS allowance as OIDC_BASE_ENV.
DESKTOP_OIDC_ENV := \
  FRAMES_UNSAFE_AUTH_ENABLED=true \
  FRAMES_BEARER_JWKS_URL=$(DESKTOP_KC)/realms/$(REALM)/protocol/openid-connect/certs \
  FRAMES_BEARER_ISSUER=$(DESKTOP_KC)/realms/$(REALM) \
  FRAMES_BEARER_AUDIENCE=apollo-desktop \
  FRAMES_AUTH_IDENTITY_CLAIM=sub \
  $(CLAIMS_ORG_ENV)

# Connectors brokered by the local Keycloak (real providers).
KC_CONNECTOR_ENV := \
  COLLAB_HUB_API__CONNECTORS__GOOGLE__BROKER_TOKEN_URL=$(KC_URL)/realms/$(REALM)/broker/google/token \
  COLLAB_HUB_API__CONNECTORS__SLACK__BROKER_TOKEN_URL=$(KC_URL)/realms/$(REALM)/broker/slack/token \
  COLLAB_HUB_API__CONNECTORS__GITHUB__BROKER_TOKEN_URL=$(KC_URL)/realms/$(REALM)/broker/github/token \
  COLLAB_HUB_API__CONNECTORS__SHAREPOINT__BROKER_TOKEN_URL=$(KC_URL)/realms/$(REALM)/broker/microsoft/token

.DEFAULT_GOAL := help
.PHONY: help api api-watch api-pg api-oidc api-fakes api-full api-membership \
        api-desktop api-desktop-fakes hosts-check hub-proxy hub-proxy-down desktop-check \
        postgres services keycloak fakes up down destroy logs ps \
        keycloak-admin realm-import broker-role idp-google idp-github idp-microsoft idp-list \
        token sub whoami seed-org psql \
        kind-up kind-cluster kind-image kind-redeploy kind-forward kind-logs \
        kind-smoke kind-down test lint clean op controller fake-model

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "; \
		print ""; print "Collab Hub — local development"; print ""; \
		print "  Full walkthrough: dev/README.md"; print ""} \
		/^## /{printf "\n\033[1m%s\033[0m\n", substr($$0, 4); next} \
		/^[a-zA-Z0-9_-]+:.*?## /{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)
	@echo ""

# ---------------------------------------------------------------------------
## Level 1 — the API process
# ---------------------------------------------------------------------------

api: | $(LOCAL)/frames ## The API alone: no containers, dev auth, local-FS frames, in-memory stores
	cd $(API_DIR) && env $(BASE_ENV) $(DEV_AUTH_ENV) $(LEVEL1_ENV) $(RUNS_ENV) \
		uv run --group runs python -m collab_hub_api

api-watch: | $(LOCAL)/frames ## Same as `api` (no containers), restarted on every source change
	cd $(API_DIR) && env $(BASE_ENV) $(DEV_AUTH_ENV) $(LEVEL1_ENV) $(RUNS_ENV) \
		uv run --group runs --with watchfiles watchfiles 'python -m collab_hub_api' src

# ---------------------------------------------------------------------------
## Level 1 — running Cogs and Ops
# ---------------------------------------------------------------------------

OP       ?= echo
BACKEND  ?= none
LOCATION ?=

# No container. Without a LOCATION the fake Cogs under cogs/ answer in the same
# process. With LOCATION=local each step's worker is a real process: the Cog
# package's `serve` task, run in its own pixi environment (pixi is needed for
# that, and only for that), on a loopback port, killed when the step ends or
# when this host dies. Either way the Track is the SQLite one in .local/. The
# execution package's own environment runs it; PyYAML reads the Op.
op: ## Run a fake Op and print its Track (OP=echo|needs-review|fails|slow|spender, LOCATION=local for real worker processes)
	@if [ "$(LOCATION)" = local ] && ! command -v pixi >/dev/null; then \
		echo "LOCATION=local runs each fake Cog in its own pixi environment: install pixi (https://pixi.sh), or run without LOCATION"; exit 1; fi
	cd $(ROOT)/execution && LOCATION=$(LOCATION) uv run --quiet --with pyyaml python $(CURDIR)/op.py $(OP) \
		--backend $(BACKEND) --track $(LOCAL)/track.sqlite

# The process that advances the runs `make api` (or `make api-oidc`) accepts: it
# watches the same SQLite Track, starts each submitted run's worker as a local
# process (the Cog package's `serve` task in its own pixi environment), and
# delivers cancels and turns. No limit on how long an interaction takes: a Cog
# holding a session stays in one for as long as someone talks to it.
controller: | $(LOCAL)/frames ## The run controller for `make api`: runs the Cogs launched through /v1/runs as local processes
	@command -v pixi >/dev/null || { \
		echo "the controller runs each Cog in its own pixi environment: install pixi (https://pixi.sh)"; exit 1; }
	cd $(ROOT)/execution && uv run --quiet python -m collab_hub_execution.controller \
		--track $(LOCAL)/track.sqlite $(foreach dir,$(subst :, ,$(COGS)),--packages $(dir)) \
		--work-dir $(LOCAL)/runs --backend $(BACKEND) --location local --interaction-timeout 0 \
		--deliver hermes:COLLAB_MODEL_PROVIDER --deliver hermes:COLLAB_MODEL_BASE_URL \
		--deliver hermes:COLLAB_MODEL_NAME --deliver hermes:COLLAB_MODEL_API_KEY

# The model the Hermes Cog calls: `make fake-model` by default. Point it at a real
# OpenAI-compatible endpoint by setting these before `make controller`, or at
# Claude with COLLAB_MODEL_PROVIDER=anthropic; the key is read from your
# environment, never from the make command line.
FAKE_MODEL_PORT ?= 8090
ifneq ($(COLLAB_MODEL_PROVIDER),anthropic)
# The fake model's defaults; with Claude the Cog picks its own model unless COLLAB_MODEL_NAME says.
export COLLAB_MODEL_BASE_URL ?= http://127.0.0.1:$(FAKE_MODEL_PORT)/v1
export COLLAB_MODEL_NAME ?= fake-model
endif

fake-model: ## An OpenAI-compatible model that answers without one, for the Hermes Cog (port 8090)
	python3 $(CURDIR)/fake-model/fake_model.py --port $(FAKE_MODEL_PORT)

# ---------------------------------------------------------------------------
## Level 2 — API with real datastores
# ---------------------------------------------------------------------------

api-pg: postgres | $(LOCAL)/frames ## Starts Postgres, then the API: history, groups, orgs, tasks
	cd $(API_DIR) && env $(BASE_ENV) $(DEV_AUTH_ENV) $(PG_ENV) uv run python -m collab_hub_api

# ---------------------------------------------------------------------------
## Level 3 — API with Keycloak (real bearer tokens, web surface, connectors)
# ---------------------------------------------------------------------------

api-oidc: postgres keycloak | $(LOCAL)/frames ## API verifying real Keycloak bearer tokens (no dev-auth shortcut)
	cd $(API_DIR) && env $(BASE_ENV) $(OIDC_ENV) $(PG_ENV) $(RUNS_ENV) uv run --group runs python -m collab_hub_api

# Connector routes exchange the CALLER's bearer with the broker, so the
# dev-auth shortcut cannot reach them -- it authenticates a subject without
# ever producing a token to forward. This target therefore runs real Keycloak
# verification and points only the provider APIs at the fakes.
api-fakes: postgres keycloak fakes | $(LOCAL)/frames ## API + Keycloak + fake Google/Slack/GitHub/Graph connector providers
	cd $(API_DIR) && env $(BASE_ENV) $(OIDC_ENV) $(PG_ENV) $(FAKE_CONNECTOR_ENV) \
		uv run python -m collab_hub_api

api-full: services keycloak | $(LOCAL)/frames ## Everything: OIDC + Postgres + S3 + web surface + Keycloak-brokered connectors
	cd $(API_DIR) && env $(BASE_ENV) $(OIDC_ENV) $(PG_ENV) $(S3_ENV) $(WEB_ENV) $(KC_CONNECTOR_ENV) \
		uv run python -m collab_hub_api

api-membership: postgres keycloak | $(LOCAL)/frames ## API in multi-tenant mode: organization comes from collab_org_members
	cd $(API_DIR) && env $(BASE_ENV) $(MEMBERSHIP_ENV) $(PG_ENV) $(WEB_ENV) uv run python -m collab_hub_api

# ---------------------------------------------------------------------------
## Apollo Desktop — connect the Collab client to this hub
# ---------------------------------------------------------------------------

# Three mechanisms because none is portable on its own: `getent` is glibc-only
# and absent on macOS, where it would report every hostname missing and block
# the desktop targets whatever /etc/hosts says; `dscacheutil` is macOS-only; the
# Python fallback covers anything else and adds no dependency, since $(PYTHON)
# is already what `make token` uses. Deliberately NOT `host` or `nslookup`:
# those query DNS directly and ignore /etc/hosts, which is the thing being
# checked, so they would answer "no" for a correctly configured machine.
hosts-check: ## Check the /etc/hosts entries the desktop client needs
	@if ! { command -v getent >/dev/null 2>&1 || command -v dscacheutil >/dev/null 2>&1 \
	        || $(PYTHON) -c '' >/dev/null 2>&1; }; then \
		echo "hosts-check: nothing here can resolve a hostname (no getent,"; \
		echo "dscacheutil or python), so the check is skipped rather than"; \
		echo "blocking you. Make sure these names point at 127.0.0.1:"; \
		echo "  $(DESKTOP_HOSTS)"; \
		exit 0; \
	fi; \
	missing=""; for h in $(DESKTOP_HOSTS); do \
		if command -v getent >/dev/null 2>&1 && getent hosts "$$h" >/dev/null 2>&1; then :; \
		elif command -v dscacheutil >/dev/null 2>&1 \
		     && dscacheutil -q host -a name "$$h" 2>/dev/null | grep -q ip_address; then :; \
		elif $(PYTHON) -c 'import socket,sys; socket.getaddrinfo(sys.argv[1], None)' "$$h" \
		     >/dev/null 2>&1; then :; \
		else missing="$$missing $$h"; fi; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "Missing from /etc/hosts:$$missing"; \
		echo ""; \
		echo "Add them once (they all point at your own machine):"; \
		echo "  echo '127.0.0.1 $(DESKTOP_HOSTS)' | sudo tee -a /etc/hosts"; \
		exit 1; \
	fi; \
	echo "All desktop hostnames resolve."

hub-proxy: hosts-check services ## Start the single-port front door on :9080
	$(COMPOSE) --profile auth up -d --wait keycloak
	$(COMPOSE) --profile auth --profile desktop up -d hub-proxy
	@echo ""
	@echo "Front door ready. In Collab, enter this as the Hub address:"
	@echo ""
	@echo "    http://localhost:$(DESKTOP_PORT)"
	@echo ""
	@echo "The http:// is required: an address without a scheme is treated as"
	@echo "https, and this front door speaks plaintext."
	@echo ""
	@echo "Then run 'make api-desktop' in another shell. Sign in as dev / dev."

# Binds 0.0.0.0, unlike every other target. The front door runs in a container
# and reaches your machine through the docker gateway, which a process bound to
# 127.0.0.1 will not answer on. Nothing else here binds beyond loopback, and
# this mode has no dev-auth shortcut -- a caller still needs a valid token.
api-desktop: hub-proxy | $(LOCAL)/frames ## API reachable by Apollo Desktop (binds 0.0.0.0)
	cd $(API_DIR) && env $(BASE_ENV) COLLAB_HUB_API__SERVER__HOSTNAME=0.0.0.0 \
		$(DESKTOP_OIDC_ENV) $(PG_ENV) uv run python -m collab_hub_api

api-desktop-fakes: hub-proxy fakes | $(LOCAL)/frames ## Same, with the fake connectors wired in so Collab shows them connected
	cd $(API_DIR) && env $(BASE_ENV) COLLAB_HUB_API__SERVER__HOSTNAME=0.0.0.0 \
		$(DESKTOP_OIDC_ENV) $(PG_ENV) $(FAKE_CONNECTOR_ENV) uv run python -m collab_hub_api

desktop-check: ## Probe the three origins the desktop actually calls (issuer, hub API, Frames)
	@set -e; \
	printf '%-34s ' "keycloak.localhost:$(DESKTOP_PORT) (issuer)"; \
	curl -sf -m 5 $(DESKTOP_RESOLVE) http://keycloak.localhost:$(DESKTOP_PORT)/realms/$(REALM)/.well-known/openid-configuration \
		| $(PYTHON) -c 'import json,sys; print("OK  iss =", json.load(sys.stdin)["issuer"])'; \
	printf '%-34s ' "localhost:$(DESKTOP_PORT) (hub API)"; \
	curl -sf -m 5 -o /dev/null -w 'OK  /healthz -> %{http_code}\n' http://localhost:$(DESKTOP_PORT)/healthz; \
	printf '%-34s ' "frames.localhost:$(DESKTOP_PORT) (frames)"; \
	curl -s -m 5 $(DESKTOP_RESOLVE) -o /dev/null -w 'OK  /v1/frames -> %{http_code}\n' http://frames.localhost:$(DESKTOP_PORT)/v1/frames

hub-proxy-down: ## Stop the front door (Keycloak stays up on localhost:8080)
	$(COMPOSE) --profile auth --profile desktop rm -sf hub-proxy

# ---------------------------------------------------------------------------
## Supporting services (docker compose)
# ---------------------------------------------------------------------------

postgres: ## Start Postgres alone: every API target but api-full keeps frames on the local filesystem
	$(COMPOSE) up -d --wait postgres

services: ## Start Postgres + the S3 frame store
	$(COMPOSE) up -d --wait postgres s3
	$(COMPOSE) up s3-init

keycloak: ## Start Keycloak and import the `nebari` realm
	$(COMPOSE) --profile auth up -d --wait keycloak
	@echo "Keycloak admin console: $(KC_URL)  ($(KC_ADMIN)/$(KC_ADMIN_PW))"

fakes: ## Start the fake Google, Slack, GitHub and Microsoft Graph providers
	$(COMPOSE) --profile fakes up -d fake-google fake-slack fake-github fake-graph

up: services keycloak fakes ## Start every supporting service

down: ## Stop every supporting service (data volumes survive)
	$(COMPOSE) $(ALL_PROFILES) down

destroy: ## Stop services AND delete their data volumes
	$(COMPOSE) $(ALL_PROFILES) down -v

ps: ## Show supporting-service status
	$(COMPOSE) $(ALL_PROFILES) ps

logs: ## Follow supporting-service logs
	$(COMPOSE) $(ALL_PROFILES) logs -f

# ---------------------------------------------------------------------------
## Keycloak administration
# ---------------------------------------------------------------------------

keycloak-admin: ## Print the admin console URL and check the credentials work
	@printf 'Admin console  %s\n' "$(KC_URL)"
	@printf 'Username       %s\n' "$(KC_ADMIN)"
	@printf 'Password       %s\n' "$(KC_ADMIN_PW)"
	@printf 'Sign-in        '
	@curl -sfS -m 10 $(KC_URL)/realms/master/protocol/openid-connect/token \
		-d grant_type=password -d client_id=admin-cli \
		-d username='$(KC_ADMIN)' -d password='$(KC_ADMIN_PW)' 2>/dev/null \
		| $(PYTHON) -c 'import json,sys; json.load(sys.stdin)["access_token"]; print("OK")' 2>/dev/null \
		|| { echo "FAILED"; \
		     echo ""; \
		     echo "Keycloak reads KC_BOOTSTRAP_ADMIN_* only against an empty database,"; \
		     echo "so changed credentials land on the next 'make destroy', not a restart."; \
		     exit 1; }

realm-import: ## Re-import the realm into a running Keycloak (overwrites it)
	$(KCADM) config credentials --server http://localhost:8080 \
		--realm master --user $(KC_ADMIN) --password $(KC_ADMIN_PW)
	-$(KCADM) delete realms/$(REALM)
	$(KCADM) create realms -f /opt/keycloak/data/import/realm-nebari.json
	@echo "Realm '$(REALM)' re-imported. Re-run 'make broker-role'."

broker-role: ## Grant every realm user the broker `read-token` role (REQUIRED for all connectors)
	$(KCADM) config credentials --server http://localhost:8080 \
		--realm master --user $(KC_ADMIN) --password $(KC_ADMIN_PW)
	$(KCADM) add-roles -r $(REALM) --rname default-roles-$(REALM) \
		--cclientid broker --rolename read-token
	@echo "default-roles-$(REALM) now includes broker:read-token"

idp-google: ## Create the Google IdP (GOOGLE_CLIENT_ID= GOOGLE_CLIENT_SECRET= required)
	@test -n "$(GOOGLE_CLIENT_ID)" -a -n "$(GOOGLE_CLIENT_SECRET)" || \
		{ echo "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required"; exit 1; }
	$(KCADM) config credentials --server http://localhost:8080 \
		--realm master --user $(KC_ADMIN) --password $(KC_ADMIN_PW)
	$(KCADM) create identity-provider/instances -r $(REALM) \
		-s alias=google -s providerId=google -s enabled=true \
		-s storeToken=true -s trustEmail=true -s hideOnLogin=true \
		-s 'config.clientId=$(GOOGLE_CLIENT_ID)' \
		-s 'config.clientSecret=$(GOOGLE_CLIENT_SECRET)' \
		-s 'config.offlineAccess=true' -s 'config.prompt=consent' \
		-s 'config.defaultScope=openid profile email https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly'
	@echo "Google IdP created. Redirect URI to register with Google:"
	@echo "  $(KC_URL)/realms/$(REALM)/broker/google/endpoint"

idp-github: ## Create the GitHub IdP (GITHUB_CLIENT_ID= GITHUB_CLIENT_SECRET= required)
	@test -n "$(GITHUB_CLIENT_ID)" -a -n "$(GITHUB_CLIENT_SECRET)" || \
		{ echo "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET are required"; exit 1; }
	$(KCADM) config credentials --server http://localhost:8080 \
		--realm master --user $(KC_ADMIN) --password $(KC_ADMIN_PW)
	$(KCADM) create identity-provider/instances -r $(REALM) \
		-s alias=github -s providerId=github -s enabled=true \
		-s storeToken=true -s hideOnLogin=true \
		-s 'config.clientId=$(GITHUB_CLIENT_ID)' \
		-s 'config.clientSecret=$(GITHUB_CLIENT_SECRET)' \
		-s 'config.defaultScope=user:email repo read:org read:project'
	@echo "GitHub IdP created. Callback URL to register with GitHub:"
	@echo "  $(KC_URL)/realms/$(REALM)/broker/github/endpoint"

idp-microsoft: ## Create the Microsoft IdP for SharePoint (MICROSOFT_CLIENT_ID= MICROSOFT_CLIENT_SECRET= MICROSOFT_TENANT_ID= required)
	@test -n "$(MICROSOFT_CLIENT_ID)" -a -n "$(MICROSOFT_CLIENT_SECRET)" -a -n "$(MICROSOFT_TENANT_ID)" || \
		{ echo "MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID are required"; exit 1; }
	$(KCADM) config credentials --server http://localhost:8080 \
		--realm master --user $(KC_ADMIN) --password $(KC_ADMIN_PW)
	$(KCADM) create identity-provider/instances -r $(REALM) \
		-s alias=microsoft -s displayName=Microsoft -s providerId=microsoft -s enabled=true \
		-s storeToken=true -s addReadTokenRoleOnCreate=true -s hideOnLogin=true -s linkOnly=true \
		-s 'config.clientId=$(MICROSOFT_CLIENT_ID)' \
		-s 'config.clientSecret=$(MICROSOFT_CLIENT_SECRET)' \
		-s 'config.tenantId=$(MICROSOFT_TENANT_ID)' \
		-s 'config.defaultScope=openid profile email offline_access User.Read Sites.Read.All Files.Read.All'
	@echo "Microsoft IdP created (alias microsoft, used by the SharePoint connector). Redirect URI to register in Entra:"
	@echo "  $(KC_URL)/realms/$(REALM)/broker/microsoft/endpoint"

idp-list: ## List the realm's identity providers
	$(KCADM) config credentials --server http://localhost:8080 \
		--realm master --user $(KC_ADMIN) --password $(KC_ADMIN_PW)
	$(KCADM) get identity-provider/instances -r $(REALM) \
		--fields alias,providerId,enabled,storeToken

# ---------------------------------------------------------------------------
## Tokens, identity, seed data
# ---------------------------------------------------------------------------

token: ## Print a bearer token for KC_USER (default: dev/dev)
	@curl -sfS -X POST $(KC_URL)/realms/$(REALM)/protocol/openid-connect/token \
		-d grant_type=password -d client_id=apollo-desktop -d scope=openid \
		-d username=$(KC_USER) -d password=$(KC_PASS) \
		| $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'

sub: ## Print the OIDC subject (sub claim) for KC_USER
	@$(MAKE) --no-print-directory token \
		| $(PYTHON) -c 'import base64,json,sys; p=sys.stdin.read().strip().split(".")[1]; print(json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4)))["sub"])'

whoami: ## Call the API with a real token and show what it resolved
	@curl -sfS -H "Authorization: Bearer $$($(MAKE) --no-print-directory token)" \
		$(API_URL)/v1/frames | head -c 400; echo

seed-org: ## Seed dev-org + owner membership + operator grant (SUB= EMAIL=)
	@test -n "$(SUB)" || { echo 'SUB is required, e.g.  make seed-org SUB=$$(make -s sub) EMAIL=dev@example.com'; exit 1; }
	$(COMPOSE) exec -T postgres psql -U collab -d collab \
		-v sub='$(SUB)' -v email='$(or $(EMAIL),dev@example.com)' < $(CURDIR)/sql/bootstrap.sql

psql: ## Open a psql shell on the dev database
	$(COMPOSE) exec postgres psql -U collab -d collab

# ---------------------------------------------------------------------------
## The collab-hub CLI
# ---------------------------------------------------------------------------

# `login` signs in the way the desktop does -- authorization code with PKCE on
# a 127.0.0.1 redirect, with the realm's apollo-desktop client -- so it needs a
# browser on this machine; `--with-token` takes `make token` instead.
cli: ## Install the collab-hub CLI into cli/.venv and print how to sign in at each level
	cd $(CLI_DIR) && uv sync --quiet
	@echo ""
	@echo "  collab-hub is $(CLI)"
	@echo "  level 1 (make api, dev auth):  $(CLI) --hub $(API_URL) whoami"
	@echo "  level 3 (make api-oidc):       $(CLI) --hub $(API_URL) login"
	@echo "  level 3 without a browser:     make -s -C dev token | $(CLI) --hub $(API_URL) login --with-token"

# ---------------------------------------------------------------------------
## kind — the chart on a real cluster
# ---------------------------------------------------------------------------

# `--set` follows `-f` so it wins: values/kind.yaml carries the default image,
# and this pins whatever IMAGE actually named -- the same one kind-image just
# built and loaded. Without it, `make kind-up IMAGE=...:custom` loads one image
# and deploys another, which with pullPolicy: Never either fails to start or,
# worse, quietly runs whatever stale `:dev` is left on the node.
kind-up: kind-cluster kind-image ## Create the kind cluster and install the chart
	@test -n "$(IMAGE_TAG)" || { echo "IMAGE ('$(IMAGE)') has no tag -- use repository:tag"; exit 1; }
	helm upgrade --install $(RELEASE) $(ROOT)/helm/collab-hub \
		--kube-context kind-$(CLUSTER_NAME) \
		--namespace $(NAMESPACE) --create-namespace \
		--set namespace.create=false \
		-f $(CURDIR)/values/kind.yaml \
		--set api.deployment.image.repository=$(IMAGE_REPO) \
		--set api.deployment.image.tag=$(IMAGE_TAG) \
		--wait --timeout 3m
	@echo ""
	@echo "Installed. Reach it with:  make kind-forward   (then http://127.0.0.1:$(FORWARD_PORT))"

kind-cluster:
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER_NAME) \
		|| kind create cluster --name $(CLUSTER_NAME)

kind-image: ## Build the API image and load it into the kind cluster
	docker build --build-context projectroot=$(ROOT) -t $(IMAGE) $(API_DIR)
	kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)

kind-redeploy: kind-image ## Rebuild the image and roll the Deployment
	$(KUBECTL) -n $(NAMESPACE) rollout restart deployment/collab-hub-api
	$(KUBECTL) -n $(NAMESPACE) rollout status deployment/collab-hub-api --timeout=120s

kind-forward: ## Port-forward the in-cluster API to 127.0.0.1:18080
	$(KUBECTL) -n $(NAMESPACE) port-forward svc/collab-hub-api $(FORWARD_PORT):80

kind-logs: ## Follow the in-cluster API logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deployment/collab-hub-api

# Every other kind-* target pins its context ($(KUBECTL), and --kube-context on
# the helm install). This one shells out to a script that does not: CLUSTER_NAME
# only tells it where to `kind load` the image, while its kubectl and helm calls
# follow whatever the ambient kubeconfig points at. That script creates a
# namespace, secrets, deployments and a Helm release whose values set
# FRAMES_UNSAFE_AUTH_ENABLED=true -- so on a developer who has switched to a
# shared or production context, running it there is the kind of accident worth
# making impossible rather than documenting.
#
# `kind get kubeconfig` writes a kubeconfig holding exactly one cluster, with it
# as the current context. Handing that to the script over KUBECONFIG pins every
# kubectl and helm call it makes, without depending on the script cooperating.
#
# PYTHON_BIN for the same reason: the script's resolve_python() looks for
# api/.venv/bin/python relative to the working directory, and this recipe runs
# in dev/. Left to itself it would miss the virtualenv and fall back to whatever
# bare `python` is on PATH -- which on a uv-managed machine is often absent, and
# otherwise is an interpreter without the smoke clients' httpx. Either way it
# fails at the first Python check, after the cluster, image and chart are up.
kind-smoke: ## Run the repo's kind connector/task smoke suite in its own namespace
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER_NAME) \
		|| { echo "kind cluster '$(CLUSTER_NAME)' does not exist -- run 'make kind-up' first"; exit 1; }
	@kubeconfig=$$(mktemp) && trap 'rm -f "$$kubeconfig"' EXIT && \
		kind get kubeconfig --name $(CLUSTER_NAME) > "$$kubeconfig" && \
		CLUSTER_NAME=$(CLUSTER_NAME) KUBECONFIG="$$kubeconfig" PYTHON_BIN="$(PYTHON)" \
			$(ROOT)/scripts/smoke_local_collab_hub_features_kind.sh

kind-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER_NAME)

# ---------------------------------------------------------------------------
## Checks
# ---------------------------------------------------------------------------

test: ## Run the API test suite
	cd $(API_DIR) && uv run --group test pytest

lint: ## Lint the Helm chart and validate the rendered manifests
	helm lint $(ROOT)/helm/collab-hub
	helm template $(ROOT)/helm/collab-hub | kubeconform -strict -ignore-missing-schemas -
	helm template $(ROOT)/helm/collab-hub -f $(ROOT)/helm/collab-hub/ci/cogs-values.yaml | kubeconform -strict -ignore-missing-schemas -
	$(ROOT)/scripts/chart_render_tests.sh

clean: ## Remove local frame content and caches (leaves docker volumes alone)
	rm -rf $(LOCAL)

$(LOCAL)/frames:
	@mkdir -p $@
