#!/bin/bash
# Planes Overhead - turns on readsb's aircraft database so the feed carries
# each aircraft's type (A320, B738...) and registration.
# Safe to run again. Undo with:  bash enable-types.sh undo
set -e

CONF="${CONF:-/etc/default/readsb}"
DB="${DB:-/usr/local/share/tar1090/aircraft.csv.gz}"
DB_URL="https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
FEED="${FEED:-/run/readsb/aircraft.json}"
BACKUP="$CONF.pre-types"

count_types() {
    python3 -c "
import json, sys
a = json.load(open(sys.argv[1]))['aircraft']
print(sum(1 for x in a if x.get('t')), 'of', len(a), 'aircraft in the feed now have a type')" "$FEED"
}

if [ "$1" = "undo" ]; then
    if [ ! -f "$BACKUP" ]; then echo "Nothing to undo."; exit 0; fi
    sudo cp "$BACKUP" "$CONF"
    sudo systemctl restart readsb
    echo "Restored the previous readsb settings."
    exit 0
fi

if [ ! -s "$DB" ]; then
    echo "Downloading the aircraft database..."
    sudo mkdir -p "$(dirname "$DB")"
    sudo wget -q -O "$DB.new" "$DB_URL"
    gzip -t "$DB.new"
    sudo mv "$DB.new" "$DB"
fi
echo "Aircraft database: $DB ($(du -h "$DB" | cut -f1))"

if grep -q -- "--db-file" "$CONF"; then
    echo "readsb is already set to use the database."
else
    if ! grep -q '^JSON_OPTIONS=".*"$' "$CONF"; then
        echo "Could not find the JSON_OPTIONS line in $CONF - nothing changed."
        exit 1
    fi
    [ -f "$BACKUP" ] || sudo cp "$CONF" "$BACKUP"
    sudo sed -i "s|^JSON_OPTIONS=\"\(.*\)\"\$|JSON_OPTIONS=\"\1 --db-file=$DB\"|" "$CONF"
    echo "Updated: $(grep '^JSON_OPTIONS=' "$CONF")"
    sudo systemctl restart readsb
    echo "Restarted readsb, waiting for it to settle..."
    sleep 20
    if ! systemctl is-active --quiet readsb; then
        echo "readsb did not start with the new setting - putting the old one back."
        sudo cp "$BACKUP" "$CONF"
        sudo systemctl restart readsb
        exit 1
    fi
fi

count_types
