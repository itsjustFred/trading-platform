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

PROJECT_ROOT="$(pwd -P)"

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
Description=Trading Platform Backend (Uvicorn)
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PROJECT_ROOT}/.venv/bin/uvicorn viewer.main:app --host 0.0.0.0 --port 5556
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/trading-daemon.service
[Unit]
Description=Trading Platform Live Runner Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PROJECT_ROOT}/.venv/bin/python -m live_runner.daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF > /etc/systemd/system/trading-frontend.service
[Unit]
Description=Trading Platform Frontend
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_ROOT}/frontend
ExecStart=/usr/bin/env npx serve -s dist -l 3000
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
echo "Next steps:"
echo "  sudo systemctl enable trading-backend.service trading-daemon.service trading-frontend.service"
echo "  sudo systemctl start trading-backend.service trading-daemon.service trading-frontend.service"
echo "  sudo systemctl status trading-backend.service trading-daemon.service trading-frontend.service"
