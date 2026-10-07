#!/bin/bash
# Planes Overhead - also send this receiver's data to FlightAware (PiAware).
# readsb stays the decoder; PiAware just forwards what it hears.
#
#   bash enable-flightaware.sh              new FlightAware site
#   bash enable-flightaware.sh FEEDER-ID    keep an existing site and its history
#
# Undo with:  sudo apt-get remove -y piaware
set -e

ID="$1"
REPO_DEB="https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.3_all.deb"

if [ -n "$ID" ] && ! echo "$ID" | grep -Eqi '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
    echo "That doesn't look like a feeder ID (expected 8-4-4-4-12 letters and digits). Nothing changed."
    exit 1
fi

if dpkg -s piaware >/dev/null 2>&1; then
    echo "PiAware is already installed."
else
    echo "Installing PiAware from FlightAware's repository..."
    wget -q -O /tmp/flightaware-apt-repository.deb "$REPO_DEB"
    sudo dpkg -i /tmp/flightaware-apt-repository.deb
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends piaware
fi

if dpkg -s dump1090-fa >/dev/null 2>&1; then
    echo "WARNING: dump1090-fa is installed too. It competes with readsb for the dongle -"
    echo "         remove it with: sudo apt-get remove -y dump1090-fa"
fi

if [ -n "$ID" ]; then
    sudo piaware-config feeder-id "$ID"
fi
sudo systemctl enable piaware >/dev/null 2>&1 || true
sudo systemctl restart piaware
echo "Started PiAware, waiting a minute for it to connect..."
sleep 60

echo
piaware-status || true
echo
if [ -s /var/cache/piaware/feeder_id ]; then
    FID="$(cat /var/cache/piaware/feeder_id)"
    echo "Feeder ID: $FID"
    echo "Claim it (signed in to FlightAware): https://www.flightaware.com/adsb/piaware/claim/$FID"
else
    echo "No feeder ID yet - give it a few minutes, then run: cat /var/cache/piaware/feeder_id"
fi
