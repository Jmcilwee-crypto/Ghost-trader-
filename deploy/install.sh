#!/usr/bin/env bash
# Sets up Ghost Trader as systemd services on a Linux server.
#
# systemd rather than nohup/tmux on purpose: it restarts a crashed bot on its
# own, brings everything back after a reboot, and captures logs -- which is the
# entire reason for moving off a laptop that sleeps.
#
#   git clone <repo> ~/ghost-trader   (or scp the folder up)
#   cd ~/ghost-trader
#   python3 deploy/preflight.py       # CHECK THIS FIRST
#   bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"
PY="$APP_DIR/.venv/bin/python3"

echo "Installing Ghost Trader from $APP_DIR as $USER_NAME"

if ! command -v python3 >/dev/null; then
  echo "python3 not found. On Ubuntu: sudo apt update && sudo apt install -y python3 python3-venv"
  exit 1
fi

echo "==> creating virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'EOF'
# API keys. Never commit this file.
ODDS_API_KEY=
ALPHAVANTAGE_API_KEY=
EOF
  chmod 600 "$APP_DIR/.env"
  echo "==> created a blank .env -- paste your keys into it before starting"
fi

write_unit() {                 # name, description, exec args
  local name="$1" desc="$2" args="$3"
  sudo tee "/etc/systemd/system/ghost-$name.service" >/dev/null <<EOF
[Unit]
Description=Ghost Trader - $desc
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$PY -u -m $args
# Data sources go down and connections drop; always come back rather than dying.
Restart=always
RestartSec=30
StandardOutput=append:$APP_DIR/data/logs/$name.log
StandardError=append:$APP_DIR/data/logs/$name.log

[Install]
WantedBy=multi-user.target
EOF
}

mkdir -p "$APP_DIR/data/logs"

echo "==> writing systemd units"
write_unit polymarket  "Polymarket whale experiment" "src.experiment"
write_unit crypto      "Crypto spot experiment"      "src.spot_experiment --market crypto"
write_unit commodities "Commodities spot experiment" "src.spot_experiment --market commodities"
write_unit forex       "Forex spot experiment"       "src.spot_experiment --market forex"
write_unit stocks      "Stocks spot experiment"      "src.spot_experiment --market stocks"
write_unit dashboard   "Web dashboard"               "src.dashboard"

sudo systemctl daemon-reload
for s in polymarket crypto commodities forex stocks dashboard; do
  sudo systemctl enable --now "ghost-$s"
done

echo
echo "Done. All six services are running and will restart on reboot."
echo
echo "  status:   systemctl status 'ghost-*' --no-pager"
echo "  logs:     journalctl -u ghost-polymarket -f    (or tail data/logs/*.log)"
echo "  stop one: sudo systemctl stop ghost-crypto"
echo
echo "The dashboard binds to localhost only. Reach it from your laptop with an"
echo "SSH tunnel -- no open ports, nothing exposed to the internet:"
echo "  ssh -N -L 8765:localhost:8765 $USER_NAME@<server-ip>"
echo "then open http://localhost:8765"
