#!/usr/bin/env bash
# Install the pinned opencode CLI from the GitHub release binary, verified
# against the sha256 the caller pins next to OPENCODE_VERSION - the same
# pinned-binary pattern as gitleaks in secret-scan.yml and actionlint in
# ci.yml. The release's own SHA256SUMS is deliberately not fetched: it only
# proves the download matches what the release published, not that the
# release is the one we pinned, and this step runs with NVIDIA_API_KEY in
# reach. The npm package is deliberately not used either: `npm install -g`
# resolves transitive dependencies with no lockfile, which SonarCloud rates
# a security issue.
#
# Shared by the agent jobs; the quota probe keeps its own inline copy of
# this because it deliberately runs without a checkout (the scripts do not
# exist on other PRs' heads). The contract test asserts the two stay
# identical.
#
# Environment: OPENCODE_VERSION, OPENCODE_SHA256, RUNNER_TEMP, GITHUB_PATH.
set -euo pipefail

: "${OPENCODE_VERSION:?OPENCODE_VERSION is required}"
: "${OPENCODE_SHA256:?OPENCODE_SHA256 is required}"
asset="opencode-linux-x64.tar.gz"
base="https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}"
dest="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/opencode"
mkdir -p "$dest"
curl -fsSL --proto '=https' --proto-redir '=https' -o "$dest/$asset" "$base/$asset"
echo "${OPENCODE_SHA256}  ${asset}" | (cd "$dest" && sha256sum -c -)
tar -xzf "$dest/$asset" -C "$dest" opencode
chmod +x "$dest/opencode"
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$dest" >> "$GITHUB_PATH"
else
  echo "::warning::GITHUB_PATH is unset; opencode is at $dest/opencode but not on PATH"
fi
"$dest/opencode" --version
