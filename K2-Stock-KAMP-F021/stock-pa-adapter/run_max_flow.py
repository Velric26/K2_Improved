"""Find the K2's servo-current threshold with short, individually measured flow steps."""

import json
import math
from uuid import uuid4

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
# Approximate slopes around the threshold in the archived stock PETG and ABS sweeps.
ESTIMATED_SLOPE = {"PETG": .05, "ABS": .04}
# The 0.6 mm ABS trace rose by about .0225 per mm³/s over 15–39 and .06 near 39.
ABS_06_SLOPE = .0225
ABS_06_REFERENCE = (39, .06)


def probe_ceiling(slot, material, nozzle_mm=.4):
    if slot not in ("A", "B", "C", "D") or material not in ESTIMATED_SLOPE:
        raise ValueError("Auto Max Flow supports ABS or PETG in CFS unit 1.")
    if material == "ABS" and nozzle_mm == .6:
        return ABS_06_CEILING
    reference_slot = "D" if material == "ABS" else ("C" if slot == "C" else "A")
    return MAP_FLOWS[reference_slot][-1]


def search_current(probe, ceiling, slope, reference=None):
    currents = {}

    def measure(flow):
        if flow not in currents:
            currents[flow] = probe(flow)
        return currents[flow]

    def estimate_below(flow, iq, slope, end):
        crossing = flow + (CURRENT_CUTOFF - iq) / slope
        return min(end, max(flow + 1, math.ceil(crossing) - 1))

    first = measure(START_FLOW)
    if first >= CURRENT_CUTOFF:
        flow = START_FLOW - 1
    else:
        initial_limit = reference[0] if reference else ceiling - 1
        flow = estimate_below(START_FLOW, first, slope, initial_limit)
    check_dip = True
    while flow in currents or len(currents) < MAX_PROBES:
        if (len(currents) >= MAX_PROBES - 2 and flow not in currents
                and all(iq < CURRENT_CUTOFF for iq in currents.values())):
            # Reserve the final two probes for the top adjacent pair if the search is still below cutoff.
            top_pair = ceiling - (MAX_PROBES - 1 - len(currents))
            if flow < top_pair and top_pair not in currents:
                flow = top_pair
        iq = measure(flow)
        above = iq >= CURRENT_CUTOFF
        if not above and flow == ceiling:
            return {"reason": "current_below_threshold"}
        if above and flow == MIN_FLOW:
            break
        if not above and reference and flow == reference[0] and len(currents) == 2:
            flow = estimate_below(flow, iq, reference[1], min(flow + 4, ceiling - 1))
            continue

        adjacent = flow - 1 if above else flow + 1
        if adjacent in currents and (currents[adjacent] >= CURRENT_CUTOFF) != above:
            low, high = sorted((flow, adjacent))
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
        flow = adjacent
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
        expanded_abs = self.nozzle_mm == .6 and sweep.material == "ABS"
        slope = ABS_06_SLOPE if expanded_abs else ESTIMATED_SLOPE[sweep.material]
        reference = ABS_06_REFERENCE if expanded_abs else None
        report = {
            "slot": f"1{sweep.slot}", "material": sweep.material, "nozzle_mm": self.nozzle_mm,
            "temperature_c": sweep.temperature, "preset_mm3_s": preset,
            "start_mm3_s": START_FLOW, "planned_min_mm3_s": MIN_FLOW,
            "planned_max_mm3_s": self.ceiling, "tested_through_mm3_s": 0,
            "algorithm": "servo_current_threshold", "search": "adaptive", "step_seconds": STEP_SECONDS,
            "measurement_seconds": 8, "estimated_iq_per_mm3_s": slope, "max_probes": MAX_PROBES,
            "current_cutoff": CURRENT_CUTOFF, "boundary_mm3_s": None, "recommended_mm3_s": None,
            "steps": [], "capture": f"{name}_summary.json",
        }
        if reference:
            report.update(reference_mm3_s=reference[0], reference_iq_per_mm3_s=reference[1])

        def probe(flow):
            return self.probe(report, f"{name}_{len(report['steps']) + 1}", flow)

        try:
            self.update("Lowering the bed and moving the nozzle to the center. Keep the build area clear.")
            move_to_flow_position(sweep)
            sweep.check_cancel()
            sweep.send("SET_PRESSURE_ADVANCE ADVANCE=0", "M400")
            report.update(search_current(probe, self.ceiling, slope, reference))
        except UnusableCurrent as exc:
            report.update(reason="unusable_current", unusable_flow_mm3_s=exc.flow)
        except Exception as exc:
            report.update(reason="interrupted", error=str(exc))
            raise
        finally:
            (sweep.folder / report["capture"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
