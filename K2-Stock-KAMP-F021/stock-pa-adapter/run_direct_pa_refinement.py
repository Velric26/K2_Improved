"""Run a supervised PA selection preview for PETG in slot 1B or ABS in slot 1D."""

import argparse
import json
import math
import statistics
import time
from pathlib import Path

from analyze_sweep import score_edges
from run_short_pa_sweep import CALIBRATION_LIMITS, CALIBRATION_SMOOTH, CLOSE_SCORE, Sweep, limit_command, motion_limits


COARSE = {"PETG": (550, 600, 650, 700, 750), "ABS": (400, 450, 500, 550, 600)}
CLI_MATERIAL = {"A": "PETG", "B": "PETG", "C": "PETG", "D": "ABS"}  # Ten-thousandths of a second.
FINE_STEP = 25
FINAL_STEP = 5
LOWER_SCORE_TIE = .005
BANDS = ("LOWER", "PEAK")
EDGES = ("rise", "fall")


def rank(captures, lower_limits=None, peak_tie=None):
    values = {}
    for capture in captures:
        if all(capture["edges"].get(edge) is not None for edge in EDGES):
            for edge in EDGES:
                key = (capture["pa"], capture["band"], edge)
                values.setdefault(key, []).append(capture["edges"][edge]["rms"])
    rows = []
    conditions = [(band, edge) for band in BANDS for edge in EDGES]
    for pa in sorted({capture["pa"] for capture in captures}):
        repeats = [values.get((pa, band, edge), []) for band, edge in conditions]
        if min(map(len, repeats)) < 2:
            continue
        medians = [statistics.median(scores) for scores in repeats]
        noise = [(max(scores) - min(scores)) / 2 for scores in repeats]
        rows.append({"pa": pa, "scores": medians, "repeat_half_ranges": noise,
                     "lower_worst": max(medians[:2]), "peak_worst": max(medians[2:])})
    if not rows:
        return rows, None, None, None
    repeat_noise = [statistics.median(row["repeat_half_ranges"][i] for row in rows) for i in range(4)]
    allowances = [max(CLOSE_SCORE, noise) for noise in repeat_noise]
    if lower_limits is None:
        lower_limits = [min(row["scores"][i] for row in rows) + allowances[i] for i in range(2)]
    if peak_tie is None:
        peak_tie = max(allowances[2:])
    for row in rows:
        row["lower_pass"] = all(score <= limit for score, limit in zip(row["scores"][:2], lower_limits))
    passing = [row for row in rows if row["lower_pass"]]
    if not passing:
        return rows, None, lower_limits, peak_tie
    peak_best = min(row["peak_worst"] for row in passing)
    near_peak = [row for row in passing if row["peak_worst"] <= peak_best + peak_tie]
    lower_best = min(row["lower_worst"] for row in near_peak)
    lower_tie = max(LOWER_SCORE_TIE, *repeat_noise[:2])
    winner = min((row for row in near_peak if row["lower_worst"] <= lower_best + lower_tie),
                 key=lambda row: row["pa"])
    return rows, winner, lower_limits, peak_tie


