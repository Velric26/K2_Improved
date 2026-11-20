"""Run and analyze the supervised ABS endurance pull on the stock K2."""

import argparse
import bisect
import json
import math
import statistics
import time
from pathlib import Path

from analyze_capture import clean_pressure
from run_short_pa_sweep import Sweep


MAP_FLOWS = {"D": tuple(range(25, 41)), "C": tuple(range(15, 31)), "B": tuple(range(19, 30)), "A": tuple(range(19, 30))}
MAP_DURATIONS = (16, 20)
MAX_SAMPLE_GAP = .10
MAX_TEST_FLOW = 32


def check_extrusion_limits(diameter, extruder, flow, seconds):
    if not math.isclose(diameter, extruder["filament_diameter"], rel_tol=.005):
        raise RuntimeError("The preset filament diameter differs from the firmware setting.")
    speed = flow / (math.pi * (diameter / 2) ** 2)
    if (speed > extruder["max_extrude_only_velocity"]
            or speed * seconds > extruder["max_extrude_only_distance"]):
        raise RuntimeError("The test exceeds the firmware's extrusion-only limits.")


def analyze(data, expected_seconds=120):
    speed, = data["filament_feed_mm_s"]
    moves = [(t, seconds, start_v, accel) for t, seconds, start_v, accel in data["moves"]
             if seconds > 0 and (start_v > 0 or accel != 0)]
    if not moves:
        raise ValueError("The capture contains no extrusion motion.")
    start = moves[0][0]
    end = max(t + seconds for t, seconds, _, _ in moves)
    distance = sum(seconds * (start_v + .5 * accel * seconds) for _, seconds, start_v, accel in moves)
    gaps = [max(0., b[0] - a[0] - a[1]) for a, b in zip(moves, moves[1:])]
    slowdown = sum(seconds for _, seconds, start_v, accel in moves
                   if accel != 0 or not math.isclose(start_v, speed, rel_tol=.002))
    duration = end - start
    samples = clean_pressure(data["samples"])
    times = [t for t, _ in samples]

    def window(lo, hi):
        subset = samples[bisect.bisect_left(times, lo):bisect.bisect_left(times, hi)]
        edges = [lo, *(t for t, _ in subset), hi]
        gap = max(b - a for a, b in zip(edges, edges[1:]))
        return [p for _, p in subset], gap

    rest, rest_gap = window(start - 1., start - .05)
    if len(rest) < 160 or rest_gap > MAX_SAMPLE_GAP:
        raise ValueError("The pre-extrusion pressure baseline is incomplete.")
    tare = statistics.median(rest)
    points = []
    for second in range(math.floor(duration + .01)):
        values, gap = window(start + second, min(end, start + second + 1))
        usable = len(values) >= 160 and gap <= MAX_SAMPLE_GAP
        points.append({"start_s": second, "force": abs(statistics.mean(values) - tare) if usable else None,
                       "noise": statistics.stdev(values) if usable else None, "max_gap_s": gap})

    reference = [row for row in points[10:20] if row["force"] is not None]
    if len(reference) < 9:
        raise ValueError("The 10–20 second steady-flow reference has incomplete pressure data.")
    force = statistics.median(row["force"] for row in reference)
    noise = statistics.median(row["noise"] for row in reference)
    final = [row["force"] for row in points[expected_seconds - 10:expected_seconds] if row["force"] is not None]
    settling = next((a["start_s"] for a, b, c in zip(points, points[1:], points[2:])
                     if all(row["force"] is not None and abs(row["force"] - force) <= .05 * force
                            for row in (a, b, c))), None)
    onset = {}
    streaks = {}
    for row in points[20:]:
        usable = row["force"] is not None
        flags = {"force_drop": usable and row["force"] < .85 * force,
                 "force_rise": usable and row["force"] > 1.10 * force,
                 "force_noise": usable and row["noise"] > 3 * noise}
        for key, flag in flags.items():
            streaks[key] = streaks.get(key, 0) + 1 if flag else 0
            if streaks[key] >= 3:
                onset.setdefault(key, row["start_s"] - 2)
    usable_count = sum(row["force"] is not None for row in points)
    return {"duration_s": duration, "filament_mm": distance, "largest_motion_gap_s": max(gaps, default=0.),
            "off_cruise_s": slowdown, "tare": tare, "initial_force": force, "initial_noise": noise,
            "settled_at_s": settling, "reference_s": [10, 20],
            "final_change_pct": 100 * (statistics.mean(final) / force - 1) if force and len(final) >= 9 else None,
            "usable_seconds": usable_count, "signals_at_s": onset, "seconds": points,
            "complete": abs(duration - expected_seconds) < .25 and max(gaps, default=0.) < .10
            and usable_count == len(points) and not data.get("stream_errors"), "timing": data["timing"]}


