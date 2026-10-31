#!/usr/bin/env bash
# Installs the Helm the workflows run: one pinned version, one pinned sha256.
#
# Pinned because when the latest-release lookup in azure/setup-helm failed it
# fell back to Helm 3, whose schema errors the negative render tests do not
# expect. The version AND its sha256 live here, rather than a checksum fetched
# alongside the tarball from the same server, so a changed download is a CI
# failure and not a changed CI (#205). Every workflow that needs Helm calls
# this script, so an upgrade is a change to the two lines below and nowhere
# else: take the linux-amd64 sha256 from the release notes at
# https://github.com/helm/helm/releases (it also ships as
# helm-<version>-linux-amd64.tar.gz.sha256sum on get.helm.sh).
#
#   scripts/ci/install_helm.sh            # into /usr/local/bin (sudo if needed)
#   scripts/ci/install_helm.sh ~/.local/bin
set -euo pipefail

version=v4.3.1
sha256=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b

dest="${1:-/usr/local/bin}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -sSfLo "$tmp/helm.tar.gz" "https://get.helm.sh/helm-${version}-linux-amd64.tar.gz"
echo "${sha256}  $tmp/helm.tar.gz" | sha256sum -c -
tar -xzf "$tmp/helm.tar.gz" -C "$tmp" linux-amd64/helm

if [ -w "$dest" ]; then
  install -m 0755 "$tmp/linux-amd64/helm" "$dest/helm"
else
  sudo install -m 0755 "$tmp/linux-amd64/helm" "$dest/helm"
fi
"$dest/helm" version --short
