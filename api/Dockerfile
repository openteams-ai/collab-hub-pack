FROM python:3.14-slim-trixie AS python
FROM ghcr.io/astral-sh/uv:0.12.7 AS uv
FROM node:24-bookworm-slim AS node

# The admin panel is a built bundle, so it is built here rather than committed.
# `npm ci --ignore-scripts` installs exactly what package-lock.json pins and
# runs no package lifecycle script: the panel's dependency set needs none, and
# a build stage that executes arbitrary install hooks is the step a compromised
# transitive dependency would aim at.
FROM node AS admin-ui

WORKDIR /var/admin-ui
COPY admin-ui/package.json admin-ui/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY admin-ui/ ./
RUN npm run build


FROM python AS builder

WORKDIR /var/collab-hub-api

COPY pyproject.toml uv.lock ./
# The execution package (the run API's Track and intents, and the run controller's
# entrypoint, collab-hub-run-controller) is a path dependency at ../execution: it
# comes from the repository root through the projectroot context, and is installed
# into the virtual environment as a package, not linked to this source.
COPY --from=projectroot execution/pyproject.toml /var/execution/pyproject.toml
COPY --from=projectroot execution/src /var/execution/src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync \
    --no-progress --link-mode=copy --compile-bytecode \
    --frozen --python=$(which python) --no-default-groups --no-install-project --no-editable

COPY src ./src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=README.md,target=./README.md \
    --mount=type=bind,from=projectroot,source=LICENSE,target=./LICENSE \
    uv pip install \
    --no-progress --link-mode=copy --compile-bytecode \
    --no-deps .

FROM python AS runtime

RUN groupadd --gid 1000 nonroot && \
    useradd --shell "$(which bash)" --uid 1000 --gid 1000 nonroot
USER nonroot

WORKDIR /var/collab-hub-api
COPY --from=builder --chown=nonroot:nonroot /var/collab-hub-api/.venv /var/collab-hub-api/.venv
ENV PATH="/var/collab-hub-api/.venv/bin:${PATH}"

# The built panel, and the setting that makes the app serve it. An image
# without this directory mounts no panel routes at all, which is what a
# deployment that predates the panel already does.
COPY --from=admin-ui --chown=nonroot:nonroot /var/admin-ui/dist /var/collab-hub-api/admin-ui-dist
ENV COLLAB_HUB_API__WEB__ADMIN_UI_DIST=/var/collab-hub-api/admin-ui-dist

ENV COLLAB_HUB_API__SERVER__HOSTNAME=0.0.0.0

# The image ships pre-compiled bytecode (uv --compile-bytecode above), so
# nothing needs to write more at runtime. It matters for the read-only identity
# inventory (`python -m collab_hub_api.identity_inventory`, issue #65), which
# claims to write nothing to the filesystem: without this, importing it in a
# container whose site-packages happens to be writable would create __pycache__
# directories and make that claim false.
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/bin/bash"]
CMD ["-c", "python -m collab_hub_api"]
