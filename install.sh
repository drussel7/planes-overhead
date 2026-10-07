#!/bin/bash
# Planes Overhead - installs the logger service and the 15-minute render timer.
# Safe to run again after updating the files.
# Run as your normal user from the folder holding planelog.py:  bash install.sh
set -e

if [ "$(id -u)" = "0" ]; then
    echo "Run this as your normal user, not with sudo."
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"

echo "Installing Python packages and fonts..."
sudo apt-get install -y python3-yaml python3-pil fonts-dejavu-core

echo "Creating services for user $USER_NAME in $DIR..."
sudo tee /etc/systemd/system/planelog.service >/dev/null <<EOF
[Unit]
Description=Planes Overhead logger
After=readsb.service
Wants=readsb.service

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 -u $DIR/planelog.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/planes-render.service >/dev/null <<EOF
[Unit]
Description=Planes Overhead - render and push to TRMNL
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 -u $DIR/planes_render.py
EOF

sudo tee /etc/systemd/system/planes-render.timer >/dev/null <<EOF
[Unit]
Description=Planes Overhead - render every 15 minutes

[Timer]
OnCalendar=*:02/15
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable planelog.service
sudo systemctl restart planelog.service
sudo systemctl enable --now planes-render.timer

echo
echo "Done."
echo "  Logger:    journalctl -u planelog -f"
echo "  Renderer:  journalctl -u planes-render -n 20"
echo "  Render now: sudo systemctl start planes-render"
