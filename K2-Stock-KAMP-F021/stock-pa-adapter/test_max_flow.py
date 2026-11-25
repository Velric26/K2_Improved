import json
import math
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orca_auto_pa import EXTENDED_PA_SUPPORT, check_request, run_calibration, run_job
from run_max_flow import COMMAND, HOST_LIMITS, MAX_PROBES, MIN_FLOW, SLOT_SUPPORT, STEP_SECONDS, MaxFlowRun, probe_ceiling, search_current


EXTRUDER = {"filament_diameter": 1.75, "max_extrude_only_velocity": 212.86,
            "max_extrude_only_distance": 1000}
PAYLOAD = {"printer": "192.0.2.118", "slot": "1D", "material": "ABS", "temperature_c": 280,
           "nozzle_mm": .4, "kind": "max_flow", "max_flow_mm3_s": 31, "filament_diameter_mm": 1.75}
COMMANDS = {COMMAND: f"Capture one step in {SLOT_SUPPORT} (minimum {MIN_FLOW} mm3/s, {HOST_LIMITS})",
            "STOCK_PA_CAPTURE": ""}


def capture(flows, iq):
    area = math.pi * (.875 ** 2)
    feeds = [flow / area for flow in flows]
    segments = [[flow, i * STEP_SECONDS, (i + 1) * STEP_SECONDS] for i, flow in enumerate(flows)]
    return {"mode": "servo_current", "planned_segments": segments, "filament_feed_mm_s": feeds,
            "moves": [[start, STEP_SECONDS, speed, 0] for (_, start, _), speed in zip(segments, feeds)],
            "motor_current": [[start + 8 + i / 4, -iq(flow), 0] for flow, start, _ in segments for i in range(31)],
            "motor_missed": 0}


