# Auto PA, Adaptive PA, and Auto Max Flow on stock K2 firmware

[Stock setup and recovery](K2-STOCK-FIRMWARE-KAMP-AUTO-PA.md) · [Chamber heater](K2-DIY-CHAMBER-HEATER.md)

## Run a calibration in Orca

1. Start the [Windows launcher](../K2-Orca-CFS/Start-K2-AutoPA-Orca.cmd) with the printer address and official Orca executable as described in the [setup instructions](../README.md#set-up-the-orca-companion). It starts the PC companion on `127.0.0.1:8766`. Activate **K2 Calibration** in Orca’s Plugins window on first use.
2. Press **Space** and open **Auto Calibrations**. The list shows saved user filament presets that Orca marks compatible with the active printer and nozzle. After changing the nozzle in Orca, use **Refresh presets and slots** and choose a preset and its matching CFS slot. Clear the plate and purge chute and supervise the calibration.
3. Review the result and choose **Apply to filament preset**. If Orca requests it, assign that preset to the plate first. Save it in Orca to retain the result and re-slice to use it.

Auto Calibrations reads the active Orca printer preset and requires the K2 to report the same nozzle diameter. It accepts **0.4 mm** and **0.6 mm** with CFS unit 1. Reports record the diameter used. Auto PA accepts **PETG at 230 or 240 °C** and **ABS at 280 °C**. Auto Max Flow and the combined run use **PETG at 240 °C** or **ABS at 280 °C**. The companion uses the stock CFS load, wipe, and purge routines. After a reboot, if stock all-axis homing reports `key22`, home X/Y and then Z on the touchscreen.

If the companion is stopped, launch again with the same arguments and `-CompanionOnly`, then use **Refresh presets and slots** in Orca. The launcher stores new reports under `%LOCALAPPDATA%\K2AutoPA\reports`. [Plugin](../K2-Orca-CFS/k2_calibration_plugin.py) · [Companion](../K2-Stock-KAMP-F021/stock-pa-adapter/orca_auto_pa.py)

## Auto PA targets and scoring

- **Standalone Auto PA** calculates one target from the active process preset’s outer-wall speed, outer-wall width, and layer height at a **1.00** flow ratio. The selected filament’s Max volumetric speed caps the target and supplies it when Orca sets outer-wall speed automatically. For example, a 0.42 mm outer wall at 200 mm/s with a 0.20 mm layer yields **15.08 mm³/s** before the filament cap.
- Standalone scoring uses the worse of the median speedup and slowdown pressure errors across two usable repeats. The lower PA wins when this score is within **0.005** of the best score or within the measured repeat variation, whichever is larger.
- **Max Flow then PA** uses the measured Max Flow cutoff as the high target and caps the lower wall flow at that cutoff. Its two-flow selection checks both lower-flow directional limits before comparing high-flow scores. Without a verified cutoff, the PA step is skipped. A failed PA step leaves the measured Max Flow result available.
- Both modes sweep in **0.0050** steps, then check the leader’s neighbors at **±0.0025** and **±0.0005**.

The continuous pressure reader timestamps samples on the MCU motion clock. The companion restores the previous PA and motion settings, shuts off the hotend, parks clear of the purge chute, and checks the stock pressure reader. [Scoring code](../K2-Stock-KAMP-F021/stock-pa-adapter/run_direct_pa_refinement.py) · [Printer-side reader](../K2-Stock-KAMP-F021/stock-pa-adapter/k2_stock_pa_direct.py)

### Earlier two-flow PA reference runs

Flow peaks are in mm³/s. Score pairs list **speedup / slowdown**, with lower scores better. Each linked JSON keeps all candidate scores and raw stroke filenames.

| Date and run | Lower / higher | Selected PA | Lower scores | Higher scores |
| --- | ---: | ---: | --- | --- |
| Nov 18, PETG 1A at 240 °C, [earlier preset ceiling](../K2-Stock-KAMP-F021/stock-pa-adapter/petg_a_short220133_direct_summary.json) | 15.08 / 24.00 | 0.0675 | 0.0417 / 0.0503 | 0.0619 / 0.0770 |
| Nov 20, ABS 1D at 280 °C, [earlier preset ceiling](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_short090616_direct_summary.json) | 15.08 / 32.00 | 0.0400 | 0.0374 / 0.0630 | 0.0778 / 0.0805 |
| Nov 20, ABS 1D at 280 °C, [outer and inner walls](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_short102547_direct_summary.json) | 15.08 / 24.42 | 0.0450 | 0.0582 / 0.0598 | 0.0683 / 0.0793 |
| Nov 20, ABS 1C at 280 °C, [combined run with measured cutoff](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_c_short104326_direct_summary.json) | 15.08 / 35.00 | 0.0500 | 0.0460 / 0.0425 | 0.1188 / 0.1250 |
| Nov 20, 0.6 mm ABS 1D at 280 °C, [both walls capped by the filament preset](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_short164524_direct_summary.json) | 18.00 / 18.00 | 0.0420 | 0.0865 / 0.0851 | 0.1066 / 0.0900 |

The 0.6 mm run used all three refinement stages, down to **0.0005**. Its two bands used different transition sizes at the same capped peak flow.

## Printed Adaptive PA

Choose **Adaptive PA** in **Auto Calibrations**. The pilot crosses the active inner- and outer-wall flows with both wall accelerations. It calculates flow with the same 1.00 flow ratio as Auto PA and caps wall speed and flow with the chosen filament’s maximum volumetric speed. If the flows coincide, it also measures half the outer-wall flow. If the accelerations coincide, it also measures half the active setting, with a floor of 500 mm/s². The resulting four points are a wall-focused starting grid. Orca’s guide recommends broader sampling across flow and acceleration for a general-purpose model. [Orca Adaptive PA guide](https://github.com/OrcaSlicer/OrcaSlicer/wiki/adaptive_pressure_advance_calib)

The test prints the footprint of Orca’s **PA Tower**, including its rear seam, diagonal sides, and sharp tip. It scales the original 70 mm footprint when longer straights are needed. The first layer prints one priming skirt 2 mm outside a roughly 5 mm attached brim, followed by the two tower walls. This layer runs at 20 mm/s, 500 mm/s², and zero PA. Three unscored base layers follow at 30, 45 and 60 mm/s with 1,000, 1,500 and 2,000 mm/s² (never above the wall settings), so the tower bonds to the bed before the test speeds start. The scored layers use the active profile’s wall geometry and layer height. [Orca PA Tower source](https://github.com/OrcaSlicer/OrcaSlicer/blob/main/src/slic3r/GUI/Plater.cpp#L16205-L16232)

Adaptive PA sets the selected filament’s first-layer bed temperature and starts any enabled chamber heating in parallel. Once the bed reaches its target, it soaks for 180 seconds, homes Z, and probes a fresh KAMP mesh covering the skirt. The chamber keeps warming in the background while the test proceeds. The run report records the targets, soak time, first-layer settings, mesh bounds, and probe counts. Cleanup returns chamber heating started by the test to its previous target.

Each candidate gets its own layer. The report records Z, PA, steady and peak flow, corner flow, and separate pressure errors entering and leaving the corners. The flow comes from Klipper’s recorded nominal extruder motion and the set filament diameter. Compare it with the printed walls to evaluate actual extrusion. The sweep uses **0.0050**, **0.0025**, then **0.0005** steps. A winning PA needs two usable traces and measured candidates on either side.

With four usable points, **Apply to filament preset** uses the recorded steady flows in Orca’s Adaptive PA measurements. The fixed PA remains the fallback. Save the preset in Orca and re-slice to use it. Partial runs still report the available flow readings. The first hardware run will verify the pressure traces against visible corner quality.

## Auto Max Flow

The companion moves the nozzle over the center of the lowered bed. It starts at **15 mm³/s**, estimates the current crossing, and checks adjacent **1 mm³/s** steps. Each step is **16 seconds** with an eight-second measurement window after settling. It reuses measured steps and stops after at most eight probes. If current stays below cutoff and probes are running out, it reserves the last two for the top adjacent pair.

The recommendation is the measured upper step with median absolute servo current **≥1.0**, paired with a measured step **1 mm³/s lower** at **<1.0**. The search can check one additional lower step for a transient dip. It requires valid current and motion readings. The probe ceilings are **40 mm³/s for 0.4 mm ABS**, **48 for 0.6 mm ABS**, **29 for PETG in A/B/D**, and **30 for PETG in C**. The 0.6 mm ABS search uses its measured current rise from 15 to 39 mm³/s for the initial estimate and makes a second estimate near 39 when needed. The higher ceiling awaits a hardware run. The result is a free-air motor-load boundary. A printed tower provides an independent print-quality limit. [Search code](../K2-Stock-KAMP-F021/stock-pa-adapter/run_max_flow.py) · [Orca flow tower](https://github.com/OrcaSlicer/OrcaSlicer_WIKI/blob/main/calibration/volumetric_speed_calib.md)

### Max Flow reference runs

| Run | Flow steps below → at cutoff (mm³/s) | Median absolute current | Printed reference |
| --- | ---: | ---: | --- |
| Nov 19, ABS 1D at 280 °C, [adaptive](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_da37b97ce73c_summary.json) | 31 → 32 | 0.891 → 1.047 | 31 mm³/s, [continuous map](../K2-Stock-KAMP-F021/stock-pa-adapter/abs_d_map_120912_summary.json) |
| Nov 19, ABS 1C at 280 °C, [adaptive](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_0f9db75bec5e_summary.json) | 32 → 33 | 0.896 → 1.022 | Pending |
| Nov 20, ABS 1C at 280 °C, [combined run](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_a854b076e249_summary.json) | 34 → 35 | 0.972 → 1.054 | Pending |
| Nov 20, 0.6 mm ABS 1D at 280 °C, [first run](../K2-Stock-KAMP-F021/stock-pa-adapter/maxflow_146b2e3b1aa5_summary.json) | 15 and 33–39 tested | 0.248 at 15, 0.788 at 39 | No cutoff in the sampled range |
| Nov 19, PETG 1A at 240 °C, [16-second continuous sweep](../K2-Stock-KAMP-F021/stock-pa-adapter/petg_a_map16_144546_summary.json) | 21 → 22 | 0.967 → 1.016 | 24 mm³/s |

## Restore the adapter

Back up the current stock config and files. Copy `k2_stock_pa_capture.py`, `k2_stock_pa_direct.py`, `k2_stock_pa_zip.py`, and `adaptive_pa_motion.py` from the [adapter directory](../K2-Stock-KAMP-F021/stock-pa-adapter/) to `/usr/share/klipper/klippy/extras/`. Merge the `[k2_stock_pa_capture]` section from the [saved adapter config](../K2-Stock-KAMP-F021/stock-pa-adapter/printer.with-timestamp-adapter.cfg) into the new stock config. Restart while idle and confirm `STOCK_PA_CAPTURE`, `STOCK_PA_DIRECT_PA_STROKE`, `STOCK_PA_DIRECT_CURRENT_FLOW`, and `STOCK_PA_DIRECT_PRINT_WALL` are registered before opening Orca. Keep the linked JSON reports and raw captures for comparisons.
