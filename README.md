# K2 Improved

Stock-firmware improvements for the **Creality K2 Combo (F021)**, tested against **1.1.8.3**. The project includes KAMP adaptive meshing, an OrcaSlicer calibration plugin, and an optional chamber heater overlay. Creality’s stock firmware handles printing and the CFS.

- [Stock firmware, mesh, and update recovery](docs/K2-STOCK-FIRMWARE-KAMP-AUTO-PA.md)
- [Orca Auto PA and Auto Max Flow](docs/K2-STOCK-AUTO-CALIBRATION.md)
- [Chamber heater and USB vent expansion](docs/K2-DIY-CHAMBER-HEATER.md)
- [Stock CFS mapping and cutter behavior](docs/K2-stock-CFS-behavior.md)

## Set up the Orca companion

Use Windows with **Python 3.12 or later** and official OrcaSlicer with scripting plugin support. Install the [printer-side calibration adapter](docs/K2-STOCK-AUTO-CALIBRATION.md#restore-the-adapter) first. In PowerShell at the repository root, replace the example address and executable path with the ones for your setup.

```powershell
python -m venv .\K2-Orca-CFS\.venv
.\K2-Orca-CFS\Start-K2-AutoPA-Orca.cmd -PrinterAddress 192.0.2.10 -OrcaExecutable 'C:\Program Files\OrcaSlicer\orca-slicer.exe'
```

The launcher installs **K2 Calibration** into the Orca data directory and starts the local companion at `127.0.0.1:8766`. It connects to the printer’s Moonraker API on port **7125**. The printer address must match the host configured in Orca. Activate the plugin in Orca, then open **Auto Calibrations** from Speed Dial. See the [calibration guide](docs/K2-STOCK-AUTO-CALIBRATION.md) for supported spools and how to save results.

To start the companion automatically, run `.\K2-Orca-CFS\Install-K2-Companion-Autostart.ps1 -PrinterAddress 192.0.2.10` once. It stores the address and registers a logon task that restarts the companion within 15 minutes if it stops. Run it again with `-Uninstall` to remove it.

Calibration constants (material codes, flow ceilings and test plans) live only in the PC companion. The printer adapter checks hardware bounds: the 150–300 °C nozzle range and the live extruder limits.

Auto Max Flow predicts where the extruder current crosses its cutoff with one formula per material, `Iq = a + b0 · (0.4 / nozzle)^n · e^(−k·(T − 250)/10) · flow`. Each finished run updates the fit in `%LOCALAPPDATA%\K2AutoPA\reports\flow_model.json`, which stores running sums rather than points. Without data for a material, the search measures two flows and extrapolates.

To run the archived-run replay and companion tests offline, use the repository’s adapter folder.

```powershell
python -m unittest discover -s .\K2-Stock-KAMP-F021\stock-pa-adapter
```

## Upstream sources

The KAMP package uses the pinned [KAMP-K2 implementation](https://github.com/grant0013/KAMP-K2/tree/ef8fd5d). Creality-supplied configuration is based on [K2 Series Klipper](https://github.com/CrealityOfficial/K2_Series_Klipper). The pressure support includes a decoder and an offline analysis helper from [K2-OpenKlipper](https://github.com/grant0013/K2-OpenKlipper). The adapted files retain their upstream notices. [GNU GPL v3 text](K2-Stock-KAMP-F021/UPSTREAM-LICENSE.md)
