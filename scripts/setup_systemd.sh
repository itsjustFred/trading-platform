#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this script with sudo/root." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_PATH="/etc/systemd/system/trading-viewer.service"
VENV_UVICORN="${PROJECT_ROOT}/.venv/bin/uvicorn"
SERVICE_USER="${SUDO_USER:-root}"

if [[ ! -x "${VENV_UVICORN}" ]]; then
  echo "Missing executable: ${VENV_UVICORN}" >&2
  echo "Create/install the virtualenv first (e.g. .venv/bin/pip install -e '.[all]')." >&2
  exit 1
fi

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=Trading Platform Viewer
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${VENV_UVICORN} viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now trading-viewer.service

echo "Installed and started trading-viewer.service"
echo "Check status with: systemctl status trading-viewer.service --no-pager"
