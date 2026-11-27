#!/usr/bin/env bash
# Install the pinned opencode CLI from the GitHub release binary, verified
# against the release's own SHA256SUMS - the same pinned-binary pattern as
# gitleaks in secret-scan.yml and actionlint in ci.yml. The npm package is
# deliberately not used: `npm install -g` resolves transitive dependencies
# with no lockfile, which SonarCloud rates a security issue.
#
# Shared by the agent jobs; the quota probe keeps its own inline copy of
# this because it deliberately runs without a checkout (the scripts do not
# exist on other PRs' heads).
#
# Environment: OPENCODE_VERSION (e.g. "2.2.7"), RUNNER_TEMP, GITHUB_PATH.
set -euo pipefail

: "${OPENCODE_VERSION:?OPENCODE_VERSION is required}"
asset="opencode-linux-x64.tar.gz"
base="https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}"
dest="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/opencode"
mkdir -p "$dest"
curl -fsSL --proto '=https' --proto-redir '=https' -o "$dest/$asset" "$base/$asset"
curl -fsSL --proto '=https' --proto-redir '=https' "$base/SHA256SUMS" |
  grep "  ${asset}\$" | (cd "$dest" && sha256sum -c -)
tar -xzf "$dest/$asset" -C "$dest" opencode
chmod +x "$dest/opencode"
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$dest" >> "$GITHUB_PATH"
else
  echo "::warning::GITHUB_PATH is unset; opencode is at $dest/opencode but not on PATH"
fi
"$dest/opencode" --version
