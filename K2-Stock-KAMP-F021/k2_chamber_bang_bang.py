# F021 bench heater controller for Creality's stock Klipper.
# Distributed under the GNU GPLv3 license.

from datetime import datetime
import logging
import math
from pathlib import Path
import re


MASTER_LOG = Path("/mnt/UDISK/creality/userdata/log/master-server.log")
JOB_START = re.compile(rb"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+\]"
                       rb"-\[INF\]-\[\d+\]-\[AppModeSdPrint\.c:\d+\] prin file name = (.+)$")
JOB_MAX_AGE = 45
PREHEAT_TIMEOUT = 20 * 60


def requested_preheat(log, gcode_root, bed, nozzle, now):
    """Read the fresh Creality job and verify its explicit chamber request."""
    try:
        with log.open("rb") as records:
            records.seek(max(0, log.stat().st_size - 256 * 1024))
            latest = next((m for row in reversed(records.read(512 * 1024).splitlines())
                           for m in (JOB_START.fullmatch(row),) if m), None)
        if latest is None:
            return None
        started = datetime.strptime(latest[1].decode("ascii"), "%Y-%m-%d %H:%M:%S")
        if not 0 <= (now - started).total_seconds() <= JOB_MAX_AGE:
            return None
        filename = Path(latest[2].decode("utf-8"))
        if not filename.is_absolute():
            return None
        path = filename.resolve(strict=True)
        if path.suffix.lower() != ".gcode" or gcode_root.resolve() not in path.parents:
            return None
        with path.open("rb") as file:
            header = file.read(256 * 1024)
        for raw in header.splitlines():
            fields = raw.partition(b";")[0].split()
            if not fields or fields[0].upper() != b"START_PRINT":
                continue
            params = dict(token.upper().split(b"=", 1) for token in fields[1:] if b"=" in token)
            target = float(params.get(b"CHAMBER_TEMP", b"0"))
            minimum = float(params.get(b"CHAMBER_MIN", b"0"))
            if (0 < target <= 60 and 0 <= minimum <= target
                    and math.isclose(float(params[b"BED_TEMP"]), bed, abs_tol=.05)
                    and math.isclose(float(params[b"EXTRUDER_TEMP"]), nozzle, abs_tol=.05)):
                return (started, path), target
            return None
    except (OSError, UnicodeError, ValueError, KeyError, RuntimeError):
        logging.info("K2 chamber preheat could not verify the Creality print header")
    return None


