#!/bin/bash
# Planes Overhead - how hard the Pi is working and how full it is.
# Changes nothing; safe to run at any time.
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== Processor =="
echo "Running for $(uptime -p 2>/dev/null | sed 's/^up //')"
echo "Load average over 1, 5 and 15 minutes: $(cut -d' ' -f1-3 /proc/loadavg)  ($(nproc) cores, so $(nproc).00 = fully busy)"
if command -v vcgencmd >/dev/null 2>&1; then
    echo "Temperature: $(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)   Power/heat warnings: $(vcgencmd get_throttled 2>/dev/null | cut -d= -f2) (0x0 = none)"
elif [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    awk '{printf "Temperature: %.1f C\n", $1/1000}' /sys/class/thermal/thermal_zone0/temp
fi

echo
echo "== Memory =="
free -h | sed -n '1,3p'

echo
echo "== Services (average since each was started) =="
up=$(cut -d' ' -f1 /proc/uptime)
for unit in readsb planelog piaware lighttpd; do
    state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)
    if [ "$state" != "active" ]; then
        printf "%-10s %s\n" "$unit" "${state:-not installed}"
        continue
    fi
    cpu=$(systemctl show -p CPUUsageNSec --value "$unit")
    mem=$(systemctl show -p MemoryCurrent --value "$unit")
    began=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$unit")
    awk -v u="$unit" -v c="$cpu" -v m="$mem" -v b="$began" -v up="$up" 'BEGIN {
        secs = up - b / 1e6; if (secs < 1) secs = 1
        printf "%-10s %5.1f%% of one core, %4.0f MB memory, running %.1f days\n", u, c / 1e9 / secs * 100, m / 1048576, secs / 86400 }'
done
echo "Renderer: last three runs"
journalctl -u planes-render --no-pager -n 40 2>/dev/null | grep "Consumed" | tail -3 | sed 's/.*Consumed/  used/'

echo
echo "== Busiest programs right now =="
top -b -n 1 -o %CPU 2>/dev/null | sed -n '7,13p'

echo
echo "== Storage =="
df -h / | awk 'NR==1 {print} NR==2 {print; print "The card is " $5 " full, with " $4 " free."}'
echo
echo "Planes Overhead folder: $(du -sh "$DIR" 2>/dev/null | cut -f1)"
ls -l "$DIR"/planes.db* 2>/dev/null | awk '{printf "  %-22s %8.1f MB\n", $NF, $5 / 1048576}' | sed "s|$DIR/||"
python3 - "$DIR/planes.db" <<'PY' 2>/dev/null
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], timeout=10)
rows, first = db.execute("SELECT COUNT(*), MIN(ts) FROM positions").fetchone()
if rows:
    print(f"  {rows:,} positions stored, oldest {(time.time() - first) / 3600:.0f} hours ago (anything over 48 hours is deleted)")
PY
echo "System logs: $(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[KMG]' | head -1)"
for extra in /var/globe_history /var/lib/collectd /usr/local/share/tar1090 /var/cache/piaware; do
    [ -e "$extra" ] && echo "$extra: $(du -sh "$extra" 2>/dev/null | cut -f1)"
done
echo
echo "Largest folders in your home directory:"
du -xh --max-depth=1 "$HOME" 2>/dev/null | sort -rh | sed -n '2,7p' | sed "s|$HOME/|  |"