def measure(sweep, flows, stamp, report, confirmation=None, update=print, diameter=1.75, extended_stroke=False):
    captures = report["captures"]
    prime_mm = report["prime_mm_per_stroke"]
    sweep.check_position(sweep.status())
    sweep.send(limit_command(CALIBRATION_LIMITS), "G90", "M83", "M220 S100", "M221 S100",
               *([] if prime_mm else ["G1 E3 F120", "M400"]))
    stages = ((COARSE[sweep.material], 50), (None, FINE_STEP), (None, FINAL_STEP)) if confirmation is None else (
        (confirmation, FINE_STEP),)
    # MEASURED_FLOW is the existing firmware flag for the extended, checked stroke.
    flow_option = " MEASURED_FLOW=1" if extended_stroke else ""
    for settings, step in stages:
        stage = f"{step / 10000:.4f}" + (" confirmation" if confirmation is not None else "")
        if settings is None:
            rows, winner, lower_limits, _ = rank(captures)
            if not rows:
                return
            if winner is None:
                # The high-flow winner may be far from passing one of the low-flow edges.
                # Minimize the largest miss first, then favor high flow and lower PA in ties.
                center = min(rows, key=lambda row: (
                    max(score - limit for score, limit in zip(row["scores"][:2], lower_limits, strict=True)),
                    row["peak_worst"], row["pa"],
                ))
                update(f"No PA passed both low-flow checks. Refining around PA {center['pa']:.4f} in {stage} steps.")
            else:
                center = winner
                update(f"Refining around PA {center['pa']:.4f} in {stage} steps.")
            key = round(center["pa"] * 10000)
            settings = (key - step, key + step)
        for repeat in (1, 2):
            order = settings if repeat == 1 else reversed(settings)
            for k in order:
                sweep.check_cancel()
                if report["captures"]:
                    sweep.wipe()
                bands = BANDS if repeat == 1 else tuple(reversed(BANDS))
                for band in bands:
                    sweep.check_cancel()
                    current = sweep.status()
                    if not sweep.idle(current):
                        raise RuntimeError("The printer is no longer idle.")
                    sweep.check_filament(current)
                    sweep.check_position(current)
                    nozzle = current["extruder"]
                    if nozzle["target"] != sweep.temperature or abs(nozzle["temperature"] - sweep.temperature) > 5:
                        raise RuntimeError("The nozzle temperature moved outside the calibration range.")
                    pa = k / 10000
                    flow = flows[band]
                    update(f"PA {pa:.4f} at {flow:.3f} mm³/s, repeat {repeat}.")
                    name = f"{sweep.slot.lower()}_ref_{stamp}_{k}_{band[0]}{repeat}"
                    prime = [f"G1 E{prime_mm:g} F120", "M400"] if prime_mm else []
                    sweep.send(f"SET_PRESSURE_ADVANCE ADVANCE={pa:.4f} SMOOTH_TIME={CALIBRATION_SMOOTH}",
                               *prime,
                               f"STOCK_PA_DIRECT_STROKE NAME={name} FLOW={flow:.8f} BAND={band} SLOT={sweep.slot}"
                               f" MATERIAL={sweep.material} TEMPERATURE={sweep.temperature} DIAMETER={diameter:.8f}{flow_option}",
                               "G1 X115 F120", "M400", timeout=60)
                    filename = f"stock_pa_direct_{name}.json"
                    data = sweep.read(f"/server/files/config/{filename}")
                    (sweep.folder / filename).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                    expected = data["filament_feed_mm_s"][1] * math.pi * (diameter / 2) ** 2
                    if not math.isclose(expected, flow, abs_tol=1e-7):
                        raise RuntimeError("The stroke used a different flow from the requested peak.")
                    result = score_edges(data)
                    row = {"band": band, "flow": flow, "pa": pa, "repeat": repeat, "stage": stage,
                           "file": filename, "timing": data["timing"], "quality": result["quality"],
                           "edges": {edge: result[edge] for edge in EDGES}}
                    captures.append(row)
                    print(json.dumps(row), flush=True)


