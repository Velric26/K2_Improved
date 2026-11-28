#!/bin/sh
set -eu

package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
config=/mnt/UDISK/printer_data/config
extras=/usr/share/klipper/klippy/extras
backup=/mnt/UDISK/k2-stock-kamp-f021-before-overlay
module=$extras/restore_bed_mesh.py

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

assert_idle() {
    python3 - <<'PY'
import json
from urllib.request import urlopen

url = ("http://127.0.0.1:7125/printer/objects/query"
       "?print_stats=state&virtual_sdcard=is_active&pause_resume=is_paused"
       "&webhooks=state&heater_bed=target&extruder=target&heater_generic%20chamber_heater=target")
with urlopen(url, timeout=5) as response:
    status = json.load(response)["result"]["status"]
heaters = ("heater_bed", "extruder", "heater_generic chamber_heater")
if (status["webhooks"]["state"] != "ready"
        or status["print_stats"]["state"] in ("printing", "paused")
        or status["virtual_sdcard"]["is_active"]
        or status["pause_resume"]["is_paused"]
        or any(status.get(h, {}).get("target") for h in heaters)):
    raise SystemExit("The stock printer must be idle with its heaters off.")
PY
}

restore_files() {
    cp -p "$backup/printer.cfg" "$config/.k2-kamp-printer.cfg"
    cp -p "$backup/gcode_macro.cfg" "$config/.k2-kamp-gcode_macro.cfg"
    mv -f "$config/.k2-kamp-printer.cfg" "$config/printer.cfg"
    mv -f "$config/.k2-kamp-gcode_macro.cfg" "$config/gcode_macro.cfg"
    rm -f "$module" "$config/KAMP/KAMP_Settings.cfg" "$config/KAMP/Adaptive_Meshing.cfg"
    rmdir "$config/KAMP" 2>/dev/null || true
}

[ "$(id -u)" = 0 ] || fail "Run this installer as root."
[ "$(fw_printenv version)" = "version=1.1.8.3" ] || fail "This overlay targets the checked F021 stock firmware 1.1.8.3."

case "${1:-apply}" in
    apply)
        assert_idle
        (cd "$config" && sha256sum -c "$package/stock.sha256") || fail "Stock config changed. Rebuild the overlay from the live files."
        (cd "$package/overlay" && sha256sum -c "$package/overlay.sha256") || fail "The overlay archive failed validation."
        [ ! -e "$module" ] && [ ! -e "$config/KAMP" ] || fail "An existing KAMP install needs review."
        mkdir "$backup"
        cp -p "$config/printer.cfg" "$config/gcode_macro.cfg" "$backup/"
        trap 'restore_files' EXIT
        mkdir "$config/KAMP"
        cp "$package/overlay/KAMP/"*.cfg "$config/KAMP/"
        cp "$package/overlay/restore_bed_mesh.py" "$module"
        cp "$package/overlay/printer.cfg" "$config/.k2-kamp-printer.cfg"
        cp "$package/overlay/gcode_macro.cfg" "$config/.k2-kamp-gcode_macro.cfg"
        mv -f "$config/.k2-kamp-printer.cfg" "$config/printer.cfg"
        mv -f "$config/.k2-kamp-gcode_macro.cfg" "$config/gcode_macro.cfg"
        trap - EXIT
        printf '%s\n' "KAMP mesh files installed. Backup: $backup. Restart Klipper to load them."
        ;;
    revert)
        [ -f "$backup/printer.cfg" ] && [ -f "$backup/gcode_macro.cfg" ] || fail "Stock backup is missing."
        (cd "$backup" && sha256sum -c "$package/stock.sha256") || fail "Stock backup failed validation."
        restore_files
        printf '%s\n' "Stock files restored. Restart Klipper."
        ;;
    *) fail "Usage: sh apply.sh [apply|revert]" ;;
esac
