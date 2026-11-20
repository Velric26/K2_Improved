# Stationary pressure-reader prototype for Creality's stock K2 Klipper.
# Distributed under the GNU GPLv3 license.
import json
import logging
import math
import os
import re
import statistics
import struct
from collections import deque
from pathlib import Path
from typing import NamedTuple

from .k2_stock_pa_zip import unzip_data


START = ("start_prtouch_pres oid=%c cfg_regs=%c acq_tick=%u ned_tftr=%c "
         "ned_hftr=%c ned_lftr=%u min_hold=%i max_hold=%i add_hold=%i lmt_dead=%u")
STOP = "stop_prtouch_pres oid=%c sta_swap=%c"
ACK = "ack_prtouch oid=%c err=%c expar0=%u expar1=%u"
PRESSURE = "resault_prtouch_pres"
GAP_SECONDS = .012
SENT = re.compile(r"^Sent \d+ (.+)$")
STROKE_PROFILES = {"PETG": ("000003", (230, 240)), "ABS": ("000004", (280,))}
PA_STROKE_LIMIT = 24.
MAX_TEST_FLOW = 32
SUSTAIN_SECONDS = 120
BED_CENTER = 130.
SERIES_FLOWS = (20, 30, 40)
SERIES_SECONDS = 60
SERIES_REST_SECONDS = 10
MAP_FLOWS = {
    ("D", "000004", 280): tuple(range(25, 41)),
    ("C", "000003", 240): tuple(range(15, 31)),
    ("B", "000003", 240): tuple(range(19, 30)),
    ("A", "000003", 240): tuple(range(19, 30)),
}
MAP_SECONDS = 20
CURRENT_PROBE_SECONDS = 16
CURRENT_PROBE_MIN_FLOW = 8
STOCK_E_BRIDGE_OID = 1  # Observed in the F021 stock extruder protection replies.
MOTOR_CURRENT_QUERY = bytes.fromhex("f7 81 04 00 08 08 c8")


class PressureProtocol(NamedTuple):
    start: tuple
    stop: tuple
    window_seconds: float


def sent_frames(serial):
    frames = [SENT.match(line) for line in serial.dump_debug().splitlines()]
    return [match.group(1) for match in frames if match]


def observed_pressure_commands(frames):
    formats = {spec.split()[0]: spec for spec in (START, STOP)}
    commands = []
    for frame in frames:
        if "read_prtouch_pres " in frame:
            raise ValueError("The stock sampler did not use unsolicited pressure replies.")
        header, _, payload = frame.partition(": ")
        for part in payload.split(", "):
            words = part.split()
            if not words or words[0] not in formats:
                continue
            values = dict(word.split("=", 1) for word in words[1:])
            names = [word.split("=", 1)[0] for word in formats[words[0]].split()[1:]]
            args = tuple(int(values[name], 0) for name in names)
            commands.append((words[0], args, float(header.split()[1])))
    if [command for command, _, _ in commands] != ["start_prtouch_pres", "stop_prtouch_pres"]:
        raise ValueError("The stock capture did not show one start and one final stop.")
    (_, start, started_at), (_, stop, stopped_at) = commands
    if start[0] != stop[0]:
        raise ValueError("The observed stock start and stop address different pressure sensors.")
    duration = stopped_at - started_at
    if not .04 <= duration <= .25:
        raise ValueError("The observed stock acquisition duration was outside the pilot range.")
    return PressureProtocol(start, stop, duration)


def decode_samples(packets, samples, expand_tick=None):
    before = len(samples)
    for _ in range(len(packets)):
        tick_bytes, data_bytes = packets.popleft()
        _, ticks = unzip_data(tick_bytes, signed=False)
        _, values = unzip_data(data_bytes)
        if len(ticks) != len(values):
            raise ValueError("The nozzle MCU returned mismatched sample and tick counts.")
        samples.update(((expand_tick(tick) if expand_tick else tick), value)
                       for tick, value in zip(ticks, values) if tick)
    return len(samples) - before


