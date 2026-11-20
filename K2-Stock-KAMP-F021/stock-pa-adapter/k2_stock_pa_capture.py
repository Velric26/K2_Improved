# Capture Creality's existing READ_PRES packets on the MCU motion clock.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import os
import re
from collections import deque
from pathlib import Path

from .k2_stock_pa_direct import StockDirectPressure
from .k2_stock_pa_zip import unzip_data


PRESSURE_REPLY = "resault_prtouch_pres"


class StockPressureCapture:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.last = {}
        self.gcode.register_command(
            "STOCK_PA_CAPTURE", self.cmd_CAPTURE,
            desc="Save stock pressure samples and extruder moves on the motion clock",
        )
        self.direct = StockDirectPressure(self.printer)

    def get_status(self, _eventtime):
        return self.last

    def cmd_CAPTURE(self, gcmd):
        count = gcmd.get_int("WINDOWS", 1, minval=1, maxval=64)
        name = gcmd.get("NAME", "idle")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name):
            raise gcmd.error("NAME must be 1–32 ASCII letters, digits, dashes, or underscores.")
        stats = self.printer.lookup_object("print_stats")
        if stats.state in ("printing", "paused"):
            raise gcmd.error("Stock pressure capture is for idle tests.")
        mcu = self.printer.lookup_object("mcu nozzle_mcu")
        parser = mcu._serial.get_msgparser()
        motion = self.printer.lookup_object("motion_report").trapqs.get("extruder")
        if motion is None:
            raise gcmd.error("The stock extruder motion queue is unavailable.")

        # The serial thread runs this wrapper only during the synchronous reads.
        # It delegates parsing and passes the original message to the stock driver.
        packets = deque(maxlen=4096)
        original_parse = parser.parse
        previous_override = vars(parser).get("parse")

        def capture_parse(message):
            params = original_parse(message)
            if params.get("#name") == PRESSURE_REPLY and params.get("ch") == 0:
                packets.append((params.get("ticks"), params.get("datas")))
            return params

        parser.parse = capture_parse
        try:
            for _ in range(count):
                self.gcode.run_script_from_command("READ_PRES")
        finally:
            if vars(parser).get("parse") is capture_parse:
                if previous_override is None:
                    del parser.parse
                else:
                    parser.parse = previous_override

        decoded = {}
        packet_count = len(packets)
        try:
            for tick_bytes, pressure_bytes in packets:
                _, ticks = unzip_data(bytes(tick_bytes or b""), signed=False)
                _, values = unzip_data(bytes(pressure_bytes or b""))
                if len(ticks) != len(values):
                    raise ValueError("Different pressure and timestamp counts in one reply.")
                for tick, value in zip(ticks, values):
                    if tick:
                        decoded[mcu.clock32_to_clock64(tick)] = value
        except (TypeError, ValueError) as exc:
            raise gcmd.error("Stock pressure packet could not be decoded: %s" % exc)
        if not decoded:
            raise gcmd.error("READ_PRES returned no timestamped channel 0 pressure samples.")

        samples = [
            [mcu.clock_to_print_time(tick), value]
            for tick, value in sorted(decoded.items())
        ]
        moves, _ = motion.extract_trapq(samples[0][0] - 2.0, samples[-1][0] + 2.0)
        move_rows = [[m.print_time, m.move_t, m.start_v, m.accel] for m in moves]
        filename = "stock_pa_%s.json" % name
        config_dir = Path(self.printer.get_start_args()["config_file"]).parent
        path = config_dir / filename
        staged = path.with_suffix(".tmp")
        result = {"samples": samples, "moves": move_rows, "windows": count, "packets": packet_count}
        try:
            with staged.open("w", encoding="utf-8") as stream:
                json.dump(result, stream, separators=(",", ":"))
            os.replace(staged, path)
        except OSError as exc:
            raise gcmd.error("Could not write stock pressure capture: %s" % exc)
        self.last = {"file": filename, "samples": len(samples), "moves": len(move_rows),
                     "windows": count, "packets": packet_count}
        gcmd.respond_info("Stock PA capture: %(samples)d samples, %(moves)d moves to %(file)s" % self.last)


def load_config(config):
    return StockPressureCapture(config)