def analyze_series(data):
    segments = data["planned_segments"]
    if [row[0] for row in segments] != [20, 30, 40]:
        raise ValueError("The K2 did not schedule all three flow targets.")
    summaries = []
    previous_end = None
    for (flow, start, end), feed in zip(segments, data["filament_feed_mm_s"], strict=True):
        rows = [row for row in data["moves"] if start - .05 <= row[0] < end
                and row[0] + row[1] <= end + .05]
        full_trace = any(duration > 59.8 and math.isclose(speed, feed, rel_tol=.002)
                         for _, duration, speed, _ in rows)
        # Klipper can discard motion history for earlier segments. The recorded
        # planner endpoint still aligns that segment with the pressure clock.
        moves = rows if full_trace else [[start, end - start, feed, 0.]]
        pressure = analyze(dict(data, moves=moves, filament_feed_mm_s=[feed]), 60)
        current = [(t - start, iq, status) for t, iq, status in data["motor_current"] if start <= t < end]
        early = [abs(iq) for t, iq, _ in current if 10 <= t < 20]
        late = [abs(iq) for t, iq, _ in current if 50 <= t < 60]
        if min(len(early), len(late)) < 10:
            early = late = []
        summaries.append(dict(pressure, flow_mm3_s=flow, motion_trace_complete=full_trace,
                              rest_before_s=None if previous_end is None else start - previous_end,
                              motor_samples=len(current), motor_samples_per_s=len(current) / 60,
                              motor_initial_abs_iq=statistics.median(early) if early else None,
                              motor_final_abs_iq=statistics.median(late) if late else None,
                              motor_peak_abs_iq=max((abs(iq) for _, iq, _ in current), default=None),
                              motor_status=sorted({status for _, _, status in current}),
                              motor_current=current))
        previous_end = end
    return summaries


def analyze_map(data, flows=MAP_FLOWS["D"], step_seconds=20, *, include_pressure=True):
    segments = sorted(data["planned_segments"], key=lambda row: row[1])
    if tuple(row[0] for row in segments) != tuple(flows):
        raise ValueError(f"The planner did not record every flow from {flows[0]} to {flows[-1]} mm³/s in order.")
    samples = clean_pressure(data["samples"]) if include_pressure else []
    times = [t for t, _ in samples]

    def pressure_window(start, end):
        rows = samples[bisect.bisect_left(times, start):bisect.bisect_left(times, end)]
        stamps = [start, *(t for t, _ in rows), end]
        gap = max(b - a for a, b in zip(stamps, stamps[1:]))
        if len(rows) < 500 or gap > MAX_SAMPLE_GAP:
            raise ValueError("The continuous flow map has an incomplete pressure window.")
        return [load for _, load in rows], gap

    origin = segments[0][1]
    if include_pressure:
        baseline, _ = pressure_window(origin - 1.5, origin - .1)
        tare = statistics.median(baseline)
    previous_end = None
    result = []
    for (flow, start, end), feed in zip(segments, data["filament_feed_mm_s"], strict=True):
        if not math.isclose(end - start, step_seconds, abs_tol=.05):
            raise ValueError("The recorded map step duration differs from the request.")
        settled_start = end - 8
        motor = [(iq, status) for t, iq, status in data["motor_current"] if start <= t < end]
        settled_motor = [abs(iq) for t, iq, _ in data["motor_current"] if settled_start <= t < end - .05]
        motion = [row for row in data["moves"] if start - .10 <= row[0] < end
                  and row[0] + row[1] <= end + .10 and row[3] == 0
                  and math.isclose(row[2], feed, rel_tol=.002)]
        row = {"flow_mm3_s": flow, "start_s": start - origin, "end_s": end - origin,
               "planner_gap_s": None if previous_end is None else start - previous_end,
               "motion_verified": any(row[1] > step_seconds - .2 for row in motion),
               "filament_commanded_mm": feed * (end - start),
               "motor_median_abs_iq": statistics.median(settled_motor) if len(settled_motor) >= 16 else None,
               "motor_peak_abs_iq": max((abs(iq) for iq, _ in motor), default=None),
               "motor_samples": len(motor), "settled_motor_samples": len(settled_motor),
               "motor_status": sorted({status for _, status in motor})}
        if include_pressure:
            early, _ = pressure_window(start + 4, start + 8)
            late, gap = pressure_window(settled_start, end - .05)
            early_load = abs(statistics.median(early) - tare)
            load = abs(statistics.median(late) - tare)
            row.update(load_counts=load, load_early_counts=early_load,
                       load_change_pct=100 * (load / early_load - 1) if early_load else None,
                       load_noise_counts=statistics.stdev(late), pressure_max_gap_s=gap)
        result.append(row)
        previous_end = end
    report = {"motor_missed": data["motor_missed"], "stream_errors": data.get("stream_errors", []), "steps": result}
    if include_pressure:
        report.update(pressure_tare=tare, pressure_timing=data["timing"])
    return report


