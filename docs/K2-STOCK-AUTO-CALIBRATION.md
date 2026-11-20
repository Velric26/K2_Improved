# Auto PA and Auto Max Flow on stock K2 firmware

[Stock setup and recovery](K2-STOCK-FIRMWARE-KAMP-AUTO-PA.md) · [Chamber heater](K2-DIY-CHAMBER-HEATER.md)

## Run a calibration in Orca

1. Start the [Windows launcher](../K2-Orca-CFS/Start-K2-AutoPA-Orca.cmd) with the printer address and official Orca executable as described in the [setup instructions](../README.md#set-up-the-orca-companion). It starts the PC companion on `127.0.0.1:8766`. Activate **K2 Calibration** in Orca’s Plugins window on first use.
2. Press **Space** and open **Auto Calibrations**. Choose a saved user filament preset and its matching CFS slot. Clear the plate and purge chute and supervise the calibration.
3. Review the result and choose **Apply to filament preset**. If Orca requests it, assign that preset to the plate first. Save it in Orca to retain the result and re-slice to use it.

The plugin supports a **0.4 mm** nozzle with CFS unit 1. Auto PA accepts **PETG at 230 or 240 °C** and **ABS at 280 °C**. Auto Max Flow and the combined run use **PETG at 240 °C** or **ABS at 280 °C**. The companion uses the stock CFS load, wipe, and purge routines. After a reboot, if stock all-axis homing reports `key22`, home X/Y and then Z on the touchscreen.

If the companion is stopped, launch again with the same arguments and `-CompanionOnly`, then use **Refresh presets and slots** in Orca. The launcher stores new reports under `%LOCALAPPDATA%\K2AutoPA\reports`. [Plugin](../K2-Orca-CFS/k2_calibration_plugin.py) · [Companion](../K2-Stock-KAMP-F021/stock-pa-adapter/orca_auto_pa.py)

## Auto PA targets and scoring

- **Standalone Auto PA** calculates outer- and inner-wall flow from the active process preset’s wall speeds, line widths, and layer height at a **1.00** flow ratio. The selected filament’s Max volumetric speed caps both walls and supplies the target for an automatic wall speed. The smaller flow is the lower target. Equal peaks use different transition sizes.
- The reference **0.20mm @ K2 0.4 - Mod** process uses 0.20 mm layers, a **0.42 mm** outer wall at **200 mm/s**, and a **0.45 mm** inner wall at **300 mm/s**. Before the filament cap, those are **15.08 mm³/s outer** and **24.42 mm³/s inner**.
- **Max Flow then PA** uses the measured Max Flow cutoff as the high PA target and caps the lower wall flow at that cutoff. Without a verified cutoff, the PA step is skipped. A failed PA step leaves the measured Max Flow result available.
- The sweep tests **0.0050** steps, then the leader’s neighbors at **±0.0025** and **±0.0005**. It checks speedup and slowdown separately at both flows, using normalized RMS pressure error across repeats. A candidate needs at least two usable traces per flow and must pass both lower-flow directional limits. Close high-flow candidates are compared on their worse lower-flow score. When those scores differ by at most **0.005** or the measured repeat variation, whichever is larger, the lower PA wins.

The continuous pressure reader timestamps samples on the MCU motion clock. The companion restores the previous PA and motion settings, shuts off the hotend, parks clear of the purge chute, and checks the stock pressure reader. [Scoring code](../K2-Stock-KAMP-F021/stock-pa-adapter/run_direct_pa_refinement.py) · [Printer-side reader](../K2-Stock-KAMP-F021/stock-pa-adapter/k2_stock_pa_direct.py)

### PA reference runs

Flow peaks are in mm³/s. Score pairs list **speedup / slowdown**, with lower scores better. Each linked JSON keeps all candidate scores and raw stroke filenames.

| Date and run | Lower / higher | Selected PA | Lower scores | Higher scores |
| --- | ---: | ---: | --- | --- |
| Nov 18, PETG 1A at 240 °C, [earlier preset ceiling](../K2-Stock-KAMP-F021/stock-pa-adapter/petg_a_short220133_direct_summary.json) | 15.08 / 24.00 | 0.0675 | 0.0417 / 0.0503 | 0.0619 / 0.0770 |
| Nov 20, ABS 1D at 280 °C, [earlier preset ceiling](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_short090616_direct_summary.json) | 15.08 / 32.00 | 0.0400 | 0.0374 / 0.0630 | 0.0778 / 0.0805 |
| Nov 20, ABS 1D at 280 °C, [outer and inner walls](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_short102547_direct_summary.json) | 15.08 / 24.42 | 0.0450 | 0.0582 / 0.0598 | 0.0683 / 0.0793 |
| Nov 20, ABS 1C at 280 °C, [combined run with measured cutoff](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_c_short104326_direct_summary.json) | 15.08 / 35.00 | 0.0500 | 0.0460 / 0.0425 | 0.1188 / 0.1250 |

These runs used the 0.0050 and 0.0025 stages. The **0.0005** stage is in the current companion and has passed software tests. A hardware report for that stage has yet to be recorded. The table lists the PA selected in each report.

## Auto Max Flow

The companion moves the nozzle over the center of the lowered bed. It starts at **15 mm³/s**, estimates the current crossing, and adjusts in **1 mm³/s** steps. Each step is **16 seconds** with an eight-second measurement window after settling. It reuses measured steps and stops after at most eight probes.

The recommendation is the measured upper step with median absolute servo current **≥1.0**, paired with a measured step **1 mm³/s lower** at **<1.0**. The search can check one additional lower step for a transient dip. It requires valid current and motion readings. The tested ceilings are **40 mm³/s for ABS**, **29 for PETG in A/B/D**, and **30 for PETG in C**. The result is a free-air motor-load boundary. A printed tower provides an independent print-quality limit. [Search code](../K2-Stock-KAMP-F021/stock-pa-adapter/run_max_flow.py) · [Orca flow tower](https://github.com/OrcaSlicer/OrcaSlicer_WIKI/blob/main/calibration/volumetric_speed_calib.md)

### Max Flow reference runs

| Run | Flow steps below → at cutoff (mm³/s) | Median absolute current | Printed reference |
| --- | ---: | ---: | --- |
| Nov 19, ABS 1D at 280 °C, [adaptive](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_da37b97ce73c_summary.json) | 31 → 32 | 0.891 → 1.047 | 31 mm³/s, [continuous map](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_map_120912_summary.json) |
| Nov 19, ABS 1C at 280 °C, [adaptive](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_0f9db75bec5e_summary.json) | 32 → 33 | 0.896 → 1.022 | Pending |
| Nov 20, ABS 1C at 280 °C, [combined run](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_a854b076e249_summary.json) | 34 → 35 | 0.972 → 1.054 | Pending |
| Nov 19, PETG 1A at 240 °C, [16-second continuous sweep](../K2-Stock-KAMP-F021/stock-pa-adapter/petg_a_map16_144546_summary.json) | 21 → 22 | 0.967 → 1.016 | 24 mm³/s |

## Restore the adapter

Back up the current stock config and files. Copy `k2_stock_pa_capture.py`, `k2_stock_pa_direct.py`, and `k2_stock_pa_zip.py` from the [adapter directory](../K2-Stock-KAMP-F021/stock-pa-adapter/) to `/usr/share/klipper/klippy/extras/`. Merge the `[k2_stock_pa_capture]` section from the [saved adapter config](../K2-Stock-KAMP-F021/stock-pa-adapter/printer.with-timestamp-adapter.cfg) into the new stock config. Restart while idle and confirm `STOCK_PA_CAPTURE`, `STOCK_PA_DIRECT_PA_STROKE`, and `STOCK_PA_DIRECT_CURRENT_FLOW` are registered before opening Orca. Keep the linked JSON reports and raw captures for comparisons.
