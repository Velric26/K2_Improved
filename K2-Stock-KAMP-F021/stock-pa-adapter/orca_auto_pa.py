"""Loopback companion for Orca's stock K2 extrusion calibrations."""

import argparse
import json
import math
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from adaptive_pa_motion import PRINT_CAPABILITY as WALL_CAPABILITY, PRINT_COMMAND as WALL_COMMAND
from run_adaptive_pa import END_RESTORE, chamber_target, grid as wall_grid, measure as measure_walls, validate_coupon
from run_direct_pa_refinement import measure as measure_direct, rank as rank_direct
from run_max_flow import COMMAND as CURRENT_FLOW_COMMAND, HOST_LIMITS, MIN_FLOW, SLOT_SUPPORT, MaxFlowRun, probe_ceiling
from run_short_pa_sweep import MATERIAL_CODES, MOTION, PROFILES, SENSOR, Sweep, limit_command, motion_limits, preset_flows, require_nozzle

PA_PEAK_LIMIT = 24
EXTENDED_PA_SUPPORT = "measured Max Flow peak"  # Capability advertised by the installed adapter.
NOZZLE_RANGE_C = (150, 300)  # Hardware sanity bounds. Orca enforces the filament preset range.
SUPPORTED_NOZZLES_MM = (0.4, 0.6)


class Cancelled(RuntimeError):
    pass


def pa_peak_limit(slot, material, temperature, nozzle_mm):
    return probe_ceiling(slot, material, nozzle_mm)


def unverified_cleanup(sweep, failure, bed_heated):
    """Stop the run if the printer is not idle, keeping the original error and cutting heaters when safe."""
    try:
        state = sweep.status()
    except Exception as error:
        if failure is None:
            raise
        raise type(failure)(f"{failure} Cleanup could not reach the printer ({error}).") from failure
    if sweep.idle(state):
        return
    heaters_off = False
    if state["print_stats"]["state"] != "printing":
        try:
            sweep.send("M104 S0", *(("M140 S0",) if bed_heated else ()))
            heaters_off = True
        except Exception:
            pass
    message = "The printer state changed before cleanup could be verified."
    message += " The heaters were switched off." if heaters_off else " Check the heaters on the printer."
    if failure is not None:
        message = f"{failure} {message}"
    raise (Cancelled if isinstance(failure, Cancelled) else RuntimeError)(message) from failure


def cap_walls(walls, flow):
    capped = {}
    for wall, settings in walls.items():
        settings = dict(settings)
        if settings["flow"] > flow:
            settings["speed"] = settings["speed"] * flow / settings["flow"]
            settings["flow"] = flow
        capped[wall] = settings
    return capped


