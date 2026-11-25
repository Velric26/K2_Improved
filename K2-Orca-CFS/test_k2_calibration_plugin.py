"""Offline tests for the Orca plugin's pure functions, with the orca module stubbed out."""

import sys
import types
import unittest

orca = types.ModuleType("orca")
orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
orca.plugin = lambda cls: cls
orca.base = object
sys.modules.setdefault("orca", orca)

import k2_calibration_plugin as plugin  # noqa: E402

EDITED = types.SimpleNamespace(name="another preset", is_dirty=False)
WALLS = {"outer": {"flow": 25., "speed": 200., "acceleration": 2500., "corner_velocity": 0.},
         "inner": {"flow": 40., "speed": 300., "acceleration": 6000., "corner_velocity": 0.}}
SLOT = {"slot": "1C", "material": "ABS", "max_flow": True, "pa_peak_limit_mm3_s": 48}


class Preset:
    is_compatible = True
    name = "Creality CR ABS"
    file = "abs.json"
    is_dirty = False

    def __init__(self, **changes):
        self.values = {"filament_type": "ABS", "nozzle_temperature": ["250"], "filament_diameter": ["1.75"],
                       "filament_max_volumetric_speed": ["48"], "pressure_advance": ["0.045"],
                       "enable_pressure_advance": ["1"], "adaptive_pressure_advance": ["0"],
                       "adaptive_pressure_advance_model": [""], "nozzle_temperature_range_low": ["240"],
                       "nozzle_temperature_range_high": ["280"]}
        self.values.update(changes)

    def config_value(self, key):
        return self.values.get(key)


class ProfileTest(unittest.TestCase):
    def test_temperature_follows_the_filament_range(self):
        self.assertEqual(plugin.calibration_profile(Preset(), EDITED)["temperature"], 250)
        self.assertEqual(plugin.calibration_profile(Preset(nozzle_temperature=["250.0"]), EDITED)["temperature"], 250)
        for value in ("230", "290"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "240–280 °C range"):
                plugin.calibration_profile(Preset(nozzle_temperature=[value]), EDITED)

    def test_missing_range_allows_any_temperature(self):
        preset = Preset(nozzle_temperature=["265"], nozzle_temperature_range_low=None)
        self.assertEqual(plugin.calibration_profile(preset, EDITED)["temperature"], 265)

    def test_other_materials_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "PETG and ABS"):
            plugin.calibration_profile(Preset(filament_type="PLA"), EDITED)


class RequestTest(unittest.TestCase):
    def test_max_flow_then_adaptive_pa_sends_walls_and_seed(self):
        profile = plugin.calibration_profile(Preset(), EDITED)
        payload = plugin.request_for("flow_then_pa", profile, SLOT, "192.0.2.10", .6, 0., 0., walls=WALLS)
        self.assertEqual((payload["kind"], payload["temperature_c"], payload["pa_seed"]), ("flow_then_pa", 250, .045))
        self.assertEqual(payload["walls"], WALLS)

    def test_auto_pa_uses_the_slot_flow_limit(self):
        profile = plugin.calibration_profile(Preset(filament_max_volumetric_speed=["60"]), EDITED)
        with self.assertRaisesRegex(ValueError, "up to 48"):
            plugin.request_for("pressure_advance", profile, SLOT, "192.0.2.10", .6, 60., 0.)


class DescribeTest(unittest.TestCase):
    FLOW = {"recommended_mm3_s": 30, "below_cutoff_mm3_s": 29, "below_cutoff_median_abs_iq": .9,
            "boundary_median_abs_iq": 1.1}

    def test_combined_result_applies_flow_and_adaptive_model(self):
        adaptive = {"points": [{"wall": "outer", "acceleration": 2500, "flow": 25, "pa": .05,
                                "selected_scores": [.1, .1], "measured_flow": 25}], "complete_grid": True}
        text, changes = plugin.describe_result(
            "flow_then_pa", {"max_flow": self.FLOW, "adaptive_pa": adaptive, "pa_state": "complete", "pa_message": ""}, {})
        self.assertIn("Adaptive PA with wall flow capped", text)
        self.assertEqual(changes["filament_max_volumetric_speed"], "30")
        self.assertEqual(changes["adaptive_pressure_advance"], "1")

    def test_skipped_adaptive_step_still_reports_max_flow(self):
        text, changes = plugin.describe_result(
            "flow_then_pa", {"max_flow": self.FLOW, "adaptive_pa": None, "pa_state": "skipped",
                             "pa_message": "Adaptive PA was skipped."}, {})
        self.assertTrue(text.endswith("Adaptive PA was skipped."))
        self.assertEqual(list(changes), ["filament_max_volumetric_speed"])


if __name__ == "__main__":
    unittest.main()
