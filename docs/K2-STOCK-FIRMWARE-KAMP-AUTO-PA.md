# Stock firmware and local changes on the K2 Combo

**November 20, 2026.** This base K2 Combo is running the Creality F021 stock stack with the stock extruder, CFS, and a 0.4 mm nozzle. The overlay was tested against firmware **1.1.8.3**. The live Klipper build reports `09faed31-dirty`.

## Current setup

| Area | What is installed or verified |
| --- | --- |
| Bed mesh | KAMP-K2 adaptive meshing in `START_PRINT` and a **16×16** full-bed grid over **X/Y 5–255 mm**. The stock purge stays in use. A previous 5×5 mm test job used a 3×3 mesh. |
| Print upload and CFS | Orca uses the **CrealityPrint** host at the printer’s LAN address. The stock service handles CFS slot mapping, cut, load, and unload. The [stock CFS record](K2-stock-CFS-behavior.md) has the popup and motion traces. |
| Device UI | Orca’s **Device UI** field points to Fluidd on port **4408**. The calibration companion connects to Moonraker on port **7125**. |
| Orca Auto Calibrations | Official OrcaSlicer with **K2 Calibration** and the PC companion. The live printer exposes the pressure capture, PA stroke, and servo current commands. [Controls, algorithms, and run comparisons](K2-STOCK-AUTO-CALIBRATION.md) |
| Chamber heater | Stock Klipper controls a separate 50 W heater through `FAN_AC`. The current warmup verification is **0.10 °C in 240 seconds**. [Wiring, controls, test results, and USB expansion](K2-DIY-CHAMBER-HEATER.md) |

KAMP needs Orca’s object definitions before `START_PRINT` so it can size the mesh to the job. The [16×16 config](../K2-Stock-KAMP-F021/printer.16x16.cfg) records the grid settings. The KAMP archive starts at 11×11. After restoring it, verify the live bounds before applying the 16×16 setting. [KAMP-K2 implementation](https://github.com/grant0013/KAMP-K2/blob/ef8fd5d/docs/how-it-works.md)

The live stock start sequence waits for bed temperature with `M190` before mesh probing. The requested **360 s PLA** and **720 s PETG/ABS/ASA** material soak remains a stock follow-up.

## Official updates and restoration

1. **Back up the active printer and presets.** Keep a copy of the clean stock config for the installed release, the current modified config and Klipper extras, the slicer presets, and the calibration reports off the printer. Record the printer and CFS versions. [OpenKlipper stock-return instructions](https://github.com/grant0013/K2-OpenKlipper/blob/main/docs/INSTALL.md#creality-firmware-updates)
2. **Restore clean stock for the installed release and apply the official update.** Use Creality’s touchscreen OTA or documented USB procedure. Check the CFS version afterward because the update can upgrade the CFS too. Save a fresh stock backup before reapplying overlays. [Creality update guide](https://www.creality.com/blog/k2-series-cfs-firmware-upgrade-guide) · [Firmware downloads](https://www.creality.com/download/k2-series-3d-printer)
3. **Reapply the reviewed changes while idle and with the heaters off.** Rebase KAMP on the new stock files and verify adaptive bounds. Reinstall the [calibration adapter](K2-STOCK-AUTO-CALIBRATION.md#restore-the-adapter) and [chamber heater overlay](K2-DIY-CHAMBER-HEATER.md#restore-or-roll-back). Use the new stock backup for rollback on that release.
4. **Verify one path at a time.** Check a small off-center adaptive mesh, Orca CFS mapping, and one calibration result. Check chamber output and shutoff with its AC supply isolated before a powered test.

The [1.1.8.3 KAMP archive](../K2-Stock-KAMP-F021/overlay-f021-1.1.8.3.tar.gz) pins KAMP-K2 to `ef8fd5d87580e4c43f9f09525d46203aa977ecf7` and checks the original file hashes. Its clean files are at `/mnt/UDISK/k2-stock-kamp-f021-before-overlay`. On that release, `sh apply.sh revert` from the extracted package restores the whole saved `printer.cfg` and `gcode_macro.cfg`. Back up later edits before using it. [KAMP-K2 update and rollback notes](https://github.com/grant0013/KAMP-K2/blob/ef8fd5d/docs/troubleshooting.md#after-a-creality-firmware-update)
