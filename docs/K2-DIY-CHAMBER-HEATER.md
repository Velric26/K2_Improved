# Chamber heater and vent expansion on the K2 Combo

[Current stock setup and recovery](K2-STOCK-FIRMWARE-KAMP-AUTO-PA.md) · [Auto Calibration guide](K2-STOCK-AUTO-CALIBRATION.md)

## Installed heater on F021

| Part | Setup |
| --- | --- |
| Heater and blower | **50 W, 110 V AC / 60 Hz** forced-air assembly with its own AC cord and disconnect. The operator reports a **90 °C continuous ambient rating** for the heater assembly. |
| SSR control | **FAN_AC / PA0** supplies the SSR’s DC control input. The meter read about **24 V DC on** and **14.4 mV off**. The heater draws mains power through the SSR from its own cord. |
| Chamber sensor | The existing **PC5** EPCOS 100 kΩ thermistor feeds `heater_generic chamber_heater`. The stock chamber sensor and exhaust reporting remain available. |
| Independent cutoffs | The operator identified a separate hardwired chamber thermostat, a **60 °C heatsink switch** that opens the element circuit while the blower remains powered, and an **84 °C one-shot fuse** in the element circuit. Installed trip points and the enclosure’s sustained temperature limit still need validation. |
| Klipper control | Full on/off with **±1 °C** around the target, zero target on boot, and normal `TURN_OFF_HEATERS` shutdown. `external_ac_supply: true` lets the bed and chamber heater warm together. `monitor_factory_chamber_fan: false` skips the factory tachometer check for this self-contained blower. |
| Heater verification | While more than **8 °C** below target, the chamber must rise **0.10 °C within 240 seconds**. Once inside that band, Klipper adds up each second's shortfall below *target − 8 °C* and faults at **120** (for example, 0.4 °C short for 5 minutes). **CP2559** is this fault. The stock 5 °C band stopped a print on Nov 27: the chamber reached 50 °C for a 55 °C target, then held at 49.5–49.9 °C at full power and tripped after about 5 minutes. Near 50–55 °C this heater gains only about 0.1 °C per minute. |

