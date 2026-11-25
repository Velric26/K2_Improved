"""Find the K2's servo-current threshold with short, individually measured flow steps."""

import json
import math
from uuid import uuid4

from flow_model import FlowModel, cutoff, fit_line
from run_sustained_flow import MAP_FLOWS, analyze_map, check_extrusion_limits, move_to_flow_position


COMMAND = "STOCK_PA_DIRECT_CURRENT_FLOW"
SLOT_SUPPORT = "all CFS slots"
START_FLOW = 15
MIN_FLOW = 8
STEP_SECONDS = 16
CURRENT_CUTOFF = 1.0
MAX_PROBES = 8
ABS_06_CEILING = 48
# Capability text the printer adapter advertises once it takes its flow limits from this companion.
HOST_LIMITS = "host-defined limits"
# Without a model, the second probe goes this far above the first so the line has a usable span.
NO_MODEL_STEP = 10
MIN_FIT_SPREAD = 5
MODEL_FILE = "flow_model.json"


def probe_ceiling(slot, material, nozzle_mm=.4):
    if slot not in ("A", "B", "C", "D") or material not in ("ABS", "PETG"):
        raise ValueError("Auto Max Flow supports ABS or PETG in CFS unit 1.")
    if material == "ABS" and nozzle_mm == .6:
        return ABS_06_CEILING
    reference_slot = "D" if material == "ABS" else ("C" if slot == "C" else "A")
    return MAP_FLOWS[reference_slot][-1]


def search_current(probe, ceiling, prior=None):
    """Find the adjacent flow pair where the median servo current reaches the cutoff.

    prior is the model's (intercept, slope) for this nozzle and temperature, or None. Each next
    probe aims just below the predicted crossing. With two or more probes the run's own line is
    used. With one probe, the model slope through that probe is used, and without a model the
    second probe is NO_MODEL_STEP higher so two points can set the line.
    """
    currents = {}

    def measure(flow):
        if flow not in currents:
            currents[flow] = probe(flow)
        return currents[flow]

    def is_above(flow):
        return currents[flow] >= CURRENT_CUTOFF

    def bracket():
        above = [f for f in currents if is_above(f)]
        if above and min(above) - 1 in currents and not is_above(min(above) - 1):
            return min(above) - 1, min(above)
        return None

    def predicted():
        points = sorted(currents.items())
        line = None
        if len(points) >= 2 and points[-1][0] - points[0][0] >= MIN_FIT_SPREAD:
            line = fit_line(points)
        if line is None and prior is not None:
            flow, iq = min(points, key=lambda point: abs(point[1] - CURRENT_CUTOFF))
            line = (iq - prior[1] * flow, prior[1])
        return cutoff(*line) if line else None

    def next_flow():
        above = [f for f in currents if is_above(f)]
        hi = min(above, default=ceiling + 1)
        lo = max((f for f in currents if not is_above(f) and f < hi), default=MIN_FLOW - 1)
        if lo in currents and hi in currents:
            # Both sides are measured: interpolate between them (the run's line overshoots near the top).
            estimate = lo + (CURRENT_CUTOFF - currents[lo]) * (hi - lo) / (currents[hi] - currents[lo])
        else:
            estimate = predicted()
        target = START_FLOW + NO_MODEL_STEP if estimate is None else math.ceil(round(estimate, 6)) - 1
        flow = min(max(target, lo + 1), hi - 1, ceiling)
        if not above and len(currents) >= MAX_PROBES - 2:
            # Keep the last two probes for the top adjacent pair while every probe is below cutoff.
            flow = max(flow, ceiling - (MAX_PROBES - 1 - len(currents)))
        if flow in currents:
            flow = lo + 1 if lo + 1 not in currents else hi - 1
        if flow in currents or not MIN_FLOW <= flow <= ceiling:
            return None
        return flow

    flow = START_FLOW
    check_dip = True
    while True:
        iq = measure(flow)
        above = iq >= CURRENT_CUTOFF
        if not above and flow == ceiling:
            return {"reason": "current_below_threshold"}
        if above and flow == MIN_FLOW:
            break
        pair = bracket()
        if pair:
            low, high = pair
            # A single below-threshold dip separated two hits in the slot A archive.
            # Check one step below the bracket when the budget allows it.
            lookback = low - 1
            if check_dip and lookback >= MIN_FLOW and (lookback in currents or len(currents) < MAX_PROBES):
                check_dip = False
                if measure(lookback) >= CURRENT_CUTOFF:
                    flow = lookback
                    continue
            return {"reason": "current_cutoff", "boundary_mm3_s": high, "recommended_mm3_s": high,
                    "boundary_median_abs_iq": currents[high],
                    "below_cutoff_mm3_s": low, "below_cutoff_median_abs_iq": currents[low]}
        flow = next_flow()
        if flow is None or len(currents) >= MAX_PROBES:
            break
    return {"reason": "unbracketed_current"}


