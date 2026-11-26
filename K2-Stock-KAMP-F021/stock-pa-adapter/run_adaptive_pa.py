"""Measure independent PA winners on an Orca flow/acceleration grid."""

import json
import math
import statistics
import time

from adaptive_pa_motion import COUPON_CENTER, MAX_LAYER_Z, MAX_PA, MIN_ACCEL, MIN_SPAN, PRINT_COMMAND
from adaptive_pa_motion import coupon_span, ring_gcode, wall_ring
from analyze_sweep import MAX_EDGE_GAP, interval
from analyze_capture import clean_pressure, cv_segments
from run_short_pa_sweep import CALIBRATION_SMOOTH, limit_command, motion_limits

BED_SOAK_SECONDS = 180
FIRST_LAYER_SPEED = 20
FIRST_LAYER_LIMITS = (500, 250)
# Unmeasured layers between the first layer and the test walls, ramping (speed mm/s, accel mm/s²)
# so the tower base bonds before the full wall speed and acceleration pull on it.
FOUNDATION_RAMP = ((30, 1000), (45, 1500), (60, 2000))
BRIM_WIDTH = 5.
SKIRT_GAP = 2.
CHAMBER_STATUS = "heater_generic chamber_heater"
TOWER_OBJECT = "K2_ADAPTIVE_PA_TOWER"
RESET_OBJECTS = "EXCLUDE_OBJECT_DEFINE RESET=1"


