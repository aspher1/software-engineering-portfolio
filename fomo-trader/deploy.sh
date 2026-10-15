#!/bin/bash
# Deploy the FOMO trader + phone dashboard on a fresh Ubuntu VPS.
#
#   1. Copy this folder to the VPS:   scp -r fomo-trader root@<vps-ip>:/root/
#   2. SSH in and run:                bash /root/fomo-trader/deploy.sh
#   3. Open the dashboard URL it prints on your phone.
#
# The bot does NOT auto-start (you start it from your phone).
# The dashboard DOES auto-start on boot.
set -e
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then echo "run as root (or with sudo)"; exit 1; fi

echo "== system deps =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv curl

echo "== python env =="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q solders requests

[ -f config.json ] || cp config.example.json config.json

echo "== dashboard token =="
.venv/bin/python - <<'EOF'
import json, secrets
cfg = json.load(open("config.json"))
if not cfg.get("dashboard_token"):
    cfg["dashboard_token"] = secrets.token_urlsafe(24)
    json.dump(cfg, open("config.json", "w"), indent=1)
    print("generated new dashboard token")
EOF

echo "== systemd services =="
DIR="$(pwd)"
cat > /etc/systemd/system/fomo-trader.service <<EOF
[Unit]
Description=FOMO Trader bot
After=network-online.target
[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/fomo_trader.py --config $DIR/config.json
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=42
# exit 42 = kill switch engaged -> do NOT restart
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/fomo-dashboard.service <<EOF
[Unit]
Description=FOMO Trader phone dashboard
After=network-online.target
[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/dashboard.py --config $DIR/config.json --port 8080
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fomo-trader.service >/dev/null 2>&1 || true   # installed, NOT started
systemctl enable --now fomo-dashboard.service
ufw allow 8080/tcp >/dev/null 2>&1 || true

IP=$(curl -s --max-time 5 ifconfig.me || echo "<vps-ip>")
TOKEN=$(.venv/bin/python -c "import json; print(json.load(open('config.json'))['dashboard_token'])")
echo ""
echo "=================================================="
echo " Dashboard: http://$IP:8080/?token=$TOKEN"
echo ""
echo " Open that URL on your phone."
echo " Next steps (do these before starting the bot):"
echo "   1. Put your trading keypair at ~/.config/solana/id.json"
echo "      (or set keypair_path in config.json)"
echo "   2. Fund that wallet with SOL"
echo "   3. In config.json: tune buy size, then start from the phone"
echo "      (dry_run=true until you trust it)"
echo " The token in the URL is the only login - keep it private."
echo "=================================================="
