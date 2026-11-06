# The registry client the pull-through-the-Hub E2E drives: oras, as released.
# Built by run.sh; nothing from this repository goes into the image.
#
# oras is pinned by version AND per-architecture sha256 here, rather than a
# checksum fetched beside the tarball from the same server, so a changed
# download fails the build (as scripts/ci/install_helm.sh does, #207).
# Dependabot does not update these ARGs: to upgrade, set the version and take
# both linux sha256s from oras_<version>_checksums.txt on
# https://github.com/oras-project/oras/releases.
#
# What protects oras against the eMudhra compromise (#285) is the apt
# ca-certificates below (bookworm's 20250419~deb12u2 drops the compromised
# roots), at run time as well as build time: Go reads every file in
# /etc/ssl/certs even when SSL_CERT_FILE is set, as run.sh sets it. oras
# 1.3.6's release binary is built with go1.26.9, before Go's own crypto/x509
# fix (1.26.10), so it does not carry that fix itself.
FROM debian:bookworm-slim
ARG ORAS_VERSION=1.3.6
ARG ORAS_SHA256_AMD64=66fb0ad7206f3f6fa77ed5b552049d364ccf4b50b1684554f731f76302b52ca0
ARG ORAS_SHA256_ARM64=b84269e78dfc2d5329a4aab373da3200de95e6c138338cf6c66c10c0a3f248c1
RUN set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in \
      arm64) arch=arm64; sha256="${ORAS_SHA256_ARM64}" ;; \
      *) arch=amd64; sha256="${ORAS_SHA256_AMD64}" ;; \
    esac; \
    curl -fsSLo /tmp/oras.tar.gz "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_${ORAS_VERSION}_linux_${arch}.tar.gz"; \
    echo "${sha256}  /tmp/oras.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/oras.tar.gz -C /usr/local/bin oras; \
    rm /tmp/oras.tar.gz; \
    oras version
ENV HOME=/tmp/home