def timing_stats(samples):
    intervals = [later[0] - earlier[0] for earlier, later in zip(samples, samples[1:])]
    if not intervals:
        raise ValueError("The direct reader returned too few unique timestamped samples.")
    gaps = [value * 1000 for value in intervals if value > GAP_SECONDS]
    return {"count": len(samples), "span_s": round(samples[-1][0] - samples[0][0], 4),
            "median_interval_ms": round(1000 * statistics.median(intervals), 4),
            "max_interval_ms": round(1000 * max(intervals), 3), "gaps_over_12_ms": len(gaps),
            "median_gap_ms": round(statistics.median(gaps), 3) if gaps else None}


def motor_current_reply(data):
    for start in range(len(data) - 9):
        frame = data[start:start + 10]
        if frame[:3] != b"\xf7\x81\x07" or frame[4] != 8:
            continue
        crc = 0
        for byte in frame[2:-1]:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ (0x07 if crc & 0x80 else 0)) & 0xff
        if crc == frame[-1]:
            value, = struct.unpack("<f", frame[5:9])
            if math.isfinite(value) and abs(value) < 100:
                return value, frame[3]
    return None


class StockExtruderCurrent:
    def __init__(self, mcu, reactor):
        self.mcu, self.reactor = mcu, reactor
        self.query = mcu.lookup_query_command(
            "transparent_send oid=%c write=%*s timeout_ms=%u", "transparent_response oid=%c read=%*s",
            oid=STOCK_E_BRIDGE_OID)
        self.samples = []
        self.missed = 0

    def poll(self, eventtime):
        serial = self.mcu._serial
        with serial.lock:
            busy = serial.handlers.get(("transparent_response", STOCK_E_BRIDGE_OID)) is not None
        if busy:
            self.missed += 1
            return eventtime + .25
        try:
            # The stock bridge can return before the motor's response arrives.
            self.query.send([STOCK_E_BRIDGE_OID, b"", 2])
            packet = bytes(self.query.send([STOCK_E_BRIDGE_OID, MOTOR_CURRENT_QUERY, 25])["read"])
            result = motor_current_reply(packet)
            if result is None:
                packet += bytes(self.query.send([STOCK_E_BRIDGE_OID, b"", 25])["read"])
                result = motor_current_reply(packet)
        except Exception:
            logging.warning("Stock E motor current query failed", exc_info=True)
            result = None
        if result is None:
            self.missed += 1
        else:
            current, status = result
            self.samples.append([self.mcu.estimated_print_time(eventtime), current, status])
        return max(eventtime + .25, self.reactor.monotonic() + .03)