[Live heater include](../K2-Stock-KAMP-F021/chamber_heater_fan_ac.cfg) · [On/off controller](../K2-Stock-KAMP-F021/k2_chamber_bang_bang.py) · [Creality board service reference](https://www.youtube.com/watch?v=wOV16smiMnU) · [Klipper heater verification](https://www.klipper3d.org/Config_Reference.html#verify_heater)

On stock firmware **1.1.8.3**, the F021 Home page hides its read-only chamber tile when a heater target is set. The target widget is absent on F021, leaving a blank space. A live capture confirmed the blank at **49 °C with a 60 °C target** and showed the live **50 °C** tile again after the target returned to zero. The Home-screen patch keeps the live readout selected for model **1015 / F021**. ARM emulation verified the stock failure, the corrected warmup path, and the unchanged visibility behavior of the other tested models. [Display patch](../K2-Stock-KAMP-F021/patch_chamber_home_tile.py)

The touch patch below includes this Home-screen fix, so it replaces the separate patch. The original stock executable is backed up at `/mnt/UDISK/k2-before-chamber-home-tile-20261121-113026/display-server`.

### Set the chamber from the touchscreen

The chamber tiles on **Home** and **Axis/Temp** work like the nozzle and bed tiles. Tap one, enter a temperature, and the screen sends `M141 S<temperature>`. Verified on the printer on Nov 27 with display build `cdcc2660…` (SHA-256 below).

| Page | Stock F021 | Patched |
| --- | --- | --- |
| Home | Read-only live reading. Hidden while a target is set. | Tap opens the keypad. Shows `44 °C` with no target. While heating, it shows `44 /55°C` with the target in the small grey unit font, and the tile content shifts 16 px left to stay centered. Within 1 °C of the target, it shows only the reading, such as `54 °C`, so the ±1 °C heater cycle does not flicker the display. |
| Axis/Temp | Read-only live reading. | Tap opens the keypad. While a target is set, it switches to the `44 → 50 °C` view that the heated K2 models use. |

The patch gives F021 the tap handlers of the heated models. On Home it attaches them to the live tile, because F021 never creates the separate heating tile. It also removes the F021 exception that kept the Axis/Temp target view hidden, and it rewrites the Home tile's update routine to show the target. The screen redraws that tile when the reading moves by more than 1 °C or the target changes. ARM emulation confirmed the tap setup matches the heated models, that other models are unchanged, and the Home text and positions for each case. [Touch patch](../K2-Stock-KAMP-F021/patch_chamber_touch.py) · [Installer](../K2-Stock-KAMP-F021/install_chamber_touch.py)

The overlay makes `M141` and `M191` drive the heater, as `M140` and `M190` drive the bed:

| Command | Effect |
| --- | --- |
| `M141 S<n>` | Sets the chamber heater target, limited to 0–60 °C, and switches the exhaust off. |
| `M141 S0` | Turns the heater off and leaves the exhaust as it is, so entering 0 on the screen during a PLA print keeps the chamber vented. `END_PRINT` and cancel switch the exhaust off with `CHAMBER_EXHAUST S=0`. |
| `M191 S<n>` | Same as `M141`, then waits until the chamber reaches `n − 1` °C. |
| Bare `M141` / `M191` | No action, so the K2 Mod presets' bare `M191` still skips Orca's own wait. |
| `CHAMBER_EXHAUST S=<n>` | The stock F021 `M141`, which sets the exhaust fan's chamber limit. |

Creality Print files that send `M141 S35` to keep a PLA chamber cool now heat to 35 °C instead. Orca files are unaffected, because the K2 Mod presets pass the chamber target through `START_PRINT`. Fluidd's **Chamber Heater** control and `SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=<temperature>` still work. [Stock macros](../K2-Stock-KAMP-F021/overlay/gcode_macro.cfg)

#### Reinstall after a firmware reset or update

A firmware update or reset restores the stock `display-server` and stock macros. The patch only accepts the stock **1.1.8.3** binary (SHA-256 `0e23e687…25190`). If Creality ships a new release, recheck the addresses before patching.

1. Reapply the stock overlay and the chamber heater as described in [Restore or roll back](#restore-or-roll-back). The [overlay archive](../K2-Stock-KAMP-F021/overlay-f021-1.1.8.3.tar.gz) and the [`overlay`](../K2-Stock-KAMP-F021/overlay) folder contain the same files, including the chamber `M141` / `M191` / `CHAMBER_EXHAUST` macros. Its macros switch the PA0 board fan only while `[output_pin board_dissipation_fan]` exists, so removing that section from `printer.cfg` is the only edit the heater needs. Restart Klipper while idle and confirm `M141` reports *Set the chamber heater target* in Fluidd's command help.
2. Copy `/usr/bin/display-server` from the printer to the PC and build the patched binary:
   `python K2-Stock-KAMP-F021/patch_chamber_touch.py display-server.stock display-server`
   The script refuses any binary except stock 1.1.8.3. The result must hash to `cdcc2660653a65d55a8b00a8d65dffef70aa2b8769b48deb58e59177365f937d`.
3. Put that `display-server` and [`install_chamber_touch.py`](../K2-Stock-KAMP-F021/install_chamber_touch.py) in one folder on the printer, for example `/mnt/UDISK/k2-chamber-touch-stage`. With every heater target at zero, run `python3 install_chamber_touch.py` there.
4. Power-cycle the printer. Set a chamber target on Home and check the tile shows `/target°C` and the Axis/Temp arrow view.

The installer changes nothing unless the printer is idle with every heater target at zero and the current binary is stock or an earlier patched build. It skips the macros when they already contain the chamber `M141`. When they lack it, stage the overlay `gcode_macro.cfg` next to the installer. It installs that file only over a known KAMP overlay version and only if the staged file contains the chamber macros, then restarts Klipper. Stock macros are refused, because the overlay needs the KAMP files that `apply.sh` installs. Replaced files are backed up to `/mnt/UDISK/k2-before-chamber-touch-<time>`. To roll back, copy the backed-up files to their original paths and power-cycle while idle.

For automatic control, enable **Support controlling chamber temperature** on the Orca printer preset and **Activate temperature control** on the starting filament. The K2 Mod presets send the filament’s target and minimum to `START_PRINT` as `CHAMBER_TEMP` and `CHAMBER_MIN`. Their bare `M191` keeps Orca from adding a separate chamber wait before machine startup. The startup macro disables the exhaust during chamber heating, warms the bed alongside the chamber, and waits for the requested minimum once the bed is at temperature and any configured soak has finished. While the chamber warms, the side fan runs at 100 % from homing until 3 °C below the minimum (47 °C for a 50 °C minimum). That circulates the bed's heat: in a test from 27 °C, the chamber reached 47 °C in 28.5 min, against 35 min with the fan at 5 %, and the bed held 100 °C. The fan then stops, so the bed settles for the last few degrees before probing. Creality's probing turns the fan off anyway. In that test, the bed's shape after a 10-minute settle differed from its shape right after the fan by 0.010 mm RMS once tilt was removed, about the probe's own noise. Pass `CHAMBER_FAN_OFF=<°C>` to `START_PRINT` to change the cut-off, or `CHAMBER_FAN_OFF=0` to keep the fan off. The target stays active until the normal end or cancel cleanup. The Kingroom ABS profile drives toward **55 °C** and lets probing proceed at **50 °C** once the bed and nozzle standby targets are reached. ABS adds no timed bed soak. [Verified ABS startup](K2-STOCK-FIRMWARE-KAMP-AUTO-PA.md#verified-abs-startup-reference) · [Orca chamber settings](https://github.com/OrcaSlicer/OrcaSlicer/wiki/printer_basic_information_accessory#support-controlling-chamber-temperature)

For Creality’s stock print preparation, the early `PRINT_TEMP_SET` starts the bed before the file runs. The overlay now requests chamber heat immediately after setting the bed target. It matches a fresh job record from Creality to the file’s explicit `CHAMBER_TEMP` and verifies both bed and nozzle temperatures against the preparation command. If the stock job or request cannot be verified, the file’s `START_PRINT` handles chamber heating. Early preheat stops if the bed target clears or if `START_PRINT` has not taken over within 20 minutes. `START_PRINT` still waits for the chamber minimum before adaptive probing.

## Representative heater tests

| Request and conditions | Sensor result | End of test |
| --- | --- | --- |
| 36 °C, 3 min, bed off | **33.05 → 33.49 °C** | Timed shutoff. The run stayed within the verifier’s 5 °C band. |
| 60 °C, bed off, prior 120 s verifier | **32.47 → about 32.4 °C** | **CP2559** at about **123 s**. Warm airflow was observed and stopped at shutdown. |
| 40 °C, 20 min, bed off, current 240 s verifier | **32.81 → 37.41 °C**, **+0.23 °C/min** average | Timed shutoff with no fault. The verifier reached its band after about **9 min 53 s**. |
| 60 °C chamber and 100 °C bed, 20 min planned | **36.69 → about 42.7 °C** in **10 min 44 s**. The bed peaked at **101.14 °C** and settled near 100 °C. | A PC status poll timed out and the monitor sent an emergency stop after handoff. Klipper reported **key1**. The heater verifier logged no fault. |

Bench commissioning also verified that the SSR LED followed two three-second DC pulses and that the blower delivered steady warm airflow during a ten-second pulse before stopping. Regulation at 40 °C or 60 °C is still unverified in the recorded runs. For a timed handoff test, leave the printer-side heater shutoff queued. Use read-only logging after handoff and record missed polls as gaps. After a fault, the values displayed by Moonraker can include the last targets and outputs. Use Klipper’s shutdown log and observed airflow to confirm the stop.

## Expansion board for vent servos, extra fans, and sensors

Use the K2’s **USB-A flash-drive port** as the host. A **Raspberry Pi Pico H** uses a **USB-A to Micro-USB data cable**. An **SKR Pico** uses a **USB-A to USB-C data cable**, with three switched fan outputs and two thermistor inputs onboard. A Pico H is the simpler DIY choice for several servo signals. [K2 port location](https://cdn.creality.com/ow/official/6bbb36e0-25b7-42b9-bb91-0451320ba2b0.pdf#page=5) · [Pico pinout](https://datasheets.raspberrypi.com/pico/pico-datasheet.pdf) · [SKR Pico hardware](https://github.com/bigtreetech/docs/blob/master/docs/SKR%20Pico.md)

| Accessory | Electrical connection | Klipper section |
| --- | --- | --- |
| Hobby vent servo | One GPIO signal per servo and shared ground. Use a separate regulated supply sized for the servos’ stall current and keep its positive rail separate from USB 5 V. Check that the servo accepts a 3.3 V signal. | `[servo vent]` with `SET_SERVO` |
| Two-wire 24 V fan | One 3.3 V-compatible MOSFET switch per fan on a fused 24 V supply. | `[fan_generic vent_fan]` |
| Thermistor | One ADC input per probe, a pull-up resistor, and the matching sensor curve. The Pico exposes three ADC inputs. | `[temperature_sensor vent_temp]` |

Flash only the add-on with compatible Klipper MCU firmware in USB serial mode. First verify the K2 sees the board as a stable USB serial device, then add `[mcu vent_io]` with its device path and prefix accessory pins with `vent_io:`. USB enumeration and protocol compatibility on this printer are still untested. Secure the cable before printing because loss of a configured auxiliary MCU can stop Klipper. The existing heater cutoffs stay independent of the vent board. [Creality RP2040 build options](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/src/rp2040/Kconfig) · [Klipper additional MCU](https://www.klipper3d.org/Config_Reference.html#mcu-my_extra_mcu) · [Creality MCU shutdown behavior](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/klippy/mcu.py#L1076-L1098)

The unused `FAN2_C` and `RFID` headers and the proposed `BED` / `PC12` heater route remain unverified on this PCB. The documented USB route avoids relying on those pin mappings. [F021 stock config](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/config/F021_CR0CN200400C10/printer.cfg)

### Connections besides USB

| Link | What it would take on this K2 |
| --- | --- |
| **Direct UART** | Linux exposes `/dev/ttyS4` and pinmux maps it to **PB2/PB3 on the Linux host SoC**. A read-only check found no process holding it open. Its physical header or test pads, TX/RX assignments, and voltage are still unverified. If an accessible, compatible UART is confirmed, the Pico can use its GPIO0/GPIO1 serial mode. The link needs TX, RX, a common ground, and matching baud rates. Use level conversion if the logic voltages differ. [Creality RP2040 serial options](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/src/rp2040/Kconfig#L88-L102) |
| **Wi-Fi** | An **ESP32 running ESPHome** can read sensors and run the vent servos and fans independently. The K2 would need a separate control integration to command it over the LAN. Klipper’s standard `[mcu]` configuration supports local serial and CAN, with no native Wi-Fi MCU transport. Keep any temperature safety cutoffs local and hardwired. [ESPHome servo control](https://esphome.io/components/servo/) · [Klipper MCU configuration](https://www.klipper3d.org/Config_Reference.html#mcu) |
| **CAN bus** | Klipper supports a CAN expansion MCU, but this K2 has no active CAN interface in Linux. It would need a compatible host-side CAN interface and transceiver, plus a CAN transceiver on the add-on board. The common USB-to-CAN bridge still uses the K2’s USB port. [Klipper CAN setup](https://www.klipper3d.org/CANBUS.html) |

`ttyS0` is the Linux console, `ttyS2` is the main MCU link, `ttyS3` is the nozzle MCU link, and `ttyS5` runs the CFS RS‑485 bus. The `RFID`, `PC IN`, and fan connector labels do not establish an available UART pinout. Locate and verify the physical `ttyS4` signals before choosing a connector. [F021 MCU links](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/config/F021_CR0CN200400C10/printer.cfg#L14-L22) · [F021 CFS bus](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/config/F021_CR0CN200400C10/box.cfg#L1-L9)

## Restore or roll back

- **Reapply on stock.** Start from a fresh backup. Install `k2_chamber_bang_bang.py` in `/usr/share/klipper/klippy/extras/` and include `chamber_heater_fan_ac.cfg` from `printer.cfg`. Remove the `[output_pin board_dissipation_fan]` section from `printer.cfg` so PA0 has a single owner. The overlay macros skip that fan when the section is gone. Retain the stock exhaust actions and heater shutdown. With heater AC isolated, verify zero startup output, the sensor reading, and the SSR indicator before a powered check. Then reinstall the touchscreen chamber control as described in [Reinstall after a firmware reset or update](#reinstall-after-a-firmware-reset-or-update).
- **Restore the previous verifier window.** The 120-second include is saved on the printer at `config/chamber_heater_fan_ac.cfg.before-240s-warmup-20261120-121642.bak`. Restore it as `chamber_heater_fan_ac.cfg` and restart while idle.
- **Remove the heater remap.** With the heater AC isolated, compare the original files in `/mnt/UDISK/k2-before-fan-ac-heater-20261119_164300` with the current config. Restore the original PA0 output and macro calls with a targeted edit so later KAMP and calibration changes are preserved. The earlier fan-monitor module and include are in `/mnt/UDISK/k2-before-chamber-fan-monitor-20261119-231606`.
