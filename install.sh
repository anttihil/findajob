#!/usr/bin/env bash
set -euo pipefail

APP_NAME="findajob"
APP_VERSION="0.1.0"
RELEASE_OWNER="anttihil"
RELEASE_REPO="findajob"
INSTALL_ROOT="${FIND_A_JOB_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME}"
VENV="$INSTALL_ROOT/venv"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
WHEEL_URL="${FIND_A_JOB_WHEEL_URL:-https://github.com/$RELEASE_OWNER/$RELEASE_REPO/releases/latest/download/findajob-${APP_VERSION}-py3-none-any.whl}"

die() { echo "install: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "Python 3.10 or newer is required."
python3 - <<'PY' || die "Python 3.10 or newer is required."
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
PY

download() {
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --silent --show-error "$WHEEL_URL" --output "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget --quiet --show-progress "$WHEEL_URL" --output-document="$1"
  else
    python3 - "$WHEEL_URL" "$1" <<'PY'
import sys
from urllib.request import urlopen
with urlopen(sys.argv[1]) as response, open(sys.argv[2], "wb") as output:
    output.write(response.read())
PY
  fi
}

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
python3 -m venv "$VENV"

wheel=""
if compgen -G "dist/*.whl" >/dev/null 2>&1 && [[ -d findajob/web/frontend/dist ]]; then
  wheel=$(printf '%s\n' dist/*.whl | head -n 1)
else
  wheel="$INSTALL_ROOT/$APP_NAME.whl"
  echo "Downloading the prebuilt Find a Job release..."
  download "$wheel" || die "could not download $WHEEL_URL"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "$wheel"
ln -sfn "$VENV/bin/findajob" "$BIN_DIR/findajob"
"$VENV/bin/findajob" init >/dev/null

env_path="$("$VENV/bin/python" -c 'from findajob.core.paths import ENV_PATH; print(ENV_PATH)')"
if [[ ! -f "$env_path" ]]; then
  cat >"$env_path" <<'EOF'
# Add DEEPSEEK_API_KEY here, or authenticate a supported CLI provider.
# DEEPSEEK_API_KEY=
# FIND_A_JOB_OWNER=
# FIND_A_JOB_PASSWORD=
# FIND_A_JOB_AUTH_TOKEN=
EOF
  chmod 600 "$env_path"
fi
if [[ -t 0 ]] && ! grep -q '^DEEPSEEK_API_KEY=.' "$env_path"; then
  printf "DeepSeek API key (optional; press Enter to configure another provider later): "
  read -r -s deepseek_key
  printf '\n'
  if [[ -n "$deepseek_key" ]]; then
    printf 'DEEPSEEK_API_KEY=%s\n' "$deepseek_key" >>"$env_path"
  fi
fi

pid_path="$("$VENV/bin/python" -c 'from findajob.core.paths import STATE_DIR; print(STATE_DIR + "/findajob.pid")')"
if [[ -f "$pid_path" ]] && kill -0 "$(cat "$pid_path")" 2>/dev/null; then
  echo "Find a Job is already running at http://127.0.0.1:8010"
  exit 0
fi
log_path="$("$VENV/bin/python" -c 'from findajob.core.paths import LOG_PATH; print(LOG_PATH)')"
nohup "$VENV/bin/findajob" start --port 8010 >>"$log_path" 2>&1 &
echo $! >"$pid_path"

echo "Find a Job is installed and running."
echo "Open http://127.0.0.1:8010"
echo "Executable: $BIN_DIR/findajob"