def run(sweep, flows, stamp, report, confirmation=None):
    from orca_auto_pa import prepare_slot

    state = sweep.status()
    ext = state["extruder"]
    if (not sweep.idle(state) or ext["target"] or state["heater_bed"]["target"] or ext["nozzle_diameter"] != .4):
        raise RuntimeError("The K2 must be idle with heaters off and the verified 0.4 mm nozzle.")
    sweep.check_filament(state)
    limits = motion_limits(state)
    captures = report["captures"]
    sweep.send("SAVE_GCODE_STATE NAME=k2_direct_refinement")
    try:
        print(f"Preparing {sweep.material} from slot 1{sweep.slot} at {sweep.temperature} °C.", flush=True)
        prepare_slot(sweep, state)
        measure(sweep, flows, stamp, report, confirmation)
    finally:
        state = sweep.status()
        if sweep.idle(state):
            sweep.send("M104 S0", limit_command(limits),
                       f"SET_PRESSURE_ADVANCE ADVANCE={ext['pressure_advance']} SMOOTH_TIME={ext['smooth_time']}",
                       "RESTORE_GCODE_STATE NAME=k2_direct_refinement MOVE=0")
            sweep.park_after_purge(sweep.status())
            if captures:
                sweep.send(f"STOCK_PA_CAPTURE WINDOWS=4 NAME=direct_ref_handoff_{stamp}")
        state = sweep.status()
        report["final_state"] = {"extruder_target": state["extruder"]["target"], "bed_target": state["heater_bed"]["target"],
                                 "pressure_advance": state["extruder"]["pressure_advance"],
                                 "smooth_time": state["extruder"]["smooth_time"], "motion_limits": motion_limits(state),
                                 "loaded_slot": state["box"]["T1"]["filament"], "stock_reader": state["k2_stock_pa_capture"]}
        if (state["extruder"]["target"] or state["heater_bed"]["target"] or motion_limits(state) != limits
                or any(state["extruder"][key] != ext[key] for key in ("pressure_advance", "smooth_time"))):
            raise RuntimeError("Heater shutdown or restoration of the original PA and motion settings could not be verified.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Moonraker URL for the stock K2.")
    parser.add_argument("--lower", type=float, required=True)
    parser.add_argument("--peak", type=float, required=True)
    parser.add_argument("--slot", choices=CLI_MATERIAL, default="B")
    parser.add_argument("--prime-mm", type=float, choices=(0., 3.), default=0.)
    parser.add_argument("--confirm", type=Path, help="Retest the prior candidate and its nearest neighbors.")
    args = parser.parse_args()
    if not 0 < args.lower < args.peak <= 24:
        parser.error("Require distinct positive peaks with the high flow at or below 24 mm³/s.")
    folder = Path(__file__).resolve().parent
    stamp = time.strftime("%H%M%S")
    flows = dict(zip(BANDS, (args.lower, args.peak)))
    material = CLI_MATERIAL[args.slot]
    sweep = Sweep(args.host, folder, stamp, args.slot, material, lambda: None)
    report = {"slot": f"1{args.slot}", "material": material, "temperature_c": sweep.temperature, "flows": flows,
              "prime_mm_per_stroke": args.prime_mm, "captures": [], "completed": False}
    prior = None
    confirmation = None
    if args.confirm:
        prior = json.loads(args.confirm.read_text(encoding="utf-8"))
        if (not prior["completed"] or prior["flows"] != flows or prior["slot"] != report["slot"]
                or prior.get("prime_mm_per_stroke", 0.) != args.prime_mm
                or prior["candidate"] is None):
            parser.error("Use a complete report with a candidate from the same slot, flow targets, and priming length.")
        k = round(prior["candidate"]["pa"] * 10000)
        confirmation = (k - 2 * FINE_STEP, k - FINE_STEP, k, k + FINE_STEP)
    path = folder / f"{material.lower()}_{args.slot.lower()}_direct_refinement_{stamp}.json"
    print("Report:", path, flush=True)
    try:
        run(sweep, flows, stamp, report, confirmation)
        report["completed"] = True
    finally:
        if prior:
            report["confirmation_of"] = args.confirm.name
            report["captures"] = prior["captures"] + report["captures"]
        rows, winner, limits, tie = rank(report["captures"], prior["lower_limits"] if prior else None,
                                          prior["peak_tie"] if prior else None)
        report.update(scores=rows, candidate=winner, lower_limits=limits, peak_tie=tie)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Selection:", json.dumps({"candidate": winner, "lower_limits": limits, "peak_tie": tie}), flush=True)


if __name__ == "__main__":
    main()