def check_request(payload, printer_host):
    if not isinstance(payload, dict) or payload.get("printer") != printer_host:
        raise ValueError("The calibration helper is configured for a different K2.")
    slot, material = payload.get("slot"), payload.get("material")
    if slot not in ("1A", "1B", "1C", "1D") or material not in ("ABS", "PETG"):
        raise ValueError("Select an ABS or PETG spool in CFS unit 1.")
    mode = payload.get("kind", "pressure_advance")
    if mode not in ("pressure_advance", "max_flow", "flow_then_pa", "adaptive_pa"):
        raise ValueError("Unknown K2 calibration type.")
    reader = payload.get("reader")
    direct = reader == "continuous"
    if reader is not None and (not direct or mode != "pressure_advance"):
        raise ValueError("Unknown pressure advance reader.")
    if payload.get("nozzle_mm") not in SUPPORTED_NOZZLES_MM:
        sizes = " or ".join(f"{size:g}" for size in SUPPORTED_NOZZLES_MM)
        raise ValueError(f"Select a supported nozzle diameter in Orca ({sizes} mm).")
    temperature = payload.get("temperature_c")
    if type(temperature) is not int or not NOZZLE_RANGE_C[0] <= temperature <= NOZZLE_RANGE_C[1]:
        raise ValueError("Use a nozzle temperature from %d to %d °C." % NOZZLE_RANGE_C)
    if direct or mode in ("max_flow", "flow_then_pa", "adaptive_pa") or any(key in payload for key in
                                ("max_flow_mm3_s", "filament_diameter_mm", "lower_flow_mm3_s")):
        values = (payload.get("max_flow_mm3_s"), payload.get("filament_diameter_mm"))
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Set positive max volumetric speed and filament diameter values in Orca.")
    if "lower_flow_mm3_s" in payload:
        lower_flow = payload["lower_flow_mm3_s"]
        peak = payload["max_flow_mm3_s"]
        if (mode != "pressure_advance" or type(lower_flow) not in (int, float)
                or not math.isfinite(lower_flow) or not 0 < lower_flow <= peak
                or (not direct and lower_flow == peak)):
            raise ValueError("Use positive PA flows in ascending order. Only the continuous reader can use equal peaks.")
    if direct:
        limit = pa_peak_limit(slot[1], material, payload["temperature_c"], payload["nozzle_mm"])
        if payload["max_flow_mm3_s"] > limit:
            raise ValueError(f"Continuous Auto PA supports a peak at or below {limit:g} mm³/s.")
    if mode in ("adaptive_pa", "flow_then_pa"):
        limit = pa_peak_limit(slot[1], material, payload["temperature_c"], payload["nozzle_mm"])
        wall_grid({**payload, "max_flow_mm3_s": limit} if mode == "flow_then_pa" else payload, limit)
        validate_coupon(payload)
    return slot[1], material


def prepare_slot(sweep, state, recover_unassigned=False):
    check_cancel = sweep.check_cancel or (lambda: None)
    sweep.check_slot(state)
    box = state["box"]
    current = box["T1"]["filament"]
    detected = state[SENSOR]["filament_detected"]
    recover = False
    if detected and current not in ("A", "B", "C", "D"):
        recover = (recover_unassigned and current in (None, "None") and box.get("enable") == 1
                   and box.get("filament") == 1)
        if not recover:
            raise RuntimeError("The nozzle detects filament, but the K2 cannot confirm CFS unit 1 as its source.")

    if state["toolhead"]["homed_axes"] != "xyz":
        sweep.send("G28", "M400", timeout=150)
    check_cancel()
    sweep.send("BOX_GO_TO_EXTRUDE_POS", "M400")
    if current != sweep.slot or not detected:
        if detected and not recover:
            sweep.send("BOX_QUIT_MATERIAL", "M400", timeout=200)
            if sweep.status()[SENSOR]["filament_detected"]:
                raise RuntimeError("The filament sensor did not clear after the stock unload.")
        check_cancel()
        address = f"T1{sweep.slot}"
        try:
            if recover:
                # Match the stock load sequence for a nozzle that already contains filament.
                # The stock retract command resolves the existing strand before a new slot is fed.
                sweep.send("BOX_LOAD_MATERIAL_HEATING", "BOX_LOAD_MATERIAL_CUT_MATERIAL",
                           "BOX_LOAD_MATERIAL_RETRUDE_MATERIAL", "M400", timeout=240)
                if sweep.status()[SENSOR]["filament_detected"]:
                    raise RuntimeError("The stock CFS recovery did not clear the nozzle filament sensor.")
                check_cancel()
                sweep.send(f"M109 S{sweep.temperature}", timeout=130)
            else:
                sweep.send("BOX_GO_TO_EXTRUDE_POS", f"M109 S{sweep.temperature}", "BOX_SAVE_FAN", timeout=130)
            sweep.send(f"BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL TNN={address}",
                       f"BOX_LOAD_MATERIAL_MATERIAL_FLUSH TNN={address}", "BOX_LOAD_MATERIAL_END",
                       "M400", timeout=260)
        except Exception:
            if sweep.idle(sweep.status()):
                sweep.send("BOX_RESTORE_FAN")
            raise
    sweep.check_filament(sweep.status())
    check_cancel()
    sweep.send(f"M109 S{sweep.temperature}", "BOX_NOZZLE_CLEAN",
               "BOX_GO_TO_EXTRUDE_POS", f"M109 S{sweep.temperature}", "M400", timeout=130)


