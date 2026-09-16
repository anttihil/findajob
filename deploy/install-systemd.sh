#!/usr/bin/env bash
# Install Find a Job as a systemd service for an existing checkout.
# Run with sudo; the checkout itself remains owned by the selected application user.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo ./deploy/install-systemd.sh --user USER --install-dir /absolute/path [--no-start]

Renders /etc/systemd/system/find-a-job.service and /etc/logrotate.d/find-a-job
for this checkout. The installer never copies or modifies personal configuration.
EOF
}

RUN_USER=""
INSTALL_DIR=""
START_SERVICE=true

while (($#)); do
  case "$1" in
    --user)
      RUN_USER=${2:?--user requires a value}
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR=${2:?--install-dir requires a value}
      shift 2
      ;;
    --no-start)
      START_SERVICE=false
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
if [[ -z $RUN_USER || -z $INSTALL_DIR ]]; then
  usage >&2
  exit 2
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "User does not exist: $RUN_USER" >&2
  exit 1
fi
if [[ ! -d $INSTALL_DIR ]]; then
  echo "Install directory does not exist: $INSTALL_DIR" >&2
  exit 1
fi

INSTALL_DIR=$(realpath "$INSTALL_DIR")
if [[ ! -x "$INSTALL_DIR/.venv/bin/findajob" ]]; then
  echo "Expected $INSTALL_DIR/.venv/bin/findajob; run 'uv sync' first." >&2
  exit 1
fi
if [[ ! -f "$INSTALL_DIR/config.local.yaml" ]]; then
  echo "Expected $INSTALL_DIR/config.local.yaml; copy config.local.example.yaml first." >&2
  exit 1
fi

USER_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
RUN_GROUP=$(id -gn "$RUN_USER")
SERVICE_PATH=/etc/systemd/system/find-a-job.service
LOGROTATE_PATH=/etc/logrotate.d/find-a-job

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=Find a Job Dashboard
Documentation=https://github.com/example/find-a-job
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$INSTALL_DIR/.venv/bin/findajob start --host 127.0.0.1 --port 8010
Restart=on-failure
RestartSec=10s
TimeoutStopSec=15s
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ReadWritePaths=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF

cat >"$LOGROTATE_PATH" <<EOF
$INSTALL_DIR/app.log {
    su $RUN_USER $RUN_GROUP
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

chmod 0644 "$SERVICE_PATH" "$LOGROTATE_PATH"
systemctl daemon-reload
if [[ $START_SERVICE == true ]]; then
  systemctl enable --now find-a-job.service
else
  systemctl enable find-a-job.service
fi

echo "Installed $SERVICE_PATH and $LOGROTATE_PATH for $INSTALL_DIR."
