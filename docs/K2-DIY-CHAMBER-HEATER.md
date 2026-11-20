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
| Warmup verification | A **0.10 °C rise within 240 seconds** while below target by more than **5 °C**. The cumulative error limit remains **120**. **CP2559** is the chamber warmup fault. |

[Live heater include](../K2-Stock-KAMP-F021/chamber_heater_fan_ac.cfg) · [On/off controller](../K2-Stock-KAMP-F021/k2_chamber_bang_bang.py) · [Creality board service reference](https://www.youtube.com/watch?v=wOV16smiMnU) · [Klipper heater verification](https://www.klipper3d.org/Config_Reference.html#verify_heater)

On stock firmware **1.1.8.3**, the F021 Home page switches from its read-only chamber tile to a target layout during warmup. F021 skips creation of that target widget, leaving a blank space. A live capture confirmed the blank at **49 °C with a 60 °C target** and showed the read-only **50 °C** tile again after the target returned to zero. Offline emulation verified a display-only fix that keeps selecting the live readout for model **1015 / F021** during warmup. The stock screen remains installed.

An earlier Pro-style keypad trial sent `M141` to the exhaust fan and left the heater target at zero. Stock `M191` calls `M141` with its default arguments. Use the **chamber heater** control in Fluidd on port **4408** or `SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=<temperature>`. Set `TARGET=0` to clear the chamber heater request. Orca’s printer preset exposes chamber-temperature settings. Automatic heater control from slicer commands still needs a firmware route. [Stock macros](../K2-Stock-KAMP-F021/overlay/gcode_macro.cfg)

## Representative heater tests

| Request and conditions | Sensor result | End of test |
| --- | --- | --- |
| 36 °C, 3 min, bed off | **33.05 → 33.49 °C** | Timed shutoff. The run stayed within the verifier’s 5 °C band. |
| 60 °C, bed off, prior 120 s verifier | **32.47 → about 32.4 °C** | **CP2559** at about **123 s**. Warm airflow was observed and stopped at shutdown. |
| 40 °C, 20 min, bed off, current 240 s verifier | **32.81 → 37.41 °C**, **+0.23 °C/min** average | Timed shutoff with no fault. The verifier reached its band after about **9 min 53 s**. |
| 60 °C chamber and 100 °C bed, 20 min planned | **36.69 → about 42.7 °C** in **10 min 44 s**. The bed peaked at **101.14 °C** and settled near 100 °C. | A PC status poll timed out and the monitor sent an emergency stop after handoff. Klipper reported **key1**. The heater verifier logged no fault. |

Bench commissioning also verified that the SSR LED followed two three-second DC pulses and that the blower delivered steady warm airflow during a ten-second pulse before stopping. Regulation at 40 °C or 60 °C is still unverified in the recorded runs. For a timed handoff test, leave the printer-side heater shutoff queued. Use read-only logging after handoff and record missed polls as gaps. After a fault, the values displayed by Moonraker can include the last targets and outputs. Use Klipper’s shutdown log and observed airflow to confirm the stop.

## USB add-on for vent servos, extra fans, and sensors

Use the K2’s **USB-A flash-drive port** as the host. A **Raspberry Pi Pico H** uses a **USB-A to Micro-USB data cable**. An **SKR Pico** uses a **USB-A to USB-C data cable**, with three switched fan outputs and two thermistor inputs onboard. A Pico H is the simpler DIY choice for several servo signals. [K2 port location](https://cdn.creality.com/ow/official/6bbb36e0-25b7-42b9-bb91-0451320ba2b0.pdf#page=5) · [Pico pinout](https://datasheets.raspberrypi.com/pico/pico-datasheet.pdf) · [SKR Pico hardware](https://github.com/bigtreetech/docs/blob/master/docs/SKR%20Pico.md)

| Accessory | Electrical connection | Klipper section |
| --- | --- | --- |
| Hobby vent servo | One GPIO signal per servo and shared ground. Use a separate regulated supply sized for the servos’ stall current and keep its positive rail separate from USB 5 V. Check that the servo accepts a 3.3 V signal. | `[servo vent]` with `SET_SERVO` |
| Two-wire 24 V fan | One 3.3 V-compatible MOSFET switch per fan on a fused 24 V supply. | `[fan_generic vent_fan]` |
| Thermistor | One ADC input per probe, a pull-up resistor, and the matching sensor curve. The Pico exposes three ADC inputs. | `[temperature_sensor vent_temp]` |

Flash only the add-on with compatible Klipper MCU firmware in USB serial mode. First verify the K2 sees the board as a stable USB serial device, then add `[mcu vent_io]` with its device path and prefix accessory pins with `vent_io:`. USB enumeration and protocol compatibility on this printer are still untested. Secure the cable before printing because loss of a configured auxiliary MCU can stop Klipper. The existing heater cutoffs stay independent of the vent board. [Creality RP2040 build options](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/src/rp2040/Kconfig) · [Klipper additional MCU](https://www.klipper3d.org/Config_Reference.html#mcu-my_extra_mcu) · [Creality MCU shutdown behavior](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/klippy/mcu.py#L1076-L1098)

The unused `FAN2_C` and `RFID` headers and the proposed `BED` / `PC12` heater route remain unverified on this PCB. The documented USB route avoids relying on those pin mappings. [F021 stock config](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/main/config/F021_CR0CN200400C10/printer.cfg)

## Restore or roll back

- **Reapply on stock.** Start from a fresh backup. Install `k2_chamber_bang_bang.py` in `/usr/share/klipper/klippy/extras/` and include `chamber_heater_fan_ac.cfg` from `printer.cfg`. Remove the old `board_dissipation_fan` pin definition and its calls in `START_PRINT` and `END_PRINT` so PA0 has a single owner. Retain the stock exhaust actions and heater shutdown. With heater AC isolated, verify zero startup output, the sensor reading, and the SSR indicator before a powered check.
- **Restore the previous verifier window.** The 120-second include is saved on the printer at `config/chamber_heater_fan_ac.cfg.before-240s-warmup-20261120-121642.bak`. Restore it as `chamber_heater_fan_ac.cfg` and restart while idle.
- **Remove the heater remap.** With the heater AC isolated, compare the original files in `/mnt/UDISK/k2-before-fan-ac-heater-20261119_164300` with the current config. Restore the original PA0 output and macro calls with a targeted edit so later KAMP and calibration changes are preserved. The earlier fan-monitor module and include are in `/mnt/UDISK/k2-before-chamber-fan-monitor-20261119-231606`.
