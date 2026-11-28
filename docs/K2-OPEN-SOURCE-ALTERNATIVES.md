# Open-source alternatives for the K2 (plan B)

A list of community projects that replace the closed parts of Creality's K2 firmware, for
the day Creality stops shipping updates. Collected 2026-11-27. Every project here is
alpha or beta and describes its own results; none of them has been tested on this printer.

## What is closed on the stock K2

| Part | Stock | Why it matters |
| --- | --- | --- |
| Klipper host | Creality's fork (~v0.12), with compiled modules: `box_wrapper` (CFS), `prtouch_v3` (strain-gauge probe), motor control | Settings can be changed, behaviour cannot |
| Closed-loop motors (X, Y, extruder) | Servo drives on an RS-485 bus, tuned by the closed modules | Stall homing and crash protection depend on them |
| CFS | Closed RS-485 protocol inside `box_wrapper` | No multi-material without Creality's code |
| Touchscreen | `display-server` / `master-server` binaries | Only binary patches, tied to one firmware version |
| Board MCUs | Creality firmware | Every project below keeps it; none reflashes it |

Creality's own "open-source" release ([forum announcement](https://forum.creality.com/t/creality-open-source-3d-printer-firmware-is-here/47218),
Dec 2025) names [CrealityOfficial/K2_Series_Klipper](https://github.com/CrealityOfficial/K2_Series_Klipper)
for the K2 Plus. Users report it lags behind the shipped firmware and still contains
binary-only files. Background on the licence dispute:
[Consumer Rights Wiki](https://consumerrights.wiki/w/Creality_K2_series_GPLv3_violation),
[Creality forum thread](https://forum.creality.com/t/where-are-the-required-to-be-released-sources-for-crealitys-modifications-to-klipper-the-k2/30019).

## Full Klipper (replaces the whole closed stack)

| Project | What it does | K2 Combo (F021) | Status |
| --- | --- | --- | --- |
| [grant0013/K2-OpenKlipper](https://github.com/grant0013/K2-OpenKlipper) ([releases](https://github.com/grant0013/K2-OpenKlipper/releases)) | Upstream Klipper plus open modules for the strain-gauge probe, CFS, closed-loop motors and pressure advance. Ships its own Mainsail edition | Author's own printer: developed and tested on it | Alpha. Installs next to stock with a backup; `uninstall.sh` returns to stock |
| [night-gnida/k2-vanilla-public](https://github.com/night-gnida/k2-vanilla-public) | Upstream klipper3d v0.13.0 with open GPL modules (probe, RS-485 motors, CFS). Keeps stock OS, Moonraker and Fluidd | Targets the F021 base; Combo not named | Runs on hardware; no full print claimed yet. Auto-rolls back on a failed install; `install.sh revert` |

Common warnings from both:
- Never install, switch or uninstall during a print. Supervise the first prints.
- Do not flash stock Klipper MCU firmware.
- A motor protection trip halts the main board until a power cycle.
- Calibrate the CFS cut position before the first filament change (k2-vanilla).
- A Creality OTA update can overwrite parts of the install.

## CFS

| Project | Approach |
| --- | --- |
| [gitstonelabs/creality-cfs-klipper](https://github.com/gitstonelabs/creality-cfs-klipper) | GPL Klipper module, protocol from bus captures; stock or mainline Klipper, or any printer via a USB-RS485 adapter. Beta |
| [Gamecard/k2-plus-cfs-reverse](https://github.com/Gamecard/k2-plus-cfs-reverse) | Protocol docs and a Python reimplementation (K2 Plus). Reports a stock motor-query bug that shuts Klipper down |
| [fake-name/cfs-reverse-engineering](https://github.com/fake-name/cfs-reverse-engineering) | Using the CFS as a general multi-material unit for any Klipper printer |
| [mikeinredding/afc-creality-cfs](https://github.com/mikeinredding/afc-creality-cfs) | Drives the CFS from AFC on mainline Klipper or Kalico. Experimental |

The CFS also comes built in with both full-Klipper projects above.

## Closed-loop servo motors

- [K2-OpenKlipper motor controller notes](https://github.com/grant0013/K2-OpenKlipper/blob/main/docs/MOTOR-CONTROLLER.md):
  Klipper still sends step/dir; setup, stall homing, protection and tuning go over RS-485
  (`/dev/ttyS5`, 230400 baud, shared with the CFS). Parameter writes are RAM only.
  Never use the flash-write opcode or the calibration parameters it lists.
- [Hark Tech reverse-engineering notes](https://harktech.co.uk/projects/k2-reverse-engineering)
  and [grant0013's GitHub](https://github.com/grant0013): the research behind it, including
  the motor parameter map. Related: [k2-adaptive-bedmesh](https://github.com/grant0013/k2-adaptive-bedmesh).
- [ArchWorks: reverse-engineering the K2 Plus locked Klipper fork](https://archworks.co/docs/k2-plus-reverse-engineering/).

## Touchscreen

- [HelixScreen K2 docs](https://helixscreen.org/dev/printers/creality-k2/): an open
  replacement for the stock screen app. Base K2 is listed as untested (K2 Plus and K2 Pro
  are confirmed). Revert by re-enabling the stock `app` service.

## Plan B checklist (do while Creality firmware still works)

1. Keep offline copies of the projects above (clone the repos and download release files),
   in case they disappear.
2. Back up the printer: `/mnt/UDISK/printer_data/config`, the stock `display-server`,
   Creality's Klipper folder with its compiled modules, and the motor and CFS firmware files
   (`mot*_C30-*.bin` and similar) that the stock update script flashes.
3. Note the current firmware version (1.1.8.3) and keep its update image.
4. Before switching, check what this repo's changes need on the new stack:
   - The chamber `M141`/`M191` macros and KAMP are plain Klipper config and can be ported.
   - The display patches are stock-screen only and do not carry over.

## Effect on this repo

The overlay and patches here target stock Creality firmware 1.1.8.3. They are not
compatible with either full-Klipper project as-is.