class MaxFlowTest(unittest.TestCase):
    def test_limits_and_supported_profiles_are_checked_before_preparation(self):
        sweep = Mock(slot="D", material="ABS")
        self.assertEqual(MaxFlowRun(sweep, 1.75, Mock(), EXTRUDER).ceiling, 40)
        self.assertEqual(probe_ceiling("A", "PETG"), 29)
        self.assertEqual(probe_ceiling("C", "PETG"), 30)
        self.assertEqual(probe_ceiling("C", "ABS"), 40)
        self.assertEqual(probe_ceiling("C", "ABS", .6), 48)
        self.assertEqual(probe_ceiling("C", "PETG", .6), 30)
        with self.assertRaisesRegex(RuntimeError, "diameter"):
            MaxFlowRun(sweep, 2.85, Mock(), EXTRUDER)
        with self.assertRaisesRegex(RuntimeError, "extrusion-only"):
            MaxFlowRun(sweep, 1.75, Mock(), {**EXTRUDER, "max_extrude_only_distance": 260})
        self.assertEqual(check_request({**PAYLOAD, "slot": "1C"}, PAYLOAD["printer"]), ("C", "ABS"))
        with self.assertRaisesRegex(ValueError, "positive"):
            check_request({**PAYLOAD, "max_flow_mm3_s": None}, PAYLOAD["printer"])

    def test_predicts_then_moves_up_to_the_inclusive_threshold(self):
        probe = Mock(side_effect={15: .69, 20: .93, 21: .98, 22: 1.0}.__getitem__)
        result = search_current(probe, 29, .05)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [15, 21, 22, 20])
        self.assertEqual(result["recommended_mm3_s"], 22)
        self.assertEqual(result["boundary_median_abs_iq"], 1.0)
        self.assertEqual(result["below_cutoff_mm3_s"], 21)
        self.assertEqual(result["below_cutoff_median_abs_iq"], .98)

    def test_moves_down_and_catches_a_one_step_dip(self):
        probe = Mock(side_effect={15: .51, 24: 1.038, 23: .999, 22: 1.044, 21: .943}.__getitem__)
        result = search_current(probe, 29, .05)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [15, 24, 23, 22, 21])
        self.assertEqual(result["recommended_mm3_s"], 22)
        self.assertEqual(result["below_cutoff_mm3_s"], 21)

    def test_budget_and_ceiling_without_an_adjacent_crossing(self):
        low = Mock(return_value=.99)
        self.assertEqual(search_current(low, 29, .05)["reason"], "current_below_threshold")
        self.assertEqual(low.call_count, MAX_PROBES)
        self.assertEqual(low.call_args.args, (29,))
        self.assertEqual(search_current(lambda _flow: .1, 29, .05)["reason"], "current_below_threshold")
        top = search_current(lambda flow: 1.01 if flow == 29 else .99, 29, .05)
        self.assertEqual(top["recommended_mm3_s"], 29)
        self.assertEqual(top["below_cutoff_mm3_s"], 28)
        hit = Mock(return_value=1.)
        self.assertEqual(search_current(hit, 29, .05)["reason"], "unbracketed_current")
        self.assertEqual([call.args[0] for call in hit.call_args_list], list(range(15, MIN_FLOW - 1, -1)))

    def test_initial_threshold_hit_requires_a_measured_lower_neighbor(self):
        probe = Mock(side_effect={15: 1.11, 14: 1.0, 13: .99995, 12: .91}.__getitem__)
        result = search_current(probe, 29, .05)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [15, 14, 13, 12])
        self.assertEqual(result["recommended_mm3_s"], 14)
        self.assertEqual(result["below_cutoff_mm3_s"], 13)
        self.assertEqual(result["below_cutoff_median_abs_iq"], .99995)

    def test_abs_06_checks_the_top_of_the_measured_range_first(self):
        with tempfile.TemporaryDirectory() as folder, patch("run_max_flow.move_to_flow_position"):
            sweep = Mock(folder=Path(folder), slot="D", material="ABS", temperature=280)
            run = MaxFlowRun(sweep, 1.75, Mock(), EXTRUDER, nozzle_mm=.6)
            # The 15 and 39 readings come from the 0.6 mm run. The higher readings simulate a cutoff.
            readings = {15: .2476, 39: .7883, 42: .94, 43: 1.01, 41: .91}
            with patch.object(run, "probe", side_effect=lambda _report, _name, flow: readings[flow]) as probe:
                result = run.run(18)
            self.assertEqual([call.args[2] for call in probe.call_args_list], [15, 39, 42, 43, 41])
            self.assertEqual(result["recommended_mm3_s"], 43)
            self.assertEqual(result["below_cutoff_mm3_s"], 42)

    def test_run_sends_one_bounded_probe_at_a_time_and_saves_captures(self):
        with tempfile.TemporaryDirectory() as folder, patch("run_max_flow.move_to_flow_position") as move:
            sweep = Mock(folder=Path(folder), slot="A", material="PETG", temperature=240)
            traces = {15: .69, 20: .93, 21: .98, 22: 1.01}
            flows = iter((15, 21, 22, 20))
            sweep.read.side_effect = lambda *_, **_opts: capture((next(flows),), traces.__getitem__)
            result = MaxFlowRun(sweep, 1.75, Mock(), EXTRUDER, nozzle_mm=.6).run(24)
            move.assert_called_once_with(sweep)
            self.assertEqual(result["recommended_mm3_s"], 22)
            self.assertEqual(result["tested_through_mm3_s"], 22)
            self.assertEqual(result["steps"][2]["settled_motor_samples"], 31)
            commands = [call.args[0] for call in sweep.send.call_args_list[1:]]
            self.assertEqual([int(re.search(r"FLOW=(\d+)", command)[1]) for command in commands], [15, 21, 22, 20])
            self.assertTrue(all(command.startswith(COMMAND + " NAME=") for command in commands))
            saved = json.loads((sweep.folder / result["capture"]).read_text())
            self.assertEqual((saved["slot"], saved["nozzle_mm"]), ("1A", .6))
            self.assertTrue(all((sweep.folder / row["capture"]).is_file() for row in result["steps"]))

    def test_bad_motor_samples_stop_the_search(self):
        with tempfile.TemporaryDirectory() as folder, patch("run_max_flow.move_to_flow_position"):
            sweep = Mock(folder=Path(folder), slot="A", material="PETG", temperature=240)
            sweep.read.return_value = {**capture((15,), lambda _flow: .7), "motor_current": []}
            result = MaxFlowRun(sweep, 1.75, Mock(), EXTRUDER).run(24)
            self.assertEqual(result["reason"], "unusable_current")
            self.assertEqual(result["unusable_flow_mm3_s"], 15)
            self.assertIsNone(result["recommended_mm3_s"])
            sweep.read.assert_called_once()

    def test_helper_restores_pa_and_checks_stock_reader_without_publishing(self):
        state = {"extruder": {"target": 0, "nozzle_diameter": .4, "pressure_advance": .038, "smooth_time": .038},
                 "heater_bed": {"target": 0}, "k2_stock_pa_capture": {},
                 "toolhead": {"homed_axes": "xyz"}}
        host = f"http://{PAYLOAD['printer']}:7125"
        settings = {"result": {"status": {"configfile": {"settings": {"extruder": EXTRUDER}}}}}
        with patch("orca_auto_pa.Sweep") as factory, patch("orca_auto_pa.prepare_slot") as prepare, \
                patch("orca_auto_pa.MaxFlowRun") as max_flow:
            sweep = factory.return_value
            sweep.status.side_effect = [state, state, state, dict(state, k2_stock_pa_capture={"samples": 22})]
            sweep.temperature = 280
            sweep.read.side_effect = [{"result": COMMANDS}, settings]
            max_flow.return_value.started_capture = True
            max_flow.return_value.run.return_value = {"reason": "current_cutoff", "recommended_mm3_s": 32}
            result = run_calibration(host, Path.cwd(), PAYLOAD, threading.Event(), Mock())
            self.assertEqual(result["recommended_mm3_s"], 32)
            max_flow.return_value.run.assert_called_once_with(31)
            self.assertEqual(sweep.send.call_args_list[-2].args[:3],
                             ("M104 S0", "M400", "SET_PRESSURE_ADVANCE ADVANCE=0.038 SMOOTH_TIME=0.038"))
            self.assertTrue(sweep.send.call_args.args[0].startswith("STOCK_PA_CAPTURE WINDOWS=4"))
            self.assertEqual(sweep.read.call_count, 2)
            prepare.assert_called_once_with(sweep, state, recover_unassigned=True)

    @staticmethod
    def combined_payload():
        return {**PAYLOAD, "slot": "1C", "kind": "flow_then_pa", "nozzle_mm": .6, "temperature_c": 250,
                "pa_seed": .045, "layer_height_mm": .2, "first_layer_height_mm": .25,
                "bed_temperature_c": 90, "chamber_temperature_c": 0,
                "walls": {"outer": {"flow": 25.0, "speed": 200, "acceleration": 2500},
                          "inner": {"flow": 40.0, "speed": 300, "acceleration": 6000}}}

    def test_combined_run_caps_adaptive_walls_at_the_measured_max_flow(self):
        payload = self.combined_payload()
        check_request(payload, PAYLOAD["printer"])
        for measured, outer, inner in ((18, 18, 18), (30, 25, 30), (48, 25, 40)):
            max_flow = {"recommended_mm3_s": measured}
            adaptive = {"candidate": None, "points": [], "complete_grid": True}
            with patch("orca_auto_pa.run_calibration", side_effect=[max_flow, adaptive]) as run:
                result = run_job(PAYLOAD["printer"], Path.cwd(), payload, threading.Event(), Mock())
            self.assertEqual([call.args[2]["kind"] for call in run.call_args_list], ["max_flow", "adaptive_pa"])
            self.assertTrue(all(call.args[2]["slot"] == "1C" for call in run.call_args_list))
            self.assertTrue(all(call.args[2]["temperature_c"] == 250 for call in run.call_args_list))
            second = run.call_args.args[2]
            self.assertEqual(second["max_flow_mm3_s"], measured)
            self.assertEqual((second["walls"]["outer"]["flow"], second["walls"]["inner"]["flow"]), (outer, inner))
            for wall in ("outer", "inner"):
                speed = payload["walls"][wall]["speed"] * second["walls"][wall]["flow"] / payload["walls"][wall]["flow"]
                self.assertAlmostEqual(second["walls"][wall]["speed"], speed)
            self.assertEqual(check_request(second, PAYLOAD["printer"]), ("C", "ABS"))
            self.assertEqual(result["max_flow"], max_flow)
            self.assertEqual(result["adaptive_pa"], adaptive)
            self.assertEqual(result["pa_state"], "complete")

    def test_max_flow_requires_an_adapter_that_takes_limits_from_the_pc(self):
        payload = {**PAYLOAD, "nozzle_mm": .6}
        state = {"extruder": {"target": 0, "nozzle_diameter": .6}, "heater_bed": {"target": 0}}
        old_adapter = {COMMAND: f"Capture one step in {SLOT_SUPPORT} (minimum {MIN_FLOW} mm3/s)", "STOCK_PA_CAPTURE": ""}
        with patch("orca_auto_pa.Sweep") as factory:
            sweep = factory.return_value
            sweep.status.return_value = state
            sweep.read.return_value = {"result": old_adapter}
            with self.assertRaisesRegex(RuntimeError, "Update the printer adapter"):
                run_calibration(f"http://{PAYLOAD['printer']}:7125", Path.cwd(), payload, threading.Event(), Mock())
            sweep.send.assert_not_called()

    def test_standalone_pa_accepts_preset_peak_within_adapter_range(self):
        payload = {**PAYLOAD, "slot": "1C", "kind": "pressure_advance", "reader": "continuous",
                   "max_flow_mm3_s": 32, "lower_flow_mm3_s": 15.08}
        self.assertEqual(check_request(payload, PAYLOAD["printer"]), ("C", "ABS"))
        self.assertEqual(check_request({**payload, "lower_flow_mm3_s": 32}, PAYLOAD["printer"]), ("C", "ABS"))
        with self.assertRaisesRegex(ValueError, "40 mm"):
            check_request({**payload, "max_flow_mm3_s": 41}, PAYLOAD["printer"])
        with self.assertRaisesRegex(ValueError, "30 mm"):
            check_request({**payload, "material": "PETG", "temperature_c": 230}, PAYLOAD["printer"])

    def test_combined_run_skips_pa_without_cutoff_and_keeps_result_on_pa_error(self):
        payload = self.combined_payload()
        with patch("orca_auto_pa.run_calibration", return_value={"recommended_mm3_s": None}) as run:
            result = run_job(PAYLOAD["printer"], Path.cwd(), payload, threading.Event(), Mock())
        run.assert_called_once()
        self.assertEqual(result["pa_state"], "skipped")
        max_flow = {"recommended_mm3_s": 23}
        with patch("orca_auto_pa.run_calibration", side_effect=[max_flow, RuntimeError("PA fault")]) as run:
            result = run_job(PAYLOAD["printer"], Path.cwd(), payload, threading.Event(), Mock())
        self.assertEqual(run.call_args.args[2]["kind"], "adaptive_pa")
        self.assertEqual(result["pa_state"], "failed")
        self.assertEqual(result["max_flow"], max_flow)

    def test_pa_above_standard_range_checks_adapter_and_motion_limits_before_preparation(self):
        payload = {**PAYLOAD, "kind": "pressure_advance", "reader": "continuous",
                   "max_flow_mm3_s": 33, "lower_flow_mm3_s": 15}
        host = f"http://{payload['printer']}:7125"
        state = {"extruder": {"target": 0, "nozzle_diameter": .4}, "heater_bed": {"target": 0}}
        commands = {"STOCK_PA_DIRECT_PA_STROKE": "", "STOCK_PA_CAPTURE": "", "STOCK_PA_DIRECT_STROKE": HOST_LIMITS}
        with patch("orca_auto_pa.Sweep") as factory, patch("orca_auto_pa.motion_limits"):
            sweep = factory.return_value
            sweep.status.return_value = state
            sweep.read.return_value = {"result": commands}
            with self.assertRaisesRegex(RuntimeError, "adapter must support"):
                run_calibration(host, Path.cwd(), payload, threading.Event(), Mock())

            extruder = {**EXTRUDER, "max_extrude_cross_section": 100, "max_extrude_only_velocity": 10}
            settings = {"result": {"status": {"configfile": {"settings": {"extruder": extruder}}}}}
            commands["STOCK_PA_DIRECT_STROKE"] = f"{EXTENDED_PA_SUPPORT} ({HOST_LIMITS})"
            sweep.read.side_effect = [{"result": commands}, settings]
            with self.assertRaisesRegex(RuntimeError, "extrusion-only limits"):
                run_calibration(host, Path.cwd(), payload, threading.Event(), Mock())
            sweep.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