class StockDirectPressure:
    def __init__(self, printer):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.gcode = printer.lookup_object("gcode")
        self.gcode.register_command("STOCK_PA_DIRECT_TEST", self.cmd_TEST,
                                    desc="Benchmark the stock nozzle MCU pressure buffer with the printer stationary")
        self.gcode.register_command("STOCK_PA_DIRECT_STROKE", self.cmd_STROKE,
                                    desc="Capture a bounded pressure stroke including a measured Max Flow peak")
        self.gcode.register_command("STOCK_PA_DIRECT_PA_STROKE", self.cmd_STROKE,
                                    desc="Capture a continuous pressure stroke for the selected CFS slot")
        self.gcode.register_command("STOCK_PA_DIRECT_FLOW", self.cmd_FLOW,
                                    desc="Capture a two-second extrusion-only flow probe over the purge chute")
        self.gcode.register_command("STOCK_PA_DIRECT_SUSTAIN", self.cmd_SUSTAIN,
                                    desc="Capture a supervised 30- or 120-second flow pull at bed center")
        self.gcode.register_command("STOCK_PA_DIRECT_MOTOR_TEST", self.cmd_MOTOR_TEST,
                                    desc="Verify the stock extruder motor's read-only current telemetry")
        self.gcode.register_command("STOCK_PA_DIRECT_SERIES", self.cmd_SERIES,
                                    desc="Capture 60-second ABS pulls at 20, 30 and 40 mm3/s with E current")
        self.gcode.register_command("STOCK_PA_DIRECT_MAP", self.cmd_MAP,
                                    desc="Map pressure and E current for slot D ABS or slot A/B/C PETG with 16- or 20-second steps")
        self.gcode.register_command("STOCK_PA_DIRECT_CURRENT_FLOW", self.cmd_CURRENT_FLOW,
                                    desc="Measure servo current in all CFS slots during a 16-second flow step (minimum %d mm3/s)"
                                    % CURRENT_PROBE_MIN_FLOW)

    def check_idle(self, gcmd, nozzle_target=0):
        stats = self.printer.lookup_object("print_stats")
        sdcard = self.printer.lookup_object("virtual_sdcard")
        pause = self.printer.lookup_object("pause_resume")
        toolhead = self.printer.lookup_object("toolhead")
        bed = self.printer.lookup_object("heaters").lookup_heater("heater_bed")
        nozzle = toolhead.get_extruder().get_heater()
        now = self.reactor.monotonic()
        deadline = now + 1.5
        while True:
            planned, current, empty = toolhead.check_busy(now)
            if (stats.state not in ("standby", "complete", "cancelled") or sdcard.is_active()
                    or pause.get_status(now)["is_paused"] or not empty
                    or bed.get_status(now)["target"] or nozzle.get_status(now)["target"] != nozzle_target):
                raise gcmd.error("The direct pressure benchmark requires an idle printer, the bed off, and the expected nozzle target.")
            if planned <= current + .02:
                return
            if now >= deadline:
                raise gcmd.error("The direct pressure benchmark stopped because the toolhead queue was still active.")
            now = self.reactor.pause(now + .05)

    @staticmethod
    def check_ack(reply, operation):
        if reply["err"]:
            raise ValueError("The nozzle MCU rejected the pressure %s (error %s)." % (operation, reply["err"]))

    def collect(self, mcu, protocol, windows, continuous, stroke=None, lead_seconds=.12, motion=None, motor=None):
        start_args, stop_args, period = protocol
        serial = mcu._serial
        oid = start_args[0]
        parser = serial.get_msgparser()
        original_parse = parser.parse
        previous_override = vars(parser).get("parse")
        start = mcu.lookup_query_command(START, ACK, oid=oid)
        stop = mcu.lookup_query_command(STOP, ACK, oid=oid)
        packets = deque(maxlen=4096)
        ack_key = (ACK.split()[0], oid)
        replies = set()

        def capture_pressure(message):
            reply = original_parse(message)
            if reply.get("#name") == PRESSURE and reply.get("oid") == oid:
                tick_bytes, data_bytes = reply.get("ticks"), reply.get("datas")
                replies.add((reply.get("ch"), reply.get("len"),
                             len(tick_bytes or b""), len(data_bytes or b"")))
                # Pushed packets can have a zero length field and a populated payload.
                if reply.get("ch") == 0 and tick_bytes and data_bytes:
                    packets.append((tick_bytes, data_bytes))
            return reply

        with serial.lock:
            original_ack_handler = serial.handlers.get(ack_key)
        parser.parse = capture_pressure
        samples = {}
        new_counts = []
        started = False
        motion_rows = {}
        capture_errors = []
        expand_tick = mcu.clock32_to_clock64 if motion else None
        motion_start = mcu.estimated_print_time(self.reactor.monotonic()) - .1 if motion else None

        def drain(eventtime):
            try:
                decode_samples(packets, samples, expand_tick)
                moves, _ = motion.extract_trapq(motion_start, mcu.estimated_print_time(eventtime) + .1)
                motion_rows.update((m.print_time, [m.print_time, m.move_t, m.start_v, m.accel]) for m in moves)
            except (ValueError, OSError) as exc:
                capture_errors.append(str(exc))
                return self.reactor.NEVER
            return eventtime + .20

        timer = motor_timer = None
        try:
            try:
                for _ in range(windows):
                    before = len(samples)
                    start_at = self.reactor.monotonic()
                    if not started:
                        started = True
                        self.check_ack(start.send(start_args), "start")
                        if motor:
                            motor_timer = self.reactor.register_timer(motor.poll, self.reactor.NOW)
                    if stroke is None:
                        self.reactor.pause(max(start_at + period, self.reactor.monotonic()))
                    else:
                        self.reactor.pause(self.reactor.monotonic() + lead_seconds)
                        if not decode_samples(packets, samples, expand_tick):
                            raise ValueError("No pressure samples arrived before the extrusion stroke.")
                        if motor and len(motor.samples) < 2:
                            raise ValueError("The extruder motor did not supply current before the pull.")
                        if motion:
                            timer = self.reactor.register_timer(drain, self.reactor.NOW)
                        stroke()
                        self.reactor.pause(self.reactor.monotonic() + .14)
                    if not continuous:
                        self.check_ack(stop.send(stop_args), "stop")
                        started = False
                    decode_samples(packets, samples, expand_tick)
                    new_counts.append(len(samples) - before)
                    if continuous and new_counts[-3:] == [0, 0, 0]:
                        break
            finally:
                if started:
                    self.check_ack(stop.send(stop_args), "stop")
            self.reactor.pause(self.reactor.monotonic() + .035)
        finally:
            if motor_timer is not None:
                self.reactor.unregister_timer(motor_timer)
            if timer is not None:
                self.reactor.unregister_timer(timer)
            if previous_override is None:
                del parser.parse
            else:
                parser.parse = previous_override
            with serial.lock:
                if original_ack_handler is None:
                    serial.handlers.pop(ack_key, None)
                else:
                    serial.handlers[ack_key] = original_ack_handler
        new_counts[-1] += decode_samples(packets, samples, expand_tick)
        if not samples:
            logging.info("Direct pressure reply headers seen: %s", replies)
        pressure = sorted((mcu.clock_to_print_time(tick if expand_tick else mcu.clock32_to_clock64(tick)), value)
                          for tick, value in samples.items())
        return pressure, new_counts, [motion_rows[t] for t in sorted(motion_rows)], capture_errors

    def cmd_TEST(self, gcmd):
        windows = gcmd.get_int("WINDOWS", 12, minval=2, maxval=30)
        continuous = gcmd.get_int("CONTINUOUS", 1, minval=0, maxval=1)
        self.benchmark(gcmd, windows, continuous)

    def cmd_MOTOR_TEST(self, gcmd):
        self.benchmark(gcmd, 20, True, motor_current=True)

    def flow_profile(self, gcmd):
        slot = gcmd.get("SLOT", "B")
        material = gcmd.get("MATERIAL", "ABS" if slot == "D" else "PETG")
        if slot not in ("A", "B", "C", "D") or material not in STROKE_PROFILES:
            raise gcmd.error("Select a slot in CFS unit 1 and ABS or PETG.")
        material_code, temperatures = STROKE_PROFILES[material]
        target = gcmd.get_int("TEMPERATURE", temperatures[-1])
        if target not in temperatures:
            raise gcmd.error("The requested nozzle temperature is outside the verified range for this material.")
        diameter = gcmd.get_float("DIAMETER", 1.75, above=0.)
        if not math.isfinite(diameter):
            raise gcmd.error("Set a positive filament diameter.")
        return slot, material_code, target, diameter

    def probe_ceiling(self, slot, material, target):
        reference_slot = "D" if material == STROKE_PROFILES["ABS"][0] else ("C" if slot == "C" else "A")
        flows = MAP_FLOWS.get((reference_slot, material, target))
        return flows[-1] if flows else None

    def cmd_STROKE(self, gcmd):
        slot, material_code, target, diameter = self.flow_profile(gcmd)
        measured = gcmd.get_int("MEASURED_FLOW", 0, minval=0, maxval=1)
        limit = self.probe_ceiling(slot, material_code, target) if measured else PA_STROKE_LIMIT
        if limit is None:
            raise gcmd.error("Measured-flow PA requires the verified Max Flow material temperature.")
        flow = gcmd.get_float("FLOW", 12., above=0., maxval=limit)
        band = gcmd.get("BAND", None)
        if not math.isfinite(flow) or (band is None and (measured or flow not in (12., 24.))):
            raise gcmd.error("Specify BAND=LOWER or BAND=PEAK for a profile-based flow target.")
        if band is not None and band not in ("LOWER", "PEAK"):
            raise gcmd.error("BAND must be LOWER or PEAK.")
        peak = 5. if band is None and flow == 12. else flow / (math.pi * (diameter / 2) ** 2)
        lower = band == "LOWER" or (band is None and flow == 12.)
        slow = peak * (.4 if lower else .55)
        feeds = (slow, peak, slow)
        segments = ((115.34, 24, .85), (115.94, 60, .60), (116.28, 24, .85))
        if measured:
            now = self.reactor.monotonic()
            config = self.printer.lookup_object("configfile").get_status(now)["settings"]["extruder"]
            actual_diameter = config["filament_diameter"]
            area = math.pi * (actual_diameter / 2) ** 2
            if not math.isclose(diameter, actual_diameter, rel_tol=.005) or any(
                    speed > config["max_extrude_only_velocity"]
                    or speed * duration > config["max_extrude_only_distance"]
                    or speed * area / (velocity / 60) > config["max_extrude_cross_section"]
                    for (_, velocity, duration), speed in zip(segments, feeds)):
                raise gcmd.error("The measured-flow pressure stroke exceeds the firmware extrusion limits.")

        def stroke():
            self.check_purge_position(gcmd, slot, material_code, target)
            commands = ["G90", "M83", "M220 S100", "M221 S100"]
            self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=k2_pa_direct_stroke")
            try:
                # Use the same purge-bin stroke and segment times as the stock PA sweep.
                for (x, velocity, duration), speed in zip(segments, feeds):
                    length = math.floor(speed * duration * 1e6 + 1e-9) / 1e6
                    commands.append("G1 X%s E%.6f F%s" % (x, length, velocity))
                self.gcode.run_script_from_command("\n".join(commands + ["M400"]))
            finally:
                self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=k2_pa_direct_stroke")

        self.check_purge_position(gcmd, slot, material_code, target)
        self.benchmark(gcmd, 1, True, stroke, feeds, nozzle_target=target)

    def cmd_FLOW(self, gcmd):
        slot, material_code, target, diameter = self.flow_profile(gcmd)
        flow = gcmd.get_int("FLOW", minval=5, maxval=MAX_TEST_FLOW)
        speed = flow / (math.pi * (diameter / 2) ** 2)

        def stroke():
            self.check_purge_position(gcmd, slot, material_code, target)
            extruder = self.printer.lookup_object("toolhead").get_extruder()
            if extruder.get_status(self.reactor.monotonic())["pressure_advance"] != 0:
                raise gcmd.error("Set pressure advance to zero before probing max flow.")
            self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=k2_pa_direct_flow")
            try:
                self.gcode.run_script_from_command(
                    "M83\nM220 S100\nM221 S100\nG1 E%.6f F%.6f\nM400" % (2 * speed, 60 * speed))
            finally:
                self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=k2_pa_direct_flow")

        self.check_purge_position(gcmd, slot, material_code, target)
        self.benchmark(gcmd, 1, True, stroke, (speed,), nozzle_target=target, lead_seconds=.45)

    def cmd_SUSTAIN(self, gcmd):
        slot, material_code, target, diameter = self.flow_profile(gcmd)
        flow = gcmd.get_int("FLOW", MAX_TEST_FLOW)
        seconds = gcmd.get_int("SECONDS", SUSTAIN_SECONDS)
        if slot != "D" or material_code != STROKE_PROFILES["ABS"][0]:
            raise gcmd.error("The sustained flow diagnostic uses the known ABS spool in slot D.")
        if (seconds, flow) not in ((30, 20), (30, 32), (30, 40), (120, MAX_TEST_FLOW)):
            raise gcmd.error("Choose 30 seconds at 20, 32, or 40 mm³/s, or 120 seconds at 32 mm³/s.")
        segment_count = 2 if seconds == SUSTAIN_SECONDS else 1
        speed = flow / (math.pi * (diameter / 2) ** 2)
        toolhead = self.printer.lookup_object("toolhead")
        extruder = toolhead.get_extruder()
        length = seconds * speed / segment_count
        now = self.reactor.monotonic()
        settings = self.printer.lookup_object("configfile").get_status(now)["settings"]["extruder"]
        if not math.isclose(settings["filament_diameter"], diameter, rel_tol=.005):
            raise gcmd.error("The sustained pull filament diameter differs from the printer setting.")
        if length > settings["max_extrude_only_distance"] or speed > settings["max_extrude_only_velocity"]:
            raise gcmd.error("The sustained pull exceeds the extrusion-only limits.")
        stock_bottom = self.printer.lookup_object("gcode_macro PRINTER_PARAM").get_status(now)["max_z_position"]
        bottom = min(float(stock_bottom), toolhead.get_status(now)["axis_maximum"][2])

        def check_position():
            x, y, z, _ = toolhead.get_position()
            if max(abs(x - BED_CENTER), abs(y - BED_CENTER)) > .05 or abs(z - bottom) > .20:
                raise gcmd.error("The sustained pull requires the bed center and the lowest configured bed position.")
            self.check_extrusion_state(gcmd, slot, material_code, target)
            if extruder.get_status(self.reactor.monotonic())["pressure_advance"] != 0:
                raise gcmd.error("Set pressure advance to zero before the sustained pull.")

        def stroke():
            check_position()
            self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=k2_pa_direct_sustain")
            try:
                move = "G1 E%.6f F%.6f" % (length, 60 * speed)
                self.gcode.run_script_from_command("\n".join(("M83", "M220 S100", "M221 S100")
                                                            + (move,) * segment_count + ("M400",)))
            finally:
                self.gcode.run_script_from_command("M104 S0\nRESTORE_GCODE_STATE NAME=k2_pa_direct_sustain")

        check_position()
        self.benchmark(gcmd, 1, True, stroke, (speed,), nozzle_target=target, lead_seconds=1.5, stream=True)

    def cmd_SERIES(self, gcmd):
        profile = self.flow_profile(gcmd)
        if profile[:3] != ("D", STROKE_PROFILES["ABS"][0], 280):
            raise gcmd.error("The three-flow series uses the known ABS spool in slot D.")
        self.flow_series(gcmd, profile, SERIES_FLOWS, SERIES_SECONDS, SERIES_REST_SECONDS)

    def cmd_MAP(self, gcmd):
        profile = self.flow_profile(gcmd)
        flows = MAP_FLOWS.get(profile[:3])
        if flows is None:
            raise gcmd.error("The map supports ABS in slot D at 280 °C or PETG in slot A, B, or C at 240 °C.")
        seconds = gcmd.get_int("SECONDS", MAP_SECONDS)
        if seconds not in (16, MAP_SECONDS):
            raise gcmd.error("The continuous map supports 16- or 20-second steps.")
        self.flow_series(gcmd, profile, flows, seconds, 0)

    def cmd_CURRENT_FLOW(self, gcmd):
        slot, material, target, diameter = self.flow_profile(gcmd)
        limit = self.probe_ceiling(slot, material, target)
        if limit is None:
            raise gcmd.error("Use PETG at 240 C or ABS at 280 C in CFS unit 1.")
        flow = gcmd.get_int("FLOW", minval=CURRENT_PROBE_MIN_FLOW, maxval=limit)
        name = self.capture_name(gcmd)
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.wait_moves()
        self.check_idle(gcmd, target)
        self.check_flow_position(gcmd, slot, material, target)
        now = self.reactor.monotonic()
        config = self.printer.lookup_object("configfile").get_status(now)["settings"]["extruder"]
        speed = flow / (math.pi * (diameter / 2) ** 2)
        duration = CURRENT_PROBE_SECONDS
        distance = duration * speed
        if (not math.isclose(config["filament_diameter"], diameter, rel_tol=.005)
                or speed > config["max_extrude_only_velocity"] or distance > config["max_extrude_only_distance"]):
            raise gcmd.error("The servo current probe exceeds the configured extrusion-only limits.")
        mcu = self.printer.lookup_object("mcu nozzle_mcu")
        motion = self.printer.lookup_object("motion_report").trapqs["extruder"]
        motor = StockExtruderCurrent(mcu, self.reactor)
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=k2_current_probe")
        timer = self.reactor.register_timer(motor.poll, self.reactor.NOW)
        try:
            self.reactor.pause(self.reactor.monotonic() + .8)
            if len(motor.samples) < 2 or any(row[2] for row in motor.samples):
                raise gcmd.error("The extruder servo did not return usable current at rest.")
            self.check_flow_position(gcmd, slot, material, target)
            self.gcode.run_script_from_command("M83\nM220 S100\nM221 S100\nG1 E%.6f F%.6f"
                                               % (distance, speed * 60))
            end = toolhead.get_last_move_time()
            toolhead.wait_moves()
        finally:
            self.reactor.unregister_timer(timer)
            self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=k2_current_probe")
        start = end - duration
        moves, _ = motion.extract_trapq(start - .15, end + .15)
        result = {"mode": "servo_current", "motor_current": motor.samples, "motor_missed": motor.missed,
                  "planned_segments": [[flow, start, end]], "filament_feed_mm_s": [speed],
                  "moves": [[move.print_time, move.move_t, move.start_v, move.accel] for move in moves]}
        path = self.save_capture(name, result)
        gcmd.respond_info("Stock servo current probe saved %s" % path.name)

    def check_flow_position(self, gcmd, slot, material, target):
        toolhead = self.printer.lookup_object("toolhead")
        now = self.reactor.monotonic()
        max_z = self.printer.lookup_object("gcode_macro PRINTER_PARAM").get_status(now)["max_z_position"]
        bottom = min(float(max_z), toolhead.get_status(now)["axis_maximum"][2])
        x, y, z, _ = toolhead.get_position()
        if max(abs(x - BED_CENTER), abs(y - BED_CENTER)) > .05 or abs(z - bottom) > .20:
            raise gcmd.error("The flow test requires the bed center and the lowest configured bed position.")
        self.check_extrusion_state(gcmd, slot, material, target)
        if toolhead.get_extruder().get_status(now)["pressure_advance"] != 0:
            raise gcmd.error("Set pressure advance to zero before measuring flow.")

    def flow_series(self, gcmd, profile, flows, duration, rest):
        slot, material, target, diameter = profile
        now = self.reactor.monotonic()
        toolhead = self.printer.lookup_object("toolhead")
        extruder = toolhead.get_extruder()
        if not rest and not callable(getattr(toolhead, "register_lookahead_callback", None)):
            raise gcmd.error("The stock planner does not expose continuous move timing.")
        config = self.printer.lookup_object("configfile").get_status(now)["settings"]["extruder"]
        feeds = tuple(flow / (math.pi * (diameter / 2) ** 2) for flow in flows)
        if (not math.isclose(config["filament_diameter"], diameter, rel_tol=.005)
                or feeds[-1] > config["max_extrude_only_velocity"]
                or duration * feeds[-1] > config["max_extrude_only_distance"]):
            raise gcmd.error("The series exceeds the configured extrusion-only limits.")
        stock_bottom = self.printer.lookup_object("gcode_macro PRINTER_PARAM").get_status(now)["max_z_position"]
        bottom = min(float(stock_bottom), toolhead.get_status(now)["axis_maximum"][2])
        planned = []

        def check_position():
            x, y, z, _ = toolhead.get_position()
            if max(abs(x - BED_CENTER), abs(y - BED_CENTER)) > .05 or abs(z - bottom) > .20:
                raise gcmd.error("The series requires the bed center and the lowest configured bed position.")
            self.check_extrusion_state(gcmd, slot, material, target)
            if extruder.get_status(self.reactor.monotonic())["pressure_advance"] != 0:
                raise gcmd.error("Set pressure advance to zero before the series.")

        def stroke():
            self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=k2_pa_direct_sustain")
            try:
                self.gcode.run_script_from_command("M83\nM220 S100\nM221 S100")
                for index, (flow, speed) in enumerate(zip(flows, feeds)):
                    check_position()
                    self.gcode.run_script_from_command("G1 E%.6f F%.6f" % (duration * speed, 60 * speed))
                    if rest:
                        end = toolhead.get_last_move_time()
                        planned.append([flow, end - duration, end])
                        dwell = "\nG4 P%d\nM400" % (1000 * rest) if index < len(feeds) - 1 else ""
                        self.gcode.run_script_from_command("M400" + dwell)
                    else:
                        # This records planner time without flushing the previous E move.
                        toolhead.register_lookahead_callback(
                            lambda end, flow=flow: planned.append([flow, end - duration, end]))
                self.gcode.run_script_from_command("M400")
            finally:
                self.gcode.run_script_from_command("M104 S0\nRESTORE_GCODE_STATE NAME=k2_pa_direct_sustain")

        check_position()
        self.benchmark(gcmd, 1, True, stroke, feeds, nozzle_target=target, lead_seconds=2.5, stream=True,
                       motor_current=True, planned=planned)

    def check_purge_position(self, gcmd, slot, material_code, target):
        x, y, z, _ = self.printer.lookup_object("toolhead").get_position()
        if abs(x - 115) > .2 or abs(y - 291.5) > .2 or z < 4:
            raise gcmd.error("The extrusion stroke requires the stock purge chute position.")
        self.check_extrusion_state(gcmd, slot, material_code, target)

    def check_extrusion_state(self, gcmd, slot, material_code, target):
        index = "ABCD".index(slot)
        address = "T1%s" % slot
        now = self.reactor.monotonic()
        toolhead = self.printer.lookup_object("toolhead")
        nozzle = toolhead.get_extruder().get_heater().get_status(now)
        box = self.printer.lookup_object("box").get_status(now)
        filament = self.printer.lookup_object("filament_switch_sensor filament_sensor").get_status(now)
        if (toolhead.get_status(now)["homed_axes"] != "xyz" or abs(nozzle["temperature"] - target) > 5
                or nozzle["target"] != target or box["state"] != "connect"
                or box["map"][address] != address or box["T1"]["filament"] != slot
                or box["T1"]["material_type"][index] != material_code or not filament["filament_detected"]):
            raise gcmd.error("The extrusion stroke requires XYZ homed and the selected material from slot %s at %d °C."
                             % (slot, target))

    @staticmethod
    def capture_name(gcmd):
        name = gcmd.get("NAME", "idle")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name):
            raise gcmd.error("Use 1–32 ASCII letters, digits, dashes, or underscores for NAME.")
        return name

    def save_capture(self, name, result):
        folder = Path(self.printer.get_start_args()["config_file"]).parent
        path = folder / ("stock_pa_direct_%s.json" % name)
        pending = path.with_suffix(".tmp")
        with pending.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, separators=(",", ":"))
        os.replace(pending, path)
        return path

    def benchmark(self, gcmd, windows, continuous, stroke=None, feeds=None, nozzle_target=0, lead_seconds=.12,
                  stream=False, motor_current=False, planned=None):
        name = self.capture_name(gcmd)
        self.check_idle(gcmd, nozzle_target)
        mcu = self.printer.lookup_object("mcu nozzle_mcu")
        try:
            seen = set(sent_frames(mcu._serial))
            self.gcode.run_script_from_command("READ_PRES")
            frames = [frame for frame in sent_frames(mcu._serial) if frame not in seen]
            try:
                protocol = observed_pressure_commands(frames)
            except ValueError:
                logging.info("Stock pressure command trace: %s",
                             [frame for frame in frames if "_prtouch_pres " in frame])
                raise
            self.check_idle(gcmd, nozzle_target)
            motion = self.printer.lookup_object("motion_report").trapqs["extruder"] if stroke else None
            motor = StockExtruderCurrent(mcu, self.reactor) if motor_current else None
            samples, new_counts, streamed_moves, errors = self.collect(
                mcu, protocol, windows, continuous, stroke, lead_seconds, motion if stream else None, motor)
            result = {"mode": "single_start" if continuous else "windowed", "protocol": protocol._asdict(),
                      "samples": samples, "new_samples_per_window": new_counts, "timing": timing_stats(samples)}
            if stroke:
                moves, _ = motion.extract_trapq(samples[0][0] - .1, samples[-1][0] + .1)
                rows = {m[0]: m for m in streamed_moves}
                rows.update((m.print_time, [m.print_time, m.move_t, m.start_v, m.accel]) for m in moves)
                result["moves"] = [rows[t] for t in sorted(rows)]
                result["filament_feed_mm_s"] = feeds
                if stream:
                    result["stream_errors"] = errors
            if motor:
                result.update(motor_current=motor.samples, motor_missed=motor.missed)
            if planned is not None:
                result["planned_segments"] = planned
            path = self.save_capture(name, result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise gcmd.error("Stock direct pressure benchmark: %s" % exc)
        gcmd.respond_info("Stock direct pressure benchmark saved %s" % path.name)
