# F021 bench heater controller for Creality's stock Klipper.
# Distributed under the GNU GPLv3 license.


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
            "bed_priority_enabled": not self.external_ac_supply,
            "factory_fan_monitor_active": self.factory_fan_monitor_active,
        }


def load_config(config):
    return ChamberBangBang(config)
