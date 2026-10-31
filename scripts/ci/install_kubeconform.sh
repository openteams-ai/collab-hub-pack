#!/usr/bin/env bash
# Installs the kubeconform lint.yaml validates the rendered chart with: one
# pinned version, one pinned sha256, the same shape as install_helm.sh.
#
# The step used to fetch releases/latest with no version and no checksum, so
# whatever the release server served is what CI ran. On upgrade, take the
# linux-amd64 line from the CHECKSUMS asset of the release at
# https://github.com/yannh/kubeconform/releases and update both lines below.
#
#   scripts/ci/install_kubeconform.sh            # into /usr/local/bin (sudo if needed)
#   scripts/ci/install_kubeconform.sh ~/.local/bin
set -euo pipefail

version=v0.8.0
sha256=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883

dest="${1:-/usr/local/bin}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -sSfLo "$tmp/kubeconform.tar.gz" \
  "https://github.com/yannh/kubeconform/releases/download/${version}/kubeconform-linux-amd64.tar.gz"
echo "${sha256}  $tmp/kubeconform.tar.gz" | sha256sum -c -
tar -xzf "$tmp/kubeconform.tar.gz" -C "$tmp" kubeconform

if [ -w "$dest" ]; then
  install -m 0755 "$tmp/kubeconform" "$dest/kubeconform"
else
  sudo install -m 0755 "$tmp/kubeconform" "$dest/kubeconform"
fi
"$dest/kubeconform" -v
