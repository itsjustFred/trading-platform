#!/usr/bin/env bash
# Install the pinned opencode CLI. --ignore-scripts keeps every dependency's
# lifecycle scripts from running; only @opencode/cli's own postinstall (it
# links the platform binary from the optional dependency) is then run
# explicitly. Shared by the agent jobs; the quota probe keeps its own copy
# because it deliberately runs without a checkout.
set -euo pipefail

npm install -g --ignore-scripts "@opencode/cli@${OPENCODE_VERSION}"
(cd "$(npm root -g)/@opencode/cli" && node ./postinstall.mjs)
opencode --version
