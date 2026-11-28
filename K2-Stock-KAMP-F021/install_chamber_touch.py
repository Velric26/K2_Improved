"""Runs on the K2. Installs the touch-enabled chamber display and, optionally, the M141 heater macros.

Stage next to this script:
  display-server   built with patch_chamber_touch.py from the stock 1.1.8.3 binary (required)
  gcode_macro.cfg  the repository overlay's file, only when the printer's macros still lack
                   the chamber M141 (optional)

Changes nothing unless the printer is idle with every heater target at zero and each
file on the printer is a known version. Backs replaced files up first.
"""
import hashlib
import json
import os
import shutil
import sys
import time
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
DISPLAY = "/usr/bin/display-server"
MACROS = "/mnt/UDISK/printer_data/config/gcode_macro.cfg"
FILES = (
    # target, staged file, sha before (current), sha after
    (DISPLAY, "display-server",
     ("0e23e687276a989c286d25d9cf137a1c08ca1643f294a77afbda4e6d8ba25190",   # stock 1.1.8.3
      "5f4ffad69487087c6c855d69b7c330e91245ae1ebb14ea4208f47a7a58d02787",   # Home live-tile patch
      "ea20db81e6040d51c697d1d43dd3201338aebdbcdda989c1f6902810700f7086",   # first touch build
      "0a483fdb356943c926ce0cd484b224e0579b5cb1e860069be2853e99b2896f6c",   # current/target in one label
      "14071bd3313430d8a5d610a984ba26f68edd8689b2c24183b56805fdd8dd95ef"),  # small /target, not centered
     "cdcc2660653a65d55a8b00a8d65dffef70aa2b8769b48deb58e59177365f937d"),
    # The staged macros are checked for the chamber sections instead of one exact hash, so the
    # repository overlay (with or without local edits) can be staged.
    (MACROS, "gcode_macro.cfg",
     # Stock macros are not listed: the overlay needs the KAMP files that apply.sh installs.
     ("1fe1cc28d1941c2a56a1ba3d70e43341171500de5e4298fe63da57e66518bcae",   # first KAMP overlay archive
      "ce274ba2682ab336bf00c005f6638b367795718ae2108efb79ce35445528576d"),  # KAMP with heater, no chamber M141
     None),
)
API = "http://127.0.0.1:7125"


def digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 18), b""):
            sha.update(block)
    return sha.hexdigest()


def query(fields):
    with urlopen(API + "/printer/objects/query?" + fields, timeout=5) as response:
        return json.load(response)["result"]["status"]


def idle_problem():
    try:
        status = query("webhooks=state&print_stats=state&virtual_sdcard=is_active&pause_resume=is_paused"
                       "&extruder=target&heater_bed=target&heater_generic%20chamber_heater=target")
    except (OSError, ValueError, KeyError) as error:
        return "Klipper is not ready (%s)" % error
    if status["webhooks"]["state"] != "ready":
        return "Klipper is not ready"
    if status["print_stats"]["state"] in ("printing", "paused") or status["virtual_sdcard"]["is_active"] \
            or status["pause_resume"]["is_paused"]:
        return "a print is %s" % status["print_stats"]["state"]
    hot = [name for name in ("extruder", "heater_bed", "heater_generic chamber_heater")
           if status.get(name, {}).get("target")]
    return "heater targets are set: " + ", ".join(hot) if hot else None


def macros_installed(path=None):
    with open(path or MACROS, encoding="utf-8", errors="replace") as source:
        text = source.read()
    return "[gcode_macro CHAMBER_EXHAUST]" in text and "HEATER=chamber_heater TARGET={target}" in text


def main():
    problem = idle_problem()
    if problem:
        sys.exit("Not idle (%s). Nothing was changed." % problem)
    plan = []
    for target, staged, before, after in FILES:
        if target == MACROS and macros_installed():
            print("Chamber M141 macros already present:", target)
            continue
        staged = os.path.join(HERE, staged)
        if target == MACROS and not os.path.exists(staged):
            sys.exit("The printer macros lack the chamber M141. Install the overlay gcode_macro.cfg first. "
                     "Nothing was changed.")
        if after is None:
            if not macros_installed(staged):
                sys.exit("Staged %s lacks the chamber M141 macros. Nothing was changed." % os.path.basename(staged))
            after = digest(staged)
        elif digest(staged) != after:
            sys.exit("Staged %s is damaged. Nothing was changed." % os.path.basename(staged))
        current = digest(target)
        if current == after:
            print("Already installed:", target)
        elif current in before:
            plan.append((target, staged))
        else:
            sys.exit("%s is not the expected version (%s). Nothing was changed." % (target, current))
    if plan:
        backup = "/mnt/UDISK/k2-before-chamber-touch-" + time.strftime("%Y%m%d-%H%M%S")
        os.mkdir(backup, 0o700)
        for target, staged in plan:
            shutil.copy2(target, os.path.join(backup, os.path.basename(target)))
        print("Backup:", backup)
        for target, staged in plan:
            temporary = target + ".chamber-touch.new"
            shutil.copyfile(staged, temporary)
            shutil.copymode(target, temporary)
            with open(temporary, "rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            print("Installed:", target)
        os.sync()
    if not any(target == MACROS for target, _ in plan):
        print("The macros are unchanged; Klipper was not restarted.")
        print("The new screen loads on the next printer power cycle.")
        return
    # Load the new macros. Re-check right before restarting.
    problem = idle_problem()
    if problem:
        sys.exit("Files are installed, but Klipper was not restarted (%s). Restart it when idle." % problem)
    urlopen(Request(API + "/printer/restart", data=b"", method="POST"), timeout=10).read()
    print("Klipper restart sent. Waiting for it to come back...")
    for _ in range(90):
        time.sleep(1)
        try:
            if query("webhooks=state")["webhooks"]["state"] == "ready":
                break
        except OSError:
            pass
    else:
        sys.exit("Klipper did not report ready within 90 s. Check Fluidd.")
    with urlopen(API + "/printer/gcode/help", timeout=5) as response:
        commands = json.load(response)["result"]
    if "CHAMBER_EXHAUST" not in commands or "chamber heater" not in commands.get("M141", "").lower():
        sys.exit("Klipper is ready but the new M141 macro is not loaded.")
    print("Klipper ready with the new M141/M191 macros.")
    print("The touch-enabled screen loads on the next printer power cycle.")


if __name__ == "__main__":
    main()
