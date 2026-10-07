# Planes Overhead

Shows the aircraft that have flown within 5 miles of home in the last 24 hours
on a TRMNL X e-ink display, and sends phone alerts for the interesting ones.
Runs on a Raspberry Pi 4 (`navpi4`) with a FlightAware Pro Stick Plus.

## How it fits together

1. **readsb** decodes the radio signal and lists every aircraft it hears.
2. **planelog.py** (service `planelog`) stores positions of aircraft within
   6 miles in `planes.db`, keeping 48 hours.
3. **planealert.py** runs inside the logger and sends a phone notification
   through ntfy for circling aircraft, Southend traffic, and helicopters,
   military and vintage types.
4. **planes_render.py** (timer `planes-render`, every 15 minutes) draws a
   1872x1404 4-bit greyscale PNG and posts it to a TRMNL Webhook Image plugin.
5. **PiAware** forwards the same data to FlightAware.

## Files

| File | Purpose |
|---|---|
| `planelog.py` | Logger service |
| `planealert.py` | Alert rules and ntfy sender (`--test` sends a sample) |
| `planes_render.py` | Draws the picture and pushes it to TRMNL |
| `install.sh` | Installs the logger service and render timer |
| `enable-types.sh` | Turns on readsb's aircraft database (types, registrations) |
| `enable-flightaware.sh` | Installs PiAware to feed FlightAware |
| `health.sh` | Reports processor, memory and storage use |
| `repo.sh` | Saves this folder to the GitHub repo |
| `Update-Pi.ps1` | Runs everything from PowerShell on the PC |
| `config.example.yaml` | Settings template; the real `config.yaml` is not in the repo |

## Day to day (PowerShell, in `C:\flightaware`)

    .\Update-Pi.ps1                  copy files to the Pi, redraw, push to TRMNL
    .\Update-Pi.ps1 -Image           the same, then open the picture on the PC
    .\Update-Pi.ps1 -Install         copy files and restart the services
    .\Update-Pi.ps1 -Logs            latest logger, alert and renderer messages
    .\Update-Pi.ps1 -Stats           summary of the last 24 hours
    .\Update-Pi.ps1 -Health          how busy and how full the Pi is
    .\Update-Pi.ps1 -Commit "note"   copy files and save them to GitHub
    .\Update-Pi.ps1 -Run "..."       run any command in the project folder

`C:\flightaware` on the PC is the master copy. Changes to `planelog.py`,
`planealert.py` or the alert settings need `-Install` to take effect.

## Setting up on a fresh Pi

1. Install readsb and set its location (wiedehopf's `readsb-install.sh`).
2. Copy `config.example.yaml` to `config.yaml` and fill in the TRMNL webhook
   address and an ntfy topic.
3. `.\Update-Pi.ps1 -Install`
4. Optional: `bash enable-types.sh`, `bash enable-flightaware.sh`.

## Not in the repo

`config.yaml` (private addresses), `planes.db` (the log), `planes.png` and
`map_cache.json` (coastline and place names from OpenStreetMap, centred on home).
