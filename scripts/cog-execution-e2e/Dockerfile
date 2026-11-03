# E2E image for the Cog-execution kind test: the real package under test, collab_hub_execution, copied
# verbatim from its source, with pixi beside it. It is both the worker pod's image (its init container pulls
# the Cog's artifact with the package's puller, its container serves the package with pixi) and the Op
# driver's (the Job overrides the command). Build context is the repo root.
FROM python:3.14.9-slim
FROM ghcr.io/prefix-dev/pixi:0.83.0-bookworm-slim AS pixi
FROM python:3.14.9-slim
WORKDIR /app
# pip verifies PyPI against the image's Debian trust store (its truststore
# feature, default since pip 24.2), which is fixed only when Debian ships its
# ca-certificates update; a pip version changes none of that. The certifi floor
# is for httpx, which reads certifi's bundle: 2026.10.19 is the first without
# the compromised eMudhra roots.
RUN python -m pip install --no-cache-dir "httpx>=0.28.0" "certifi>=2026.10.19"
# The real package under test, and pixi, which runs a pulled Cog's serve task in the environment its lock pins.
COPY --from=pixi /usr/local/bin/pixi /usr/local/bin/pixi
COPY execution/src/collab_hub_execution /app/collab_hub_execution
COPY scripts/cog-execution-e2e/driver.py /app/e2e/
ENV PYTHONPATH=/app
# pixi keeps the environments it builds, and its downloads, on the worker's cache volume, beside the package.
RUN mkdir -p /opt/pixi && printf 'detached-environments = "/var/cache/collab-hub/pixi-envs"\n' > /opt/pixi/config.toml
ENV PIXI_HOME=/opt/pixi PIXI_CACHE_DIR=/var/cache/collab-hub/pixi-cache
# Worker pods run with the executor's default securityContext (runAsNonRoot), as the hub's image does.
RUN useradd --uid 65532 --user-group --no-create-home nonroot
USER 65532
CMD ["python", "-m", "e2e.driver"]