def grid(request, peak_limit):
    walls = request.get("walls")
    if not isinstance(walls, dict) or walls.keys() != {"outer", "inner"}:
        raise ValueError("Supply inner and outer wall flow, speed and acceleration from Orca.")
    ceiling, diameter, seed = (request.get(key) for key in ("max_flow_mm3_s", "filament_diameter_mm", "pa_seed"))
    if type(seed) not in (int, float) or not 0 <= seed <= MAX_PA:
        raise ValueError(f"Set a fixed starting PA between 0 and {MAX_PA:.3f} in the filament preset.")
    targets = {}
    for wall, settings in walls.items():
        if not isinstance(settings, dict):
            raise ValueError(f"Invalid {wall} wall settings.")
        flow, speed, accel = (settings.get(key) for key in ("flow", "speed", "acceleration"))
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (flow, speed, accel)):
            raise ValueError(f"Set a positive {wall} wall flow, speed and acceleration.")
        if flow > min(peak_limit, ceiling):
            raise ValueError(f"The {wall} wall target exceeds the supported {min(peak_limit, ceiling):g} mm³/s peak.")
        targets[wall] = (flow, speed, accel)
    outer, inner = (targets[wall] for wall in ("outer", "inner"))
    flows = [("outer", outer[0], outer[1]), ("inner", inner[0], inner[1])]
    if math.isclose(outer[0], inner[0], rel_tol=.001):
        flows = [("slower outer", outer[0] / 2, outer[1] / 2), ("wall peak", outer[0], outer[1])]
    accelerations = sorted({round(outer[2]), round(inner[2])})
    if len(accelerations) == 1:
        nominal, = accelerations
        alternate = max(int(MIN_ACCEL), nominal // 2)
        if alternate == nominal:
            raise ValueError("Set a second wall acceleration above 500 mm/s² for this pilot.")
        accelerations.insert(0, alternate)
    conditions = []
    for accel in accelerations:
        for wall, flow, speed in sorted(flows, key=lambda row: row[1]):
            wall_ring(MIN_SPAN, 0., flow, speed, diameter)
            conditions.append({"wall": wall, "flow": flow, "speed": speed, "acceleration": accel})
    coupon_span(conditions)
    return conditions


def validate_coupon(request):
    layer = request.get("layer_height_mm")
    first = request.get("first_layer_height_mm")
    bed = request.get("bed_temperature_c")
    nozzle = request["nozzle_mm"]
    if (any(type(v) not in (int, float) or not .14 <= v <= .8 * nozzle for v in (layer, first))
            or type(bed) is not int or not 45 <= bed <= 100):
        raise ValueError("Use Orca layer heights from 0.14 mm to 80% of nozzle size and a 45–100 °C bed.")
    chamber = request.get("chamber_temperature_c", 0)
    if type(chamber) is not int or not 0 <= chamber <= 60:
        raise ValueError("Set the Adaptive PA filament chamber target to 0–60 °C.")
    for wall in request["walls"].values():
        corner = wall.get("corner_velocity", 0.)
        if type(corner) not in (int, float) or not 0 <= corner <= 25:
            raise ValueError("Wall corner speed must be from 0 to 25 mm/s.")


def chamber_target(sweep):
    status = sweep.read("/printer/objects/query?heater_generic%20chamber_heater=target")["result"]["status"]
    return status.get(CHAMBER_STATUS, {}).get("target")


def soak_bed(sweep, target):
    deadline = time.monotonic() + BED_SOAK_SECONDS
    while True:
        sweep.check_cancel()
        state = sweep.status()
        if not sweep.idle(state) or state["heater_bed"]["target"] != target:
            raise RuntimeError("The printer state or bed target changed during the Adaptive PA soak.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 2))


def probe_tower(sweep, skirt_moves, update):
    x, y = zip(*(move[:2] for move in skirt_moves))
    lo_x, lo_y = (round(min(axis) - 1, 3) for axis in (x, y))
    hi_x, hi_y = (round(max(axis) + 1, 3) for axis in (x, y))
    polygon = [[lo_x, lo_y], [hi_x, lo_y], [hi_x, hi_y], [lo_x, hi_y]]
    encoded = json.dumps(polygon, separators=(",", ":"))
    update("Probing an adaptive bed mesh around the tower, brim and skirt.")
    sweep.send("G90", "SET_GCODE_OFFSET X=0 Y=0 Z=0", "G28 Z", "M400", timeout=130)
    sweep.check_cancel()
    try:
        sweep.send(RESET_OBJECTS,
                   f"EXCLUDE_OBJECT_DEFINE NAME={TOWER_OBJECT} CENTER={COUPON_CENTER:g},{COUPON_CENTER:g} POLYGON={encoded}")
        objects = sweep.read("/printer/objects/query?exclude_object=objects")["result"]["status"]["exclude_object"]["objects"]
        if len(objects) != 1 or objects[0]["name"] != TOWER_OBJECT or objects[0].get("polygon") != polygon:
            raise RuntimeError("KAMP needs the Adaptive PA tower as its sole mesh object.")
        sweep.check_cancel()
        sweep.send("BED_MESH_CLEAR", "BED_MESH_CALIBRATE", "M400", timeout=900)
        sweep.check_cancel()
        status = sweep.read("/printer/objects/query?bed_mesh&configfile=settings")["result"]["status"]
        mesh = status["bed_mesh"]
        full = status["configfile"]["settings"]["bed_mesh"]
        matrix = mesh["probed_matrix"]
        if (not mesh["profile_name"] or not matrix or not matrix[0]
                or any(v > limit + .05 for v, limit in zip(mesh["mesh_min"], (lo_x, lo_y), strict=True))
                or any(v < limit - .05 for v, limit in zip(mesh["mesh_max"], (hi_x, hi_y), strict=True))
                or (mesh["mesh_min"] == full["mesh_min"] and mesh["mesh_max"] == full["mesh_max"])):
            raise RuntimeError("The adaptive mesh did not cover the skirt or used the full bed.")
        return {"min": mesh["mesh_min"], "max": mesh["mesh_max"],
                "probe_count": [len(matrix[0]), len(matrix)]}
    finally:
        if sweep.idle(sweep.status()):
            sweep.send(RESET_OBJECTS)


def prepare_bed(sweep, skirt_moves, target, chamber, update):
    sweep.park_after_purge(sweep.status())
    preheat = f"Heating the bed to {target} °C"
    commands = [f"M140 S{target}"]
    if chamber:
        preheat += f" and the chamber to {chamber} °C in the background"
        commands.append(f"SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET={chamber}")
    update(preheat + ".")
    sweep.send(*commands, "M104 S140", f"M190 S{target}", timeout=360)
    sweep.check_cancel()
    update(f"Soaking the bed at {target} °C for {BED_SOAK_SECONDS // 60} minutes.")
    soak_bed(sweep, target)
    sweep.send("M109 S140", timeout=120)
    mesh = probe_tower(sweep, skirt_moves, update)
    sweep.check_cancel()
    sweep.send(f"M109 S{sweep.temperature}", "M400", timeout=140)
    return mesh


def print_ring(sweep, span, inset, flow, speed, diameter, z, capture=None):
    (x, y), moves = wall_ring(span, inset, flow, speed, diameter)
    prelude = ("G90", "M83", "M220 S100", "M221 S100", f"G1 Z{z + 1:.5f} F600",
               f"G1 X{x:.5f} Y{y:.5f} F4800", f"G1 Z{z:.5f} F600")
    commands = ["M400", capture] if capture else ring_gcode(moves, speed) + ["M400"]
    sweep.send(*prelude, *commands, timeout=65)


def first_layer_paths(width, height, inner_wall_inset):
    spacing = width - height * (1 - math.pi / 4)
    if not .2 <= spacing <= 1.2:
        raise RuntimeError("The first-layer line width is outside the tower's extrusion bounds.")
    brim_loops = round(BRIM_WIDTH / spacing)
    brim = tuple(-index * spacing for index in range(brim_loops, 0, -1))
    skirt = brim[0] - SKIRT_GAP - width
    return FIRST_LAYER_SPEED * height * spacing, skirt, brim + (inner_wall_inset, 0.)


def print_first_layer(sweep, span, borders, flow, diameter, height):
    limits = motion_limits(sweep.status())
    sweep.send(limit_command(FIRST_LAYER_LIMITS),
               f"SET_PRESSURE_ADVANCE ADVANCE=0 SMOOTH_TIME={CALIBRATION_SMOOTH}")
    for border in borders:
        sweep.check_cancel()
        print_ring(sweep, span, border, flow, FIRST_LAYER_SPEED, diameter, height)
    sweep.send(limit_command(limits))


def print_foundation(sweep, span, walls, inset, diameter, first, layer, seed):
    """Print both walls slowly for FOUNDATION_RAMP layers and return the next free layer number."""
    limits = motion_limits(sweep.status())
    for number, (speed, accel) in enumerate(FOUNDATION_RAMP, 1):
        sweep.check_cancel()
        accel = min(accel, min(w["acceleration"] for w in walls.values()))
        sweep.send(limit_command((accel, accel / 2)),
                   f"SET_PRESSURE_ADVANCE ADVANCE={seed:.4f} SMOOTH_TIME={CALIBRATION_SMOOTH}")
        for wall, wall_inset in (("inner", inset), ("outer", 0.)):
            settings = walls[wall]
            wall_speed = min(speed, settings["speed"])
            print_ring(sweep, span, wall_inset, settings["flow"] * wall_speed / settings["speed"],
                       wall_speed, diameter, first + number * layer)
    sweep.send(limit_command(limits))
    return len(FOUNDATION_RAMP)


def score_printed_wall(data):
    plan = data["planned_segments"]
    e_area = math.pi * (plan["filament_diameter_mm"] / 2) ** 2
    measured = [row for row in data["moves"] if row[1] > 0 and (row[2] > 0 or row[3] != 0)]
    if not measured:
        return None, {}, "no_extrusion_motion"
    peak = max(max(v, v + a * duration) for _, duration, v, a in measured) * e_area
    volume = sum(duration * (v + .5 * a * duration) for _, duration, v, a in measured) * e_area
    plateaus = [(start, end, v * e_area) for start, end, v, _ in cv_segments(measured)
                if end - start >= .09 and v * e_area >= peak * .95]
    flow = {"peak_mm3_s": peak, "volume_mm3": volume, "corner_flow_mm3_s": []}
    if not plateaus:
        return None, flow, "no_cruise_plateau"
    flow["cruise_mm3_s"] = cruise = statistics.median(v for _, _, v in plateaus)
    if abs(cruise - plan["wall_flow_mm3_s"]) > max(.03, plan["wall_flow_mm3_s"] * .025):
        return None, flow, "target_flow_not_reached"
    if data.get("stream_errors"):
        return None, flow, "motion_capture_error"
    clean = clean_pressure(data["samples"])
    if not clean:
        return None, flow, "no_pressure_samples"
    times, pressure = zip(*clean)
    rest = interval(times, pressure, measured[0][0] - .25, measured[0][0] - .04)
    steady = [p for start, end, _ in plateaus for p in interval(times, pressure, start + .04, end - .02)]
    if len(rest) < 20 or len(steady) < 20:
        return None, flow, "missing_pressure_baseline"
    zero, high = statistics.median(rest), statistics.median(steady)
    scale = high - zero
    if abs(scale) < 20000:
        return None, flow, "weak_pressure_step"
    edges = []
    corner_records = []
    for decel, accel in zip(measured, measured[1:]):
        start, duration, v, a = decel
        junction, ramp, low, rise = accel
        if (a >= 0 or rise <= 0 or not math.isclose(start + duration, junction, abs_tol=.002)
                or not math.isclose(v + a * duration, low, abs_tol=.04)
                or min(v, low + rise * ramp) * e_area < cruise * .90
                or low * e_area > cruise * .6):
            continue
        flow["corner_flow_mm3_s"].append(low * e_area)
        xy_factor = e_area * plan["xy_speed"] / plan["wall_flow_mm3_s"]
        speedup, slowdown = rise * xy_factor, -a * xy_factor
        tolerance = max(75., plan["xy_acceleration"] * .08)
        if any(abs(value - plan["xy_acceleration"]) > tolerance for value in (speedup, slowdown)):
            continue
        end = junction + ramp
        scores = []
        windows = ((junction, end + .045, accel), (start - .02, junction, decel))
        for begin, stop, (t0, duration, v0, rate) in windows:
            samples = interval(times, clean, begin, stop)
            ticks = [begin, *(t for t, _ in samples), stop]
            if len(samples) < 5 or max(b - a for a, b in zip(ticks, ticks[1:])) > MAX_EDGE_GAP:
                break
            residuals = [(p - zero) / scale -
                         (v0 + rate * min(duration, max(0., t - t0))) * e_area / cruise for t, p in samples]
            scores.append(math.sqrt(statistics.mean(value ** 2 for value in residuals)))
        if len(scores) == 2:
            edges.append(scores)
            corner_records.append({"time_s": junction, "flow_mm3_s": low * e_area, "scores": scores,
                                   "speedup_mm_s2": speedup, "slowdown_mm_s2": slowdown})
    flow["usable_corners"] = corner_records
    if len(edges) < 2:
        return None, flow, "insufficient_usable_corners"
    return [statistics.median(edge) for edge in zip(*edges)], flow, None


def rank(captures):
    candidates = {}
    for capture in captures:
        if capture["scores"] is not None:
            candidates.setdefault(capture["pa"], []).append(capture["scores"])
    rows = []
    for pa, repeats in sorted(candidates.items()):
        if len(repeats) >= 2:
            rise, fall = zip(*repeats, strict=True)
            midpoints = [statistics.median(edge) for edge in (rise, fall)]
            rows.append({"pa": pa, "scores": midpoints, "worst": max(midpoints),
                         "noise": max((max(edge) - min(edge)) / 2 for edge in (rise, fall))})
    if not rows:
        return rows, None
    best = min(row["worst"] for row in rows)
    tolerance = max(.005, statistics.median(row["noise"] for row in rows))
    choice = next(row for row in rows if row["worst"] <= best + tolerance)
    return rows, choice


def measure(sweep, conditions, stamp, report, request, update):
    diameter = request["filament_diameter_mm"]
    seed = request["pa_seed"]
    start = round(seed * 10000)
    upper = round(MAX_PA * 10000)
    span = coupon_span(conditions)
    bed = request["bed_temperature_c"]
    chamber = request.get("chamber_temperature_c", 0)
    layer, first = request["layer_height_mm"], request["first_layer_height_mm"]
    walls = request["walls"]
    inset = sum(w["flow"] / w["speed"] for w in walls.values()) / (2 * layer)
    if not .2 <= inset <= 1.2:
        raise RuntimeError("The selected wall widths fall outside this coupon's two-perimeter spacing.")
    corner_default = sweep.read("/printer/objects/query?toolhead=square_corner_velocity")["result"]["status"]["toolhead"]["square_corner_velocity"]
    width = walls["outer"]["flow"] / (walls["outer"]["speed"] * layer) + layer * (1 - math.pi / 4)
    first_flow, skirt, borders = first_layer_paths(width, first, inset)
    _, skirt_moves = wall_ring(span, skirt, first_flow, FIRST_LAYER_SPEED, diameter)
    report.update(span_mm=span, bed_temperature_c=bed, chamber_temperature_c=chamber,
                  bed_soak_seconds=BED_SOAK_SECONDS, layer_height_mm=layer, first_layer_height_mm=first,
                  first_layer={"skirt_loops": 1, "skirt_gap_mm": SKIRT_GAP,
                               "brim_loops": len(borders) - 2, "brim_width_mm": -borders[0],
                               "speed_mm_s": FIRST_LAYER_SPEED, "acceleration_mm_s2": FIRST_LAYER_LIMITS[0],
                               "pressure_advance": 0},
                  foundation_layers=[{"speed_mm_s": s, "acceleration_mm_s2": a} for s, a in FOUNDATION_RAMP])
    sweep.check_cancel()
    report["adaptive_mesh"] = prepare_bed(sweep, skirt_moves, bed, chamber, update)
    update(f"Printing the priming skirt, brim and first layer at {FIRST_LAYER_SPEED} mm/s.")
    report["coupon_started"] = True
    print_first_layer(sweep, span, (skirt, *borders), first_flow, diameter, first)
    update(f"Printing {len(FOUNDATION_RAMP)} slow base layers before the measured walls.")
    layer_number = print_foundation(sweep, span, walls, inset, diameter, first, layer, seed)
    for index, condition in enumerate(conditions, 1):
        sweep.check_cancel()
        captures = []
        entry = {**condition, "captures": captures, "pa": None}
        report["conditions"].append(entry)
        numbers = {max(0, min(upper, start + offset)) for offset in (-100, -50, 0, 50, 100)}
        flow, speed, accel = (condition[key] for key in ("flow", "speed", "acceleration"))
        role = "inner" if condition["wall"] == "inner" else "outer"
        corner = walls[role].get("corner_velocity", 0.) or corner_default
        for next_step in (25, 5, None):
            for repeat in (1, 2):
                for k in sorted(numbers, reverse=(repeat == 2)):
                    sweep.check_cancel()
                    pa = k / 10000
                    layer_number += 1
                    z = first + layer_number * layer
                    if z > MAX_LAYER_Z or not sweep.idle(sweep.status()):
                        raise RuntimeError("The printer is no longer ready for the next coupon layer.")
                    name = f"{sweep.slot.lower()}_apa_{stamp}_{index}_{k}_{repeat}"
                    update(f"Adaptive PA {index}/{len(conditions)} · {condition['wall']} · {flow:.3g} mm³/s · "
                           f"{accel} mm/s² · PA {pa:.4f} · repeat {repeat}")
                    sweep.send(f"SET_PRESSURE_ADVANCE ADVANCE={pa:.4f} SMOOTH_TIME={CALIBRATION_SMOOTH}")
                    capture = (f"{PRINT_COMMAND} NAME={name} FLOW={flow:.8f} SPEED={speed:.8f} ACCEL={accel} "
                               f"CORNER={corner:.8f} SPAN={span:.5f} Z={z:.5f} BED={bed} "
                               f"SLOT={sweep.slot} MATERIAL={sweep.material} TEMPERATURE={sweep.temperature} "
                               f"DIAMETER={diameter:.8f}")
                    for wall, wall_inset in (("inner", inset), ("outer", 0.)):
                        if wall == role:
                            print_ring(sweep, span, wall_inset, flow, speed, diameter, z,
                                       f"{capture} INSET={wall_inset:.5f}")
                        else:
                            support = walls[wall]
                            support_speed = min(60, support["speed"])
                            print_ring(sweep, span, wall_inset, support["flow"] * support_speed / support["speed"],
                                       support_speed, diameter, z)
                    filename = f"stock_pa_direct_{name}.json"
                    data = sweep.read(f"/server/files/config/{filename}")
                    (sweep.folder / filename).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                    scores, measured_flow, reason = score_printed_wall(data)
                    captures.append({"pa": pa, "repeat": repeat, "z_mm": z, "scores": scores, "flow": measured_flow,
                                     "reason": reason, "file": filename})
            rows, choice = rank(captures)
            if choice is None or next_step is None:
                break
            center = round(choice["pa"] * 10000)
            tested = {round(capture["pa"] * 10000) for capture in captures}
            numbers = {k for k in (center - next_step, center + next_step) if 0 <= k <= upper} - tested
        entry["scores"] = rows
        recorded = [c["flow"]["cruise_mm3_s"] for c in captures if "cruise_mm3_s" in c["flow"]]
        if recorded:
            entry["observed_flow"] = statistics.median(recorded)
        if choice and len(rows) >= 3 and rows[0]["pa"] < choice["pa"] < rows[-1]["pa"]:
            measured = [c["flow"]["cruise_mm3_s"] for c in captures if c["pa"] == choice["pa"] and c["scores"] is not None]
            entry.update(pa=choice["pa"], selected_scores=choice["scores"], measured_flow=statistics.median(measured))
        else:
            entry["reason"] = "No bracketed PA with two usable traces per candidate."
    return [{key: entry[key] for key in ("wall", "flow", "speed", "acceleration", "pa", "selected_scores",
                                         "measured_flow", "observed_flow") if key in entry}
            for entry in report["conditions"]]