def move_to_flow_position(sweep):
    current = sweep.status()
    sweep.check_position(current)
    sweep.check_filament(current)
    settings = sweep.read("/printer/objects/query?gcode_move=position,gcode_position&gcode_macro%20PRINTER_PARAM=max_z_position")["result"]["status"]
    movement = settings["gcode_move"]
    offsets = [absolute - logical for absolute, logical in zip(movement["position"], movement["gcode_position"])]
    bottom = min(float(settings["gcode_macro PRINTER_PARAM"]["max_z_position"]), current["toolhead"]["axis_maximum"][2])
    x, y, z = 130 - offsets[0], 130 - offsets[1], bottom - offsets[2]
    sweep.send("G90", "M220 S100", f"G1 Y{y:.6f} F2400", f"G1 X{x:.6f} F2400",
               f"G1 Z{z:.6f} F600", "M400", timeout=70)
    current = sweep.status()
    x, y, z = current["toolhead"]["position"][:3]
    if not sweep.idle(current) or max(abs(x - 130), abs(y - 130)) > .05 or abs(z - bottom) > .20:
        raise RuntimeError("The K2 did not reach the verified center and bottom position.")
    sweep.check_filament(current)


def capture_flow_map(sweep, name, seconds, diameter):
    filename = f"stock_pa_direct_{name}.json"
    try:
        sweep.send("SET_PRESSURE_ADVANCE ADVANCE=0", "G4 P3000", "M400",
                   f"STOCK_PA_DIRECT_MAP NAME={name} SLOT={sweep.slot} MATERIAL={sweep.material}"
                   f" TEMPERATURE={sweep.temperature} DIAMETER={diameter:.8f} SECONDS={seconds}", timeout=540)
    except TimeoutError:
        print("The command response timed out. Checking for the capture saved by Klipper.", flush=True)
    data = sweep.read(f"/server/files/config/{filename}", timeout=45)
    (sweep.folder / filename).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data, filename