def run_calibration(host, output, request, cancel, update):
    slot, material = check_request(request, urlsplit(host).hostname)
    nozzle_mm = request["nozzle_mm"]
    max_flow = request.get("max_flow_mm3_s")
    diameter = request.get("filament_diameter_mm")
    lower_flow = request.get("lower_flow_mm3_s")
    find_max_flow = request.get("kind") == "max_flow"
    adaptive = request.get("kind") == "adaptive_pa"
    chamber = request.get("chamber_temperature_c", 0) if adaptive else 0
    original_chamber = None
    direct = request.get("reader") == "continuous"
    outer_only = direct and lower_flow is None
    extended_stroke = direct and max_flow > PA_PEAK_LIMIT

    def check_cancel():
        if cancel.is_set():
            raise Cancelled("Calibration canceled.")

    stamp = time.strftime("%H%M%S")
    sweep = Sweep(host, output, stamp, slot, material, check_cancel)
    flow_run = None
    sweep.temperature = int(request["temperature_c"])
    state = sweep.status()
    ext = state["extruder"]
    require_nozzle(state, nozzle_mm)
    if not sweep.idle(state) or ext["target"] != 0 or state["heater_bed"]["target"] != 0:
        raise RuntimeError("The K2 must be idle with the hotend and bed heaters off.")
    if adaptive:
        commands = sweep.read("/printer/gcode/help")["result"]
        if commands.get(WALL_COMMAND) != WALL_CAPABILITY or "STOCK_PA_CAPTURE" not in commands:
            raise RuntimeError("Install the K2 printed-wall Adaptive PA adapter before starting this test.")
        if not {"_BMC_KAMP_INNER", "EXCLUDE_OBJECT_DEFINE", "BED_MESH_CLEAR"} <= commands.keys():
            raise RuntimeError("Adaptive PA needs KAMP and object definitions for tower bed probing.")
        if "START_PRINT" not in commands:
            raise RuntimeError("Adaptive PA runs the printer's START_PRINT macro, which this K2 does not have.")
        original_corner = sweep.read("/printer/objects/query?toolhead=square_corner_velocity")[
            "result"]["status"]["toolhead"]["square_corner_velocity"]
        if chamber:
            original_chamber = chamber_target(sweep)
            if original_chamber is None or "SET_HEATER_TEMPERATURE" not in commands:
                raise RuntimeError("Enable the K2 chamber heater or turn off chamber heating in the filament preset.")
        conditions = wall_grid(request, pa_peak_limit(slot, material, sweep.temperature, nozzle_mm))
    elif direct or find_max_flow:
        commands = sweep.read("/printer/gcode/help")["result"]
        command = CURRENT_FLOW_COMMAND if find_max_flow else "STOCK_PA_DIRECT_PA_STROKE"
        if command not in commands or "STOCK_PA_CAPTURE" not in commands:
            feature = "Auto Max Flow" if find_max_flow else "Auto PA"
            raise RuntimeError(f"The printer needs the continuous {feature} adapter ({command}).")
        if find_max_flow and f"minimum {MIN_FLOW} mm3/s" not in commands[command]:
            raise RuntimeError("Update the printer's Auto Max Flow adapter to enable below-threshold probes.")
        if find_max_flow and SLOT_SUPPORT not in commands[command]:
            raise RuntimeError("Update the printer's Auto Max Flow adapter for the current CFS slot support.")
        flow_command = commands[command] if find_max_flow else commands.get("STOCK_PA_DIRECT_STROKE", "")
        if HOST_LIMITS not in flow_command:
            raise RuntimeError("Update the printer adapter. Calibration limits now come from the PC companion.")
        if extended_stroke:
            stroke = commands.get("STOCK_PA_DIRECT_STROKE", "")
            if EXTENDED_PA_SUPPORT not in stroke:
                raise RuntimeError(f"The printer adapter must support Auto PA above {PA_PEAK_LIMIT:g} mm³/s.")
    elif not state["k2_stock_pa_capture"]:
        raise RuntimeError("The stock pressure adapter has no capture yet.")
    if direct or adaptive:
        original_limits = motion_limits(state)
        direct_report = {"slot": f"1{slot}", "material": material, "nozzle_mm": nozzle_mm,
                         "temperature_c": sweep.temperature,
                         "flows": {"PEAK": max_flow} if outer_only else {"LOWER": lower_flow, "PEAK": max_flow},
                         "prime_mm_per_stroke": 3.,
                         "captures": [], "completed": False}
        if adaptive:
            direct_report = {"slot": f"1{slot}", "material": material, "nozzle_mm": nozzle_mm,
                             "temperature_c": sweep.temperature, "conditions": [], "completed": False}
    sweep.check_slot(state)
    if max_flow is not None:
        settings = sweep.read("/printer/objects/query?configfile=settings")
        extruder = settings["result"]["status"]["configfile"]["settings"]["extruder"]
        if adaptive:
            machine = settings["result"]["status"]["configfile"]["settings"]["printer"]
            if (not math.isclose(diameter, extruder["filament_diameter"], rel_tol=.005)
                    or any(point["flow"] / point["speed"] > extruder["max_extrude_cross_section"]
                           or point["speed"] > machine["max_velocity"] or point["acceleration"] > machine["max_accel"]
                           for point in conditions)):
                raise RuntimeError("Adaptive PA targets exceed the configured movement or extrusion limits.")
        elif find_max_flow:
            flow_run = MaxFlowRun(sweep, diameter, update, extruder, nozzle_mm=nozzle_mm)
        else:
            firmware_area = math.pi * (extruder["filament_diameter"] / 2) ** 2
            firmware_limit = extruder["max_extrude_cross_section"]
            bands = preset_flows(max_flow, diameter, lower_flow)
            if outer_only:
                bands = bands[-1:]
            if any(speed * firmware_area / (move[1] / 60) > firmware_limit
                   for band in bands for speed, move in zip(band, MOTION, strict=True)):
                raise RuntimeError("A PA flow target exceeds the firmware's extrusion limit for this test.")
            if extended_stroke and (
                    not math.isclose(diameter, extruder["filament_diameter"], rel_tol=.005)
                    or any(speed > extruder["max_extrude_only_velocity"]
                           or speed * duration > extruder["max_extrude_only_distance"]
                           for band in bands
                           for speed, (_, _, duration) in zip(band, MOTION, strict=True))):
                raise RuntimeError("The requested PA peak exceeds the printer's filament diameter or extrusion-only limits.")
    check_cancel()
    pa, smooth = ext["pressure_advance"], ext["smooth_time"]
    sweep.send("SAVE_GCODE_STATE NAME=orca_pa_calibration")
    try:
        update("Preparing the toolhead, loading filament and wiping the nozzle.")
        prepare_slot(sweep, state, recover_unassigned=direct or adaptive or find_max_flow)
        check_cancel()
        if find_max_flow:
            result = flow_run.run(max_flow)
        elif adaptive:
            points = measure_walls(sweep, conditions, stamp, direct_report, request, update)
            result = {"candidate": None, "points": points,
                      "complete_grid": len(points) == len(conditions) and all(p["pa"] is not None for p in points)}
            direct_report["completed"] = True
        elif direct:
            measure_direct(sweep, direct_report["flows"], stamp, direct_report, update=update,
                           diameter=diameter, extended_stroke=extended_stroke)
            flows = direct_report["flows"]
            rows, winner, limits, tie = rank_direct(direct_report["captures"], bands=tuple(flows))
            if len(rows) < 2:
                winner, reason = None, "insufficient_repeats"
            else:
                reason = "accepted" if winner else "lower_flow_limits"
            candidate = winner["pa"] if winner else None
            result = {"candidate": candidate, "assessment": {"reason": reason},
                      "flow_sweeps": [{"peak_mm3_s": flow} for flow in flows.values()],
                      "scores": rows, "selected_scores": winner["scores"] if winner else None,
                      "lower_limits": limits, "peak_tie": tie}
            if outer_only:
                result["outer_wall_flow_mm3_s"] = max_flow
            direct_report.update(result)
            direct_report["completed"] = True
        elif max_flow is None:
            update("Measuring pressure and wiping the nozzle between pairs.")
            result = sweep.run(preheated=True, nozzle_mm=nozzle_mm)
        else:
            update("Measuring pressure and wiping the nozzle between pairs.")
            result = sweep.run(preheated=True, max_flow=max_flow, diameter=diameter, update=update,
                               lower_flow=lower_flow, nozzle_mm=nozzle_mm)
        check_cancel()
    finally:
        failure = sys.exc_info()[1]
        unverified_cleanup(sweep, failure, adaptive)
        cleanup = ["M104 S0"]
        restore_chamber = False
        if adaptive:
            cleanup.append("M140 S0")
            if chamber and original_chamber != chamber and chamber_target(sweep) == chamber:
                cleanup.append(f"SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET={original_chamber:g}")
                restore_chamber = True
            if direct_report.get("coupon_started") and sweep.status()["toolhead"]["homed_axes"] == "xyz":
                cleanup.extend(("G91", "G1 Z2 F600", "G90", "M400"))
            if direct_report.get("start_print"):
                cleanup.extend(END_RESTORE)
            cleanup.append(f"SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY={original_corner:g}")
        if find_max_flow:
            cleanup.append("M400")
        if direct or adaptive:
            cleanup.append(limit_command(original_limits))
        sweep.send(*cleanup, f"SET_PRESSURE_ADVANCE ADVANCE={pa:.12g} SMOOTH_TIME={smooth:.12g}",
                   "RESTORE_GCODE_STATE NAME=orca_pa_calibration MOVE=0")
        final_state = sweep.status()
        final = final_state["extruder"]
        if (final["target"], final["pressure_advance"], final["smooth_time"]) != (0, pa, smooth):
            raise RuntimeError("Could not verify nozzle shutdown and restoration of the original pressure advance.")
        if restore_chamber and chamber_target(sweep) != original_chamber:
            raise RuntimeError("Could not verify restoration of the original chamber target.")
        sweep.park_after_purge(final_state)
        if (((direct or adaptive) and motion_limits(final_state) != original_limits)
                or ((direct or adaptive or find_max_flow) and final_state["heater_bed"]["target"])):
            raise RuntimeError("Could not verify the original motion limits and bed heater state.")
        if ((direct or adaptive) and (direct_report.get("captures") or direct_report.get("conditions"))
                or (flow_run and flow_run.started_capture)):
            sweep.send(f"STOCK_PA_CAPTURE WINDOWS=4 NAME=orca_direct_handoff_{stamp}")
            final_state = sweep.status()
            sweep.check_filament(final_state)
            if final_state["toolhead"]["homed_axes"] != "xyz" or not final_state["k2_stock_pa_capture"]["samples"]:
                raise RuntimeError("Could not verify homing and the stock reader after calibration.")
            if direct or adaptive:
                direct_report["stock_handoff"] = final_state["k2_stock_pa_capture"]
        if direct or adaptive:
            suffix = "adaptive" if adaptive else "direct"
            report_path = sweep.folder / f"{sweep.run_name}_{suffix}_summary.json"
            report_path.write_text(json.dumps(direct_report, indent=2), encoding="utf-8")
            if direct_report["completed"]:
                result["report_file"] = str(report_path.resolve())

    result.update(slot=f"1{slot}", material=material, nozzle_mm=nozzle_mm, temperature_c=sweep.temperature)
    if not find_max_flow and not adaptive and result["candidate"] is not None:
        record = {"schema": 1, "slot": result["slot"], "material": material,
                  "material_code": sweep.material_code, "nozzle_mm": nozzle_mm,
                  "temperature_c": sweep.temperature, "pressure_advance": result["candidate"],
                  "validation": "pressure_sweep", "source": f"{sweep.run_name}_summary.json"}
        if direct:
            record.update(validation="continuous_pressure_sweep", source=Path(result["report_file"]).name)
        if max_flow is not None:
            record.update(max_flow_mm3_s=max_flow, filament_diameter_mm=diameter)
        if lower_flow is not None:
            record["lower_flow_mm3_s"] = lower_flow
        if outer_only:
            record["outer_wall_flow_mm3_s"] = max_flow
        key = f"slot_{slot.lower()}_{material.lower()}"
        if nozzle_mm != 0.4:
            key = f"{key}_{nozzle_mm:g}mm"
        sweep.read("/server/database/item", {"namespace": "k2_pa_calibration", "key": key, "value": record})
    return result


