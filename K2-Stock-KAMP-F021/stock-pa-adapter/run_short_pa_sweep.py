"""Run a supervised PA sweep with the requested filament already loaded."""

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from urllib.request import Request, urlopen

from analyze_sweep import score


STATE = (
    "/printer/objects/query?print_stats=state&virtual_sdcard=is_active"
    "&pause_resume=is_paused&webhooks=state&toolhead=homed_axes,position,axis_maximum,max_accel,max_accel_to_decel"
    "&extruder=temperature,target,nozzle_diameter,pressure_advance,smooth_time&heater_bed=target"
    "&filament_switch_sensor%20filament_sensor=filament_detected"
    "&box=state,enable,filament,map,T1&k2_stock_pa_capture"
)
SENSOR = "filament_switch_sensor filament_sensor"
CLOSE_SCORE = 0.020
PA_STEP = 10  # In thousandths of a second.
MAX_EDGE_EXPANSIONS = 4
MAX_CAPTURE_ATTEMPTS = 6
CALIBRATION_LIMITS = (500., 100.)
CALIBRATION_SMOOTH = .040
PROFILES = {"PETG": ("000003", 240, 50), "ABS": ("000004", 280, 57)}
SLOT_MATERIALS = {"A": "PETG", "D": "ABS"}
BASE_FEED = (2.0, 5.0, 2.0)
MOTION = ((115.34, 24, .85), (115.94, 60, .60), (116.28, 24, .85))


def motion_limits(state):
    head = state["toolhead"]
    return head["max_accel"], head["max_accel_to_decel"]


def limit_command(limits):
    accel, accel_to_decel = limits
    return f"SET_VELOCITY_LIMIT ACCEL={accel:.12g} ACCEL_TO_DECEL={accel_to_decel:.12g}"


def timing_candidate(rows, best):
    keys = (best - PA_STEP, best, best + PA_STEP)
    points = [(k / 1000, statistics.median(row[3] for row in rows[k] if row is not None)) for k in keys]
    if not points[0][1] < points[1][1] < points[2][1]:
        return None
    for (k0, t0), (k1, t1) in zip(points, points[1:]):
        if t0 <= 0 <= t1:
            return k0 + (k1 - k0) * -t0 / (t1 - t0)
    return None


def preset_flows(maximum, diameter, lower_flow=None):
    area = math.pi * (diameter / 2) ** 2
    peak = maximum / area
    normal = min(BASE_FEED[1], peak * .75) if lower_flow is None else lower_flow / area
    return (normal * .4, normal, normal * .4), (peak * .55, peak, peak * .55)


def extrusion_moves(feeds):
    # Keep the stock purge-bin stroke and ramp durations. Only E lengths change.
    lengths = [math.floor(speed * segment[2] * 1e6 + 1e-9) / 1e6
               for speed, segment in zip(feeds, MOTION, strict=True)]
    lines = [f"G1 X{x} E{length:.6f} F{feed}"
             for (x, feed, _), length in zip(MOTION, lengths, strict=True)]
    return lines, lengths