def run(sweep, name, seconds=120):
    from orca_auto_pa import prepare_slot

    is_map = seconds in MAP_DURATIONS
    if not is_map and (sweep.slot, sweep.material) != ("D", "ABS"):
        raise RuntimeError("PETG in slots A, B, and C is supported by the continuous map.")
    state = sweep.status()
    nozzle = state["extruder"]
    if (not sweep.idle(state) or state["toolhead"]["homed_axes"] != "xyz"
            or nozzle["target"] or state["heater_bed"]["target"] or nozzle["nozzle_diameter"] != .4):
        raise RuntimeError("Start with the K2 idle, XYZ homed, both heaters off, and a 0.4 mm nozzle.")
    command = "STOCK_PA_DIRECT_MAP" if is_map else (
        "STOCK_PA_DIRECT_SERIES" if seconds == 60 else "STOCK_PA_DIRECT_SUSTAIN")
    if command not in sweep.read("/printer/gcode/help")["result"]:
        raise RuntimeError("The continuous sustained-flow command is missing.")
    settings = sweep.read("/printer/objects/query?configfile=settings")["result"]["status"]["configfile"]["settings"]
    extruder_settings = settings["extruder"]
    flows = MAP_FLOWS[sweep.slot] if is_map else {30: (20, 32, 40), 60: (20, 30, 40), 120: (MAX_TEST_FLOW,)}[seconds]
    segment_duration = seconds / 2 if seconds == 120 else seconds
    check_extrusion_limits(1.75, extruder_settings, max(flows), segment_duration)
    sweep.check_slot(state)
    results = []
    mapping = None
    sweep.send("SAVE_GCODE_STATE NAME=k2_sustain_session")
    try:
        print(f"Preparing {sweep.material} from slot {sweep.slot} and wiping the nozzle.", flush=True)
        prepare_slot(sweep, state, recover_unassigned=True)
        print("Lowering the bed and moving to X130 Y130.", flush=True)
        move_to_flow_position(sweep)
        if is_map:
            print(f"Starting the {flows[0]}–{flows[-1]} mm³/s map with {seconds} seconds per step and no rest.", flush=True)
            data, filename = capture_flow_map(sweep, name, seconds, 1.75)
            mapping = dict(analyze_map(data, flows, seconds), slot=f"1{sweep.slot}", material=sweep.material,
                           temperature_c=sweep.temperature, capture=filename, step_seconds=seconds)
        elif seconds == 60:
            print("Starting 20, 30, and 40 mm³/s for 60 seconds each, with 10 seconds of rest.", flush=True)
            filename = f"stock_pa_direct_{name}.json"
            try:
                sweep.send("SET_PRESSURE_ADVANCE ADVANCE=0", "G4 P3000", "M400",
                           f"{command} NAME={name} SLOT={sweep.slot} MATERIAL={sweep.material}"
                           f" TEMPERATURE={sweep.temperature} DIAMETER=1.75", timeout=540)
            except TimeoutError:
                print("The command response timed out. Checking for the capture saved by Klipper.", flush=True)
            data = sweep.read(f"/server/files/config/{filename}", timeout=45)
            (sweep.folder / filename).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            results = [dict(row, slot="1D", material="ABS", temperature_c=280, capture=filename,
                            motor_missed_total=data["motor_missed"]) for row in analyze_series(data)]
            for row in results:
                print({key: value for key, value in row.items() if key not in ("seconds", "motor_current")}, flush=True)
        for flow in (() if is_map or seconds == 60 else flows):
            capture_name = f"{name}_{flow}" if len(flows) > 1 else name
            print(f"Starting {seconds} seconds at {flow} mm³/s.", flush=True)
            started = time.time()
            sweep.send("M109 S280", "SET_PRESSURE_ADVANCE ADVANCE=0", "G4 P7000", "M400",
                       f"STOCK_PA_DIRECT_SUSTAIN NAME={capture_name} SLOT=D MATERIAL=ABS TEMPERATURE=280"
                       f" DIAMETER=1.75 FLOW={flow} SECONDS={seconds}", timeout=190)
            filename = f"stock_pa_direct_{capture_name}.json"
            data = sweep.read(f"/server/files/config/{filename}", timeout=35)
            (sweep.folder / filename).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            report = dict(analyze(data, seconds), slot="1D", material="ABS", temperature_c=280,
                          flow_mm3_s=flow, capture=filename, started_at=started, finished_at=time.time())
            results.append(report)
            path = sweep.folder / f"{capture_name}_summary.json"
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print({key: value for key, value in report.items() if key != "seconds"}, flush=True)
            print(path, flush=True)
    finally:
        final = sweep.status()
        if sweep.idle(final):
            sweep.send("M104 S0", f"SET_PRESSURE_ADVANCE ADVANCE={nozzle['pressure_advance']:.12g} "
                       f"SMOOTH_TIME={nozzle['smooth_time']:.12g}", "RESTORE_GCODE_STATE NAME=k2_sustain_session MOVE=0")
            final = sweep.status()
            if final["extruder"]["target"] or final["heater_bed"]["target"]:
                raise RuntimeError("Could not verify heater shutdown.")
            sweep.park_after_purge(final)
        else:
            raise RuntimeError(f"The K2 became {final['webhooks']['state']} during the sustained pull.")
    print("Extrusion stopped. The bed stays down and the toolhead stays centered.", flush=True)
    if mapping:
        path = sweep.folder / f"{name}_summary.json"
        path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        print(path, flush=True)
    elif len(results) > 1:
        path = sweep.folder / f"{name}_comparison.json"
        path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(path, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Moonraker URL for the stock K2.")
    parser.add_argument("--seconds", type=int, choices=(*MAP_DURATIONS, 30, 60, 120), default=120)
    parser.add_argument("--slot", choices=MAP_FLOWS, default="D")
    args = parser.parse_args()
    stamp = time.strftime("%H%M%S")
    label = "map" if args.seconds in MAP_DURATIONS else "sustain"
    material = "ABS" if args.slot == "D" else "PETG"
    run(Sweep(args.host, Path(__file__).resolve().parent, stamp, args.slot, material),
        f"{material.lower()}_{args.slot.lower()}_{label}_{stamp}", args.seconds)
