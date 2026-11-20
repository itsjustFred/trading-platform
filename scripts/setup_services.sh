#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    echo "Root privileges are required to write systemd unit files. Re-running with sudo..."
    exec sudo bash "$0" "$@"
  fi
  echo "Error: run this script as root (or install sudo)." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_USER="${SUDO_USER:-${USER:-ubuntu}}"

if [[ ! -d "${PROJECT_ROOT}/frontend" ]]; then
  echo "Error: frontend directory not found at ${PROJECT_ROOT}/frontend" >&2
  exit 1
fi

if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" || ! -x "${PROJECT_ROOT}/.venv/bin/uvicorn" ]]; then
  echo "Warning: missing expected virtualenv executables in ${PROJECT_ROOT}/.venv/bin" >&2
  echo "Services are still generated; install dependencies before starting services." >&2
fi

cat <<EOF > /etc/systemd/system/trading-backend.service
[Unit]
Description=Trading Platform Backend (FastAPI)
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=PATH=${PROJECT_ROOT}/.venv/bin:/usr/bin:/bin
EnvironmentFile=${PROJECT_ROOT}/.env
ExecStart=${PROJECT_ROOT}/.venv/bin/uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Worst-case graceful shutdown: 30 s drain + 20 s per bounded cleanup phase
# (live_runner.shutdown.WORST_CASE_SHUTDOWN_SECONDS, six phases), so 150 s.
# systemd's default TimeoutStopSec is 90 s, which SIGKILLs the daemon before it
# can finish the drain and report its exit code - the operator's only signal
# that a broker outcome was left unknown (docs/live-deploy-runbook.md,
# "Stopping the daemon"). 180 s leaves 30 s of margin over the worst case;
# tests/test_shutdown_budget_contract.py pins that against the constants.
TIMEOUT_STOP_SEC=180

cat <<EOF > /etc/systemd/system/trading-daemon.service
[Unit]
Description=Trading Live Daemon (Broker + Signal Sources)
After=network.target trading-backend.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=PATH=${PROJECT_ROOT}/.venv/bin:/usr/bin:/bin
EnvironmentFile=${PROJECT_ROOT}/.env
EnvironmentFile=-${PROJECT_ROOT}/.accounts.env
ExecStart=${PROJECT_ROOT}/.venv/bin/python -m live_runner.daemon
Restart=always
RestartSec=5
TimeoutStopSec=${TIMEOUT_STOP_SEC}

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/trading-frontend.service
[Unit]
Description=Trading Platform Frontend
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_ROOT}/frontend
ExecStart=/usr/bin/npx serve -s dist -l 3000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "Systemd unit files created/updated:"
echo "  /etc/systemd/system/trading-backend.service"
echo "  /etc/systemd/system/trading-daemon.service"
echo "  /etc/systemd/system/trading-frontend.service"
echo
echo "Project root: ${PROJECT_ROOT}"
echo "Service user: ${SERVICE_USER}"
echo
echo "Next steps:"
echo "  sudo systemctl enable --now trading-backend.service trading-daemon.service trading-frontend.service"
echo "  sudo systemctl status trading-backend.service trading-daemon.service trading-frontend.service --no-pager"
