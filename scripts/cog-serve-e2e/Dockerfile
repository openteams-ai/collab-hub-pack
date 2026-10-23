# The registry client the pull-through-the-Hub E2E drives: oras, as released.
# Built by run.sh; nothing from this repository goes into the image.
FROM debian:bookworm-slim
ARG ORAS_VERSION=1.2.3
RUN set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in arm64) arch=arm64 ;; *) arch=amd64 ;; esac; \
    curl -fsSL "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_${ORAS_VERSION}_linux_${arch}.tar.gz" \
      | tar -xz -C /usr/local/bin oras; \
    oras version
ENV HOME=/tmp/home