def run_job(host, output, request, cancel, update):
    if request.get("kind") != "flow_then_pa":
        return run_calibration(host, output, request, cancel, update)

    update("Step 1 of 2. Measuring max flow.")
    flow_request = {**request, "kind": "max_flow"}
    flow = run_calibration(host, output, flow_request, cancel, lambda text: update("Max Flow · " + text))
    result = {"max_flow": flow, "adaptive_pa": None, "pa_state": "skipped",
              "pa_message": "Adaptive PA was skipped because Max Flow found no verified cutoff."}
    measured = flow.get("recommended_mm3_s")
    if measured is None:
        return result

    walls = cap_walls(request["walls"], measured)
    adaptive_request = {**request, "kind": "adaptive_pa", "max_flow_mm3_s": measured, "walls": walls}
    result["walls"] = walls
    if cancel.is_set():
        result.update(pa_state="canceled", pa_message="Adaptive PA was canceled before starting.")
        return result

    update(f"Step 2 of 2. Measuring Adaptive PA with wall flow capped at {measured:.4g} mm³/s.")
    try:
        adaptive = run_calibration(host, output, adaptive_request, cancel, lambda text: update("Adaptive PA · " + text))
    except Cancelled as error:
        result.update(pa_state="canceled", pa_message=str(error))
    except Exception as error:
        result.update(pa_state="failed", pa_message=str(error))
    else:
        result.update(adaptive_pa=adaptive, pa_state="complete", pa_message="")
    return result