class ChamberBangBang:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config_error = config.error
        heater = config.getsection("heater_generic chamber_heater")
        if (heater.get("control") != "watermark"
                or heater.get("heater_pin") != "PA0"
                or heater.get("sensor_pin") != "PC5"
                or not heater.getboolean("verify_heater", True)):
            raise config.error("The K2 on/off controller requires the verified PA0 chamber heater.")
        self.max_delta = heater.getfloat("max_delta", 2.0, above=0.)
        self.external_ac_supply = config.getboolean("external_ac_supply", False)
        self.monitor_factory_chamber_fan = config.getboolean("monitor_factory_chamber_fan", True)
        if not self.monitor_factory_chamber_fan and not self.external_ac_supply:
            raise config.error("Disabling the factory chamber fan monitor requires an external AC heater.")
        self.factory_fan_monitor_active = True
        self.heating = False
        self.heater = None
        self.bed = None
        self.gcode = self.printer.lookup_object("gcode")
        self.reactor = self.printer.get_reactor()
        self.gcode_root = Path(config.getsection("virtual_sdcard").get("path")).expanduser()
        self.preheat_timer = self.reactor.register_timer(self.check_preheat)
        self.preheat_deadline, self.preheat_target = 0., 0.
        self.preheated_job = None
        self.gcode.register_command("CHAMBER_EARLY_PREHEAT", self.cmd_PREHEAT)
        self.gcode.register_command("CHAMBER_EARLY_HANDOFF", self.cmd_HANDOFF)
        self.printer.register_event_handler("klippy:connect", self.connect)

    def connect(self):
        heater = self.printer.lookup_object("heaters").lookup_heater("chamber_heater")
        bed = self.printer.lookup_object("heater_bed")
        if (not hasattr(heater, "stop_heating") or not hasattr(bed, "heater_bed_state")
                or heater.get_max_power() != 1.):
            raise self.config_error("The K2 on/off controller requires compatible Creality heater protections and max_power 1.0.")
        self.heater, self.bed = heater, bed
        heater.set_control(self)
        if not self.monitor_factory_chamber_fan:
            # Remove only this heater's Creality timer registration before Klipper is ready.
            ready_handlers = self.printer.event_handlers.get("klippy:ready", [])
            register_fan_timer = getattr(heater, "register_chamber_heater_timer", None)
            if register_fan_timer is None or ready_handlers.count(register_fan_timer) != 1:
                raise self.config_error("The factory chamber fan monitor is incompatible with this controller.")
            ready_handlers.remove(register_fan_timer)
            self.factory_fan_monitor_active = False

    def cmd_PREHEAT(self, gcmd):
        now = self.reactor.monotonic()
        bed = gcmd.get_float("BED", above=0.)
        if (not self.external_ac_supply or self.heater.get_status(now)["target"]
                or not math.isclose(self.bed.get_status(now)["target"], bed, abs_tol=.05)
                or self.printer.lookup_object("virtual_sdcard").is_active()):
            return
        request = requested_preheat(MASTER_LOG, self.gcode_root, bed,
                                    gcmd.get_float("NOZZLE", above=0.), datetime.now())
        if request is None or request[0] == self.preheated_job:
            return
        self.gcode.run_script_from_command("M141 S0\n"
                                           "SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=%g" % request[1])
        self.preheated_job, self.preheat_target = request
        self.preheat_deadline = now + PREHEAT_TIMEOUT
        self.reactor.update_timer(self.preheat_timer, now + 1.)
        gcmd.respond_info("K2 chamber warming alongside the bed to %g C." % request[1])

    def cmd_HANDOFF(self, gcmd):
        target = gcmd.get_float("TARGET", minval=0., maxval=60.)
        actual = self.heater.get_status(self.reactor.monotonic())["target"]
        if self.preheat_target and actual == self.preheat_target and not target:
            self.heater.set_temp(0.)
        self.preheat_target = 0.
        self.reactor.update_timer(self.preheat_timer, self.reactor.NEVER)

    def check_preheat(self, eventtime):
        if not self.preheat_target or self.heater.get_status(eventtime)["target"] != self.preheat_target:
            self.preheat_target = 0.
            return self.reactor.NEVER
        if eventtime >= self.preheat_deadline or not self.bed.get_status(eventtime)["target"]:
            self.heater.set_temp(0.)
            self.preheat_target = 0.
            logging.info("K2 chamber early preheat stopped before START_PRINT")
            return self.reactor.NEVER
        return eventtime + 1.

    def temperature_update(self, read_time, temp, target_temp):
        if target_temp <= 0. or self.heater.stop_heating or temp >= target_temp + self.max_delta:
            self.heating = False
        elif temp <= target_temp - self.max_delta:
            self.heating = True
        bed_allows_heat = self.external_ac_supply or self.bed.heater_bed_state != 1
        self.heater.set_pwm(read_time, 1. if self.heating and bed_allows_heat else 0.)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return smoothed_temp < target_temp - self.max_delta

    def get_status(self, eventtime):
        return {
            "active": self.heater is not None,
            "early_preheat_available": self.external_ac_supply,
            "bed_priority_enabled": not self.external_ac_supply,
            "factory_fan_monitor_active": self.factory_fan_monitor_active,
        }


def load_config(config):
    return ChamberBangBang(config)