class Sweep:
    def __init__(self, host, folder, stamp, slot, material=None, check_cancel=None):
        self.host = host.rstrip("/")
        self.folder = folder
        self.slot = slot
        self.material = material or SLOT_MATERIALS[slot]
        self.material_code, self.temperature, self.center = PROFILES[self.material]
        self.run_name = f"{self.material.lower()}_{slot.lower()}_short{stamp}"
        self.check_cancel = check_cancel
        self.results = {}
        self.wipes = []
        self.pair_times = []
        self.cycles = 0
        self.extruded_mm = 0
        self.feeds = BASE_FEED
        self.capture_label = "sweep"
        self.original_limits = None

    def read(self, route, body=None, timeout=8):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.host + route, data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def send(self, *lines, timeout=90):
        return self.read("/printer/gcode/script", {"script": "\n".join(lines)}, timeout)["result"]

    def status(self):
        return self.read(STATE)["result"]["status"]

    @staticmethod
    def idle(state):
        return (
            state["print_stats"]["state"] in ("standby", "complete", "cancelled")
            and not state["virtual_sdcard"]["is_active"]
            and not state["pause_resume"]["is_paused"]
            and state["webhooks"]["state"] == "ready"
        )

    def check_position(self, state):
        head = state["toolhead"]
        x, y, z, _ = head["position"]
        if head["homed_axes"] != "xyz" or abs(x - 115) > .2 or abs(y - 291.5) > .2 or z < 4:
            raise RuntimeError("The homed nozzle must be at the verified stock purge position.")

    def park_after_purge(self, state):
        head = state["toolhead"]
        if not self.idle(state) or head["homed_axes"] != "xyz" or head["position"][1] <= 250:
            return
        self.send("M400")
        state = self.status()
        head = state["toolhead"]
        if not self.idle(state) or head["homed_axes"] != "xyz" or head["position"][1] <= 250:
            return
        movement = self.read("/printer/objects/query?gcode_move=position,gcode_position")["result"]["status"]["gcode_move"]
        origin = [absolute - logical for absolute, logical in zip(movement["position"], movement["gcode_position"])]
        x, y, z, _ = head["position"]
        height = max(12., z) - origin[2]
        self.send("SAVE_GCODE_STATE NAME=k2_calibration_park")
        try:
            self.send("M220 S100", "G90", f"G1 X{x - origin[0]:.6f} Y{y - origin[1]:.6f} Z{height:.6f} F600",
                      f"G1 Y{130 - origin[1]:.6f} F2400", f"G1 X{130 - origin[0]:.6f} F2400", "M400")
        finally:
            self.send("RESTORE_GCODE_STATE NAME=k2_calibration_park MOVE=0")
        position = self.status()["toolhead"]["position"]
        if abs(position[0] - 130) > .25 or abs(position[1] - 130) > .25:
            raise RuntimeError("Could not verify the toolhead parked at X130 Y130.")

    def check_slot(self, state):
        box = state["box"]
        slot_address = f"T1{self.slot}"
        index = "ABCD".index(self.slot)
        if (box["state"] != "connect" or box["map"][slot_address] != slot_address
                or box["T1"]["material_type"][index] != self.material_code):
            raise RuntimeError(f"Expected {self.material} in connected CFS slot 1{self.slot}.")

    def check_filament(self, state):
        self.check_slot(state)
        if not state[SENSOR]["filament_detected"] or state["box"]["T1"]["filament"] != self.slot:
            raise RuntimeError(f"Expected filament already loaded from slot 1{self.slot}.")

    def wipe(self):
        start = time.monotonic()
        self.send("BOX_NOZZLE_CLEAN", "BOX_GO_TO_EXTRUDE_POS", f"M109 S{self.temperature}", "M400")
        self.check_position(self.status())
        seconds = time.monotonic() - start
        self.wipes.append(seconds)
        print(f"Stock wipe, return, and temperature settle: {seconds:.1f} s", flush=True)

    def group(self, k, count, smooth):
        if self.check_cancel:
            self.check_cancel()
        state = self.status()
        if not self.idle(state):
            raise RuntimeError("The printer is no longer idle.")
        self.check_filament(state)
        if self.cycles:
            self.wipe()
            state = self.status()
        self.check_position(state)
        self.check_filament(state)
        if (not self.idle(state) or abs(state["extruder"]["temperature"] - self.temperature) > 5
                or state["extruder"]["target"] != self.temperature):
            raise RuntimeError("The printer state or sweep temperature changed.")

        start = time.monotonic()
        rows = self.results.setdefault(k, [])
        lines = ["G90", "M83", f"SET_PRESSURE_ADVANCE ADVANCE={k/1000:.3f} SMOOTH_TIME={smooth:.12g}"]
        if self.original_limits is not None:
            self.send(limit_command(CALIBRATION_LIMITS))
            if motion_limits(self.status()) != CALIBRATION_LIMITS:
                raise RuntimeError("The printer could not apply the calibration acceleration limits.")
            lines.extend(("M220 S100", "M221 S100"))
        moves, lengths = extrusion_moves(self.feeds)
        names = []
        for repeat in range(len(rows) + 1, len(rows) + count + 1):
            name = f"{self.run_name}_{self.capture_label}_{k:03d}_{repeat}"
            # A dwell queued after the stroke shifts the capture phase even
            # when stock Klipper waits for room in the motion buffer.
            capture_offset = 31 * (repeat - 1) if self.original_limits is not None else 0
            names.append(name)
            lines.extend((*moves,
                          f"G4 P{capture_offset}", f"STOCK_PA_CAPTURE WINDOWS=22 NAME={name}", "M400",
                          "G1 X115 F120", "M400"))
        if self.original_limits is not None:
            lines.append(limit_command(self.original_limits))
        self.send(*lines)
        self.pair_times.append(time.monotonic() - start)
        self.cycles += count
        self.extruded_mm += count * sum(lengths)
        for name in names:
            filename = f"stock_pa_{name}.json"
            data = self.read(f"/server/files/config/{filename}")
            data["filament_feed_mm_s"] = self.feeds
            if self.original_limits is not None:
                data["calibration_motion"] = dict(xy_accel_mm_s2=CALIBRATION_LIMITS[0],
                                                  accel_to_decel_mm_s2=CALIBRATION_LIMITS[1], smooth_time_s=smooth)
            path = self.folder / filename
            path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            measurement = score(path, controlled=self.original_limits is not None)
            rows.append(measurement)
            if measurement is None:
                print(f"{k/1000:.3f}: insufficient pressure or motion coverage", flush=True)
            else:
                detail = f", signed area {measurement[3]*1000:+.1f} ms" if len(measurement) == 4 else ""
                print(f"{k/1000:.3f}: RMS {measurement[0]:.3f}, balance {measurement[1]:+.3f}{detail}", flush=True)

    def rank(self):
        medians = {}
        for k, rows in self.results.items():
            errors = [row[0] for row in rows if row is not None]
            if len(errors) >= 2:
                medians[k] = statistics.median(errors)
        return sorted(medians.items(), key=lambda item: item[1])

    def replenish_captures(self, k, smooth):
        rows = self.results[k]
        if self.original_limits is None:
            if sum(row is not None for row in rows) == 1:
                self.group(k, 1, smooth)
            return
        while sum(row is not None for row in rows) < 2 and len(rows) < MAX_CAPTURE_ATTEMPTS:
            self.group(k, min(2, MAX_CAPTURE_ATTEMPTS - len(rows)), smooth)

    def adaptive_sweep(self, smooth):
        for k in (self.center - PA_STEP, self.center, self.center + PA_STEP):
            self.group(k, 2, smooth)
        for k in tuple(self.results):
            self.replenish_captures(k, smooth)
        ranking = self.rank()
        if len(ranking) < 2:
            return {"reason": "insufficient_repeats"}
        expansions = 0
        repeated_close_pair = False
        while True:
            (best, first), (second, next_score) = ranking[:2]
            if not repeated_close_pair and next_score - first <= CLOSE_SCORE:
                for k in (best, second):
                    if self.original_limits is None or len(self.results[k]) < MAX_CAPTURE_ATTEMPTS:
                        self.group(k, 1, smooth)
                repeated_close_pair = True
            elif expansions < MAX_EDGE_EXPANSIONS and best in (min(self.results), max(self.results)):
                direction = -1 if best == min(self.results) else 1
                neighbor = best + direction * PA_STEP
                if neighbor <= 0:
                    break
                self.group(neighbor, 2, smooth)
                self.replenish_captures(neighbor, smooth)
                expansions += 1
            else:
                break
            ranking = self.rank()
        (best, first), (second, next_score) = ranking[:2]
        lead = next_score - first
        assessment = {"best_pa": best / 1000, "second_pa": second / 1000,
                      "score_lead": lead, "minimum_lead": CLOSE_SCORE}
        tested = {setting for setting, _ in ranking}
        if best - PA_STEP not in tested or best + PA_STEP not in tested:
            return dict(assessment, reason="unbracketed", tested_min_pa=min(tested) / 1000,
                        tested_max_pa=max(tested) / 1000)
        if lead <= CLOSE_SCORE:
            return dict(assessment, reason="close_scores")
        left = [row[0] for row in self.results[best] if row is not None]
        right = [row[0] for row in self.results[second] if row is not None]
        paired = list(zip(left, right))
        wins = sum(left < right for left, right in paired)
        if wins < 2 or wins * 2 <= len(paired):
            return dict(assessment, reason="inconsistent_repeats")
        if self.original_limits is not None:
            timing_pa = timing_candidate(self.results, best)
            assessment["timing_pa"] = None if timing_pa is None else round(timing_pa, 5)
            if timing_pa is None or abs(timing_pa - best / 1000) > PA_STEP / 2000:
                return dict(assessment, reason="timing_disagreement")
        return dict(assessment, reason="accepted")

    def measure(self, feeds, label, smooth):
        self.feeds, self.capture_label = feeds, label
        self.results.clear()
        assessment = self.adaptive_sweep(smooth)
        return {"assessment": assessment,
                "median_rms": {f"{k/1000:.3f}": round(rms, 4) for k, rms in self.rank()},
                "rejected_captures": sum(row is None for rows in self.results.values() for row in rows)}

    def run(self, preheated=False, max_flow=None, diameter=1.75, update=print, lower_flow=None):
        state = self.status()
        ext = state["extruder"]
        expected_target = self.temperature if preheated else 0
        if (not self.idle(state) or ext["nozzle_diameter"] != .4 or ext["target"] != expected_target
                or state["heater_bed"]["target"] != 0 or not state["k2_stock_pa_capture"]):
            raise RuntimeError("The printer, heaters or 0.4 mm nozzle are not in the expected calibration state.")
        self.check_filament(state)
        self.check_position(state)
        pa, smooth = ext["pressure_advance"], ext["smooth_time"]
        self.original_limits = motion_limits(state)
        if any(not math.isfinite(value) or value <= 0 for value in self.original_limits):
            raise RuntimeError("The printer did not report its acceleration limits.")
        start = time.monotonic()
        self.send("SAVE_GCODE_STATE NAME=pa_short_sweep")
        try:
            self.send(f"M109 S{self.temperature}", "ENABLE_PRESSURE_ADVANCE VALUE=1",
                      "G90", "M83", "G1 E3 F120", "M400", timeout=130)
            prep = time.monotonic() - start
            print(f"Nozzle heat and prime: {prep:.1f} s", flush=True)
            flow_sweeps = None
            feeds = (BASE_FEED,) if max_flow is None else preset_flows(max_flow, diameter, lower_flow)
            area = math.pi * (diameter / 2) ** 2
            if lower_flow is not None:
                update(f"Measuring pressure at the lower-flow target of {lower_flow:.4g} mm³/s.")
            normal = self.measure(feeds[0], "sweep", CALIBRATION_SMOOTH)
            assessment = normal["assessment"]
            if max_flow is not None:
                update(f"Measuring pressure at the preset's {max_flow:g} mm³/s limit.")
                peak = self.measure(feeds[1], "peak", CALIBRATION_SMOOTH)
                flow_sweeps = [dict(normal, peak_mm3_s=round(feeds[0][1] * area, 4)),
                               dict(peak, peak_mm3_s=max_flow)]
                assessment = combine_flows(normal["assessment"], peak["assessment"], flow_sweeps)
            candidate = assessment["best_pa"] if assessment["reason"] == "accepted" else None
            result = {"candidate": candidate, "assessment": assessment,
                      "median_rms": normal["median_rms"], "cycles": self.cycles,
                      "rejected_captures": normal["rejected_captures"] + (peak["rejected_captures"] if flow_sweeps else 0),
                      "calibration_motion": dict(xy_accel_mm_s2=CALIBRATION_LIMITS[0],
                                                 accel_to_decel_mm_s2=CALIBRATION_LIMITS[1], smooth_time_s=CALIBRATION_SMOOTH),
                      "filament_mm": round(3 + self.extruded_mm, 1),
                      "sweep_seconds": round(sum(self.pair_times), 1),
                      "wipe_seconds": round(sum(self.wipes), 1),
                      "wipe_count": len(self.wipes), "prep_seconds": round(prep, 1),
                      "wall_seconds": round(time.monotonic() - start, 1)}
            if flow_sweeps is not None:
                result["flow_sweeps"] = flow_sweeps
            output = self.folder / f"{self.run_name}_summary.json"
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result), flush=True)
        finally:
            if self.idle(self.status()):
                self.send("M104 S0", f"SET_PRESSURE_ADVANCE ADVANCE={pa:.12g} SMOOTH_TIME={smooth:.12g}",
                          "RESTORE_GCODE_STATE NAME=pa_short_sweep MOVE=0", limit_command(self.original_limits))
                state = self.status()
                final = state["extruder"]
                if ((final["target"], final["pressure_advance"], final["smooth_time"]) != (0, pa, smooth)
                        or motion_limits(state) != self.original_limits):
                    raise RuntimeError("Hotend, pressure advance, or acceleration cleanup could not be verified.")
                self.park_after_purge(state)
                print(f"Hotend off. Restored PA {pa:.3f} and smoothing {smooth:.3f}.", flush=True)
            else:
                raise RuntimeError("The printer state changed before PA and heater cleanup could be verified.")
        return result


def combine_flows(normal, peak, flow_sweeps):
    for assessment, band in zip((normal, peak), flow_sweeps, strict=True):
        if assessment["reason"] != "accepted":
            return dict(assessment, tested_flow_mm3_s=band["peak_mm3_s"])
    if normal["best_pa"] != peak["best_pa"]:
        return {"reason": "flow_disagreement", "best_pa": normal["best_pa"],
                "peak_best_pa": peak["best_pa"], "peak_flow_mm3_s": flow_sweeps[1]["peak_mm3_s"]}
    return normal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Moonraker URL for the stock K2.")
    parser.add_argument("--slot", choices=SLOT_MATERIALS, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    Sweep(args.host, args.output, time.strftime("%H%M%S"), args.slot).run()


if __name__ == "__main__":
    main()