class UnusableCurrent(ValueError):
    def __init__(self, flow):
        super().__init__(f"The motor-current probe at {flow} mm³/s was unusable.")
        self.flow = flow


class MaxFlowRun:
    def __init__(self, sweep, diameter, update, extruder, *, nozzle_mm=.4):
        self.sweep = sweep
        self.diameter = diameter
        self.update = update
        self.nozzle_mm = nozzle_mm
        self.ceiling = probe_ceiling(sweep.slot, sweep.material, nozzle_mm)
        check_extrusion_limits(diameter, extruder, self.ceiling, STEP_SECONDS)
        self.started_capture = False

    def probe(self, report, name, flow):
        sweep = self.sweep
        sweep.check_cancel()
        self.update(f"Probing {flow} mm³/s for {STEP_SECONDS} seconds. Cancel takes effect after this step.")
        filename = f"stock_pa_direct_{name}.json"
        self.started_capture = True
        sweep.send(f"{COMMAND} NAME={name} FLOW={flow} SLOT={sweep.slot} MATERIAL={sweep.material}"
                   f" TEMPERATURE={sweep.temperature} DIAMETER={self.diameter:.8f}", timeout=45)
        data = sweep.read(f"/server/files/config/{filename}", timeout=15)
        (sweep.folder / filename).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        sweep.check_cancel()
        if data.get("mode") != "servo_current":
            raise UnusableCurrent(flow)
        try:
            measurements = analyze_map(data, (flow,), STEP_SECONDS, include_pressure=False)
        except (KeyError, ValueError, TypeError) as exc:
            raise UnusableCurrent(flow) from exc
        row, = measurements["steps"]
        row.update(capture=filename, motor_missed=measurements["motor_missed"])
        report["steps"].append(row)
        report["tested_through_mm3_s"] = max(report["tested_through_mm3_s"], flow)
        iq = row["motor_median_abs_iq"]
        if (not row["motion_verified"] or row["motor_status"] != [0]
                or iq is None or not math.isfinite(iq)):
            raise UnusableCurrent(flow)
        return iq

    def run(self, preset):
        sweep = self.sweep
        name = f"maxflow_{uuid4().hex[:12]}"
        model = FlowModel(sweep.folder / MODEL_FILE)
        prior = model.predict(sweep.material, self.nozzle_mm, sweep.temperature)
        report = {
            "slot": f"1{sweep.slot}", "material": sweep.material, "nozzle_mm": self.nozzle_mm,
            "temperature_c": sweep.temperature, "preset_mm3_s": preset,
            "start_mm3_s": START_FLOW, "planned_min_mm3_s": MIN_FLOW,
            "planned_max_mm3_s": self.ceiling, "tested_through_mm3_s": 0,
            "algorithm": "servo_current_threshold", "search": "adaptive", "step_seconds": STEP_SECONDS,
            "measurement_seconds": 8, "max_probes": MAX_PROBES,
            "current_cutoff": CURRENT_CUTOFF, "boundary_mm3_s": None, "recommended_mm3_s": None,
            "steps": [], "capture": f"{name}_summary.json",
        }
        if prior:
            report["prediction"] = {"source": "model", "intercept": prior[0], "iq_per_mm3_s": prior[1],
                                    "cutoff_mm3_s": cutoff(*prior)}
        else:
            report["prediction"] = {"source": "two_point"}

        measured = []

        def probe(flow):
            iq = self.probe(report, f"{name}_{len(measured) + 1}", flow)
            measured.append((flow, iq))
            return iq

        try:
            self.update("Lowering the bed and moving the nozzle to the center. Keep the build area clear.")
            move_to_flow_position(sweep)
            sweep.check_cancel()
            sweep.send("SET_PRESSURE_ADVANCE ADVANCE=0", "M400")
            report.update(search_current(probe, self.ceiling, prior))
        except UnusableCurrent as exc:
            report.update(reason="unusable_current", unusable_flow_mm3_s=exc.flow)
        except Exception as exc:
            report.update(reason="interrupted", error=str(exc))
            raise
        finally:
            (sweep.folder / report["capture"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
        if report["reason"] in ("current_cutoff", "unbracketed_current", "current_below_threshold"):
            line = model.add_run(sweep.material, self.nozzle_mm, sweep.temperature, measured)
            if line:
                report["measured_line"] = {"intercept": line[0], "iq_per_mm3_s": line[1]}
                (sweep.folder / report["capture"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
