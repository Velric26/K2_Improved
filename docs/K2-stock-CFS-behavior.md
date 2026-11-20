# Stock Creality K2 Combo CFS behavior

Observed on November 16, 2026, on a K2 Combo (F021) running Creality firmware **1.1.7.0** with the stock extruder. The two-color test used OrcaSlicer **2.5.1** with the Creality K2 0.4 mm printer preset.

## Send a job and choose the slots in Orca

1. Open the printer preset’s connection settings. Under **Physical Printer → Print Host upload**, select **CrealityPrint** as the **Host Type** and point **Hostname, IP or URL** directly to the stock K2.
2. Assign the two Orca filaments to the model, slice the plate, and click **Print**. Orca opens **Send G-code to printer host** and reads the available CFS slots.
3. Under **Filament Mapping**, choose the physical slot for each filament. The label **1B** means CFS unit 1, slot B. Check the material and color next to each selection.
4. Click **Upload and Print** to apply the mapping and start the job. **Upload** sends the file for later use. **Calibrate before printing** was unchecked in this test.

| Filament in Orca | Tool in the G-code | Physical CFS slot |
| --- | --- | --- |
| 1 · White PETG | `T0` | **1B** |
| 2 · Black PETG | `T1` | **1C** |

Orca sent the slot assignments at job launch. Creality logged `T1A → T1B` and `T1B → T1C` in its internal tool map at 12:03:43, before the SD print began. The uploaded file still used logical commands `T0` and `T1`. The printer used B for `T0` and C for `T1`.


## Cutter strike and release

The following values came from Klipper’s **planned toolhead motion** during a separate successful manual unload of slot C at 11:56. They describe the queued moves. There was no measurement of blade impact force or actual carriage speed at contact. The approach value is the X travel into the cutter from X115 at Y150.

| Phase | Stock motion trace |
| --- | --- |
| Approach | X115 → X10 at **Y150**. Planned speed reached **500 mm/s** during this travel. |
| Strike | X10 → **X−6.70** at Y150. Accelerated and decelerated at **3,000 mm/s²**. Planned peak **223.8 mm/s**, duration **0.149 s**. |
| Release | X−6.70 → **X−1.20** at Y150. Planned peak **128.5 mm/s**, duration **0.086 s**. |
| Return | Creality logged `cut to return OK`. The toolhead then moved away from the cutter along Y, toward Y261. |

The strike endpoint matches the saved stock cutter calibration. The user reported that the stock strike sounded louder and looked faster. The trace confirms the approach, strike and release as distinct moves. OpenKlipper’s settings and the stock trace can be compared directly for endpoints, but an equivalent OpenKlipper trace is needed to compare the planned strike speeds.

## Manual unload and in-print color change

Both successful stock rewinds sent **four `RETRUDE START` commands** to the CFS. The first three were each followed by a **10 mm extruder pull**. The fourth was followed immediately by `RETRUDE FINISH`, with no extra extruder pull. The stock CFS slot mask was `0x04` for C and `0x02` for B.

| Operation | Three extruder pulls | From final rewind request to nozzle sensor clear |
| --- | --- | --- |
| Manual unload of C | **10 mm/s** (`F600`), logged at **220 °C** | **14.5 s** |
| First color change, B → C | **7.5 mm/s** (`F450`), logged at **250 °C** | **17.7 s** |

The saved stock configuration specifies `Tn_retrude_velocity: 600` in mm/min. The two runs show that the manual unload and in-print color change can use different speeds.

For the two-color print, the CFS first loaded B and confirmed it at the nozzle. On the first `T1` at G-code line 4535, Creality cut B, confirmed the cutter returned, and rewound B until the nozzle sensor cleared. It then loaded C, confirmed the load, purged over the chute, restored the **240 °C** print target, and resumed the file. This first swap took about **1 minute 47 seconds** from the first cutter sensor message to the restored temperature target. The job was still printing when this check ended.

## End of print

The stock backup’s `END_PRINT` calls `END_PRINT_Z_SAFE`, `BOX_END`, and `BOX_END_PRINT` before turning the heaters off. This is the stock cleanup path associated with the user’s observation that Creality unloads at print end. The two-color test above establishes the first color change. Its final automatic unload was outside the observation window.