def calibration_setup(host, output):
    status = Sweep(host, output, "slots", "A").read(
        "/printer/objects/query?box&extruder=nozzle_diameter")["result"]["status"]
    box = status["box"]
    nozzle_mm = status["extruder"]["nozzle_diameter"]
    if box["state"] != "connect":
        raise RuntimeError("CFS unit 1 is disconnected.")
    unit = box["T1"]
    materials = {code: name for name, codes in MATERIAL_CODES.items() for code in codes}
    slots = []
    for index, letter in enumerate("ABCD"):
        address = f"1{letter}"
        if box["map"].get("T" + address) != "T" + address:
            continue
        material = materials.get(unit["material_type"][index])
        if material is None:
            continue
        limit = pa_peak_limit(letter, material, None, nozzle_mm)
        slots.append({"slot": address, "material": material, "loaded": unit["filament"] == letter,
                      "max_flow": True, "pa_peak_limit_mm3_s": limit})
    return {"slots": slots, "nozzle_mm": nozzle_mm}


class CalibrationServer(ThreadingHTTPServer):
    def __init__(self, port, host, output):
        super().__init__(("127.0.0.1", port), Handler)
        self.host = host
        self.output = output
        self.lock = threading.Lock()
        self.jobs = {}
        self.active_id = None
        self.cancel = threading.Event()

    def start_calibration(self, payload):
        check_request(payload, urlsplit(self.host).hostname)
        with self.lock:
            if self.active_id is not None:
                raise RuntimeError("A calibration is already running.")
            self.cancel.clear()
            job_id = uuid4().hex
            job = {"id": job_id, "state": "running", "message": "Checking the K2."}
            self.jobs[job_id] = job
            if len(self.jobs) > 8:
                del self.jobs[next(iter(self.jobs))]
            self.active_id = job_id
            threading.Thread(target=self.work, args=(job_id, payload), daemon=False).start()
            return dict(job)

    def work(self, job_id, payload):
        def update(message):
            with self.lock:
                self.jobs[job_id]["message"] = message

        try:
            result = run_job(self.host, self.output, payload, self.cancel, update)
            outcome = {"state": "complete", "result": result}
        except Cancelled as error:
            outcome = {"state": "canceled", "message": str(error)}
        except Exception as error:
            outcome = {"state": "failed", "message": str(error)}
        with self.lock:
            self.jobs[job_id].update(outcome)
            self.active_id = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Orca polls job status every second. Log only requests that change something or fail.
        if self.command == "GET" and str(args[1] if len(args) > 1 else "").startswith("2"):
            return
        super().log_message(format, *args)

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/calibrations":
            self.reply(404, {"message": "Unknown calibration endpoint."})
            return
        if self.headers.get("Origin") or self.headers.get_content_type() != "application/json":
            self.reply(403, {"message": "Send calibration requests from Orca."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2048:
                raise ValueError("Invalid calibration request length.")
            payload = json.loads(self.rfile.read(size))
            job = self.server.start_calibration(payload)
        except ValueError as error:
            self.reply(400, {"message": str(error)})
        except RuntimeError as error:
            self.reply(409, {"message": str(error)})
        else:
            self.reply(202, job)

    def job_response(self, cancel=False):
        prefix = "/calibrations/"
        job_id = self.path[len(prefix):] if self.path.startswith(prefix) else ""
        with self.server.lock:
            job = self.server.jobs.get(job_id)
            if job is None:
                response = None
            else:
                if cancel and self.server.active_id == job_id:
                    self.server.cancel.set()
                response = dict(job)
        if response is None:
            self.reply(404, {"message": "Calibration not found."})
        else:
            self.reply(200, response)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"service": "k2-auto-pa", "printer": urlsplit(self.server.host).hostname,
                             "preset_flow": True, "outer_wall_flow": True, "direct_pa_refinement": True,
                             "pa_timing": True,
                             "max_flow_calibration": True, "adaptive_current_max_flow": True,
                             "bracketed_current_max_flow": True, "combined_calibration": True,
                             "combined_pa_measured_peak": True,
                             "standalone_pa_uses_preset_flow": True,
                             "standalone_pa_uses_wall_flows": True,
                             "standalone_pa_outer_wall_only": True,
                             "adaptive_pa": True,
                             "printed_adaptive_pa": True,
                             "adaptive_pa_profile_preheat": True,
                             "profile_temperature_range": True,
                             "max_flow_then_adaptive_pa": True,
                             "supported_nozzles_mm": SUPPORTED_NOZZLES_MM,
                             "pa_peak_limit_mm3_s": PA_PEAK_LIMIT})
            return
        if self.path == "/slots":
            try:
                setup = calibration_setup(self.server.host, self.server.output)
            except (KeyError, IndexError, OSError, RuntimeError, ValueError) as error:
                self.reply(503, {"message": str(error)})
            else:
                self.reply(200, setup)
            return
        self.job_response()

    def do_DELETE(self):
        if self.headers.get("Origin"):
            self.reply(403, {"message": "Cancel calibrations from Orca."})
            return
        self.job_response(cancel=True)


STALL_TICK_S = .25
STALL_REPORT_S = 1.5


def watch_for_stalls(tick=STALL_TICK_S, report=STALL_REPORT_S, clock=time.monotonic, sleep=time.sleep,
                     log=lambda text: print(text, file=sys.stderr, flush=True), rounds=None):
    """Log when this process stops running for a while (GIL hog or OS throttling), which delays Orca's polls."""
    while rounds is None or rounds > 0:
        before = clock()
        sleep(tick)
        late = clock() - before - tick
        if late >= report:
            log(f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] Companion stalled for {late:.1f} s.")
        if rounds is not None:
            rounds -= 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Moonraker URL for the stock K2.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with CalibrationServer(args.port, args.host, args.output) as server:
        print(f"Orca Auto PA listening on 127.0.0.1:{args.port}", flush=True)
        threading.Thread(target=watch_for_stalls, daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
