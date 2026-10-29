# E2E image for the Cog-execution kind test. Runs the real package under test —
# collab_hub_execution is copied verbatim from its source. Serves as both the Cog
# worker runner (default CMD) and the Op driver (the Job overrides the command).
# Build context is the repo root.
FROM python:3.15.0-slim
WORKDIR /app
# pip verifies PyPI against the image's Debian trust store (its truststore
# feature, default since pip 24.2), which is fixed only when Debian ships its
# ca-certificates update; a pip version changes none of that. The certifi floor
# is for httpx, which reads certifi's bundle: 2026.10.19 is the first without
# the compromised eMudhra roots.
RUN python -m pip install --no-cache-dir "httpx>=0.28.0" "certifi>=2026.10.19"
# The real package under test.
COPY execution/src/collab_hub_execution /app/collab_hub_execution
COPY scripts/cog-execution-e2e/runner.py scripts/cog-execution-e2e/driver.py /app/e2e/
ENV PYTHONPATH=/app
# Worker pods run with the executor's default securityContext (runAsNonRoot), as the hub's image does.
RUN useradd --uid 65532 --user-group --no-create-home nonroot
USER 65532
CMD ["python", "-m", "e2e.runner"]
