import json
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from analyze_sweep import score, score_edges, signed_transition_balance
from orca_auto_pa import CalibrationServer, Cancelled, check_request, prepare_slot, run_calibration
from run_direct_pa_refinement import measure as measure_direct, rank as rank_direct
from run_short_pa_sweep import (CALIBRATION_LIMITS, CALIBRATION_SMOOTH, MAX_CAPTURE_ATTEMPTS, MOTION,
                                SENSOR, Sweep, combine_flows, extrusion_moves, limit_command, preset_flows)


PRINTER = "192.0.2.118"
PAYLOAD = {"printer": PRINTER, "slot": "1D", "material": "ABS", "temperature_c": 280, "nozzle_mm": 0.4}
CAPTURES = Path(__file__).resolve().parent / "fixtures"


class SweepTest(unittest.TestCase):
    def test_direct_ranking_prefers_lower_flow_before_lower_pa(self):
        def candidate(pa, lower, peak):
            return [{"pa": pa, "band": band, "edges": {"rise": {"rms": error}, "fall": {"rms": error}}}
                    for band, error in (("LOWER", lower), ("PEAK", peak)) for _ in range(2)]

        baseline = candidate(.0500, .065, .050)
        for lower, peak, expected in ((.055, .060, .0525), (.062, .060, .0500), (.055, .075, .0500)):
            with self.subTest(lower=lower, peak=peak):
                self.assertEqual(rank_direct(baseline + candidate(.0525, lower, peak))[1]["pa"], expected)

    def test_direct_refinement_targets_closest_low_flow_compromise(self):
        scores = {
            400: (.048, .078, .101, .105),
            425: (.070, .072, .079, .088),
            450: (.051, .069, .107, .091),
            470: (.060, .058, .081, .089),
            475: (.060, .058, .082, .090),
            480: (.064, .060, .081, .087),
            500: (.076, .045, .115, .113),
            550: (.102, .052, .103, .133),
            600: (.107, .066, .139, .170),
        }
        flows = {"LOWER": 15.0832, "PEAK": 32.0}
        filament_area = math.pi * (.875 ** 2)
        report = {"prime_mm_per_stroke": 3., "captures": []}

        def read_capture(path):
            setting, stroke = Path(path).stem.rsplit("_", 2)[-2:]
            band = "LOWER" if stroke.startswith("L") else "PEAK"
            return {"setting": int(setting), "band": band, "timing": {},
                    "filament_feed_mm_s": [0., flows[band] / filament_area]}

        def score_capture(capture):
            edges = scores[capture["setting"]]
            rise, fall = edges[:2] if capture["band"] == "LOWER" else edges[2:]
            return {"rise": {"rms": rise}, "fall": {"rms": fall}, "quality": {}}

        with tempfile.TemporaryDirectory() as folder, \
                patch("run_direct_pa_refinement.score_edges", side_effect=score_capture):
            sweep = Mock(material="ABS", slot="D", temperature=280, folder=Path(folder))
            sweep.status.return_value = {"extruder": {"target": 280, "temperature": 280}}
            sweep.idle.return_value = True
            sweep.read.side_effect = read_capture
            measure_direct(sweep, flows, "test", report, update=lambda _: None, extended_stroke=True)

        coarse = [capture for capture in report["captures"] if capture["stage"] == "0.0050"]
        fine = [capture for capture in report["captures"] if capture["stage"] == "0.0025"]
        final = [capture for capture in report["captures"] if capture["stage"] == "0.0005"]
        self.assertIsNone(rank_direct(coarse)[1])
        self.assertEqual(len(fine), 8)
        self.assertEqual({capture["pa"] for capture in fine}, {.0425, .0475})
        self.assertEqual(rank_direct(coarse + fine)[1]["pa"], .0475)
        self.assertEqual(len(final), 8)
        self.assertEqual({capture["pa"] for capture in final}, {.0470, .0480})
        self.assertEqual(rank_direct(report["captures"])[1]["pa"], .0470)

    def test_single_flow_refinement_sends_only_the_outer_wall_peak(self):
        flow = 15.0832
        report = {"prime_mm_per_stroke": 3., "captures": []}
        feed = flow / (math.pi * .875 ** 2)

        def read_capture(path):
            setting = int(Path(path).stem.rsplit("_", 2)[-2])
            return {"setting": setting, "timing": {}, "filament_feed_mm_s": [0., feed]}

        def score_capture(capture):
            error = .05 + abs(capture["setting"] - 500) / 1000
            return {"rise": {"rms": error - .005}, "fall": {"rms": error}, "quality": {}}

        with tempfile.TemporaryDirectory() as folder, \
                patch("run_direct_pa_refinement.score_edges", side_effect=score_capture), patch("builtins.print"):
            sweep = Mock(material="ABS", slot="D", temperature=280, folder=Path(folder))
            sweep.status.return_value = {"extruder": {"target": 280, "temperature": 280}}
            sweep.read.side_effect = read_capture
            measure_direct(sweep, {"PEAK": flow}, "test", report, update=lambda _: None)

        strokes = [line for call in sweep.send.call_args_list for line in call.args
                   if line.startswith("STOCK_PA_DIRECT_STROKE ")]
        self.assertEqual(len(strokes), 18)
        self.assertTrue(all("FLOW=15.08320000 BAND=PEAK " in line for line in strokes))
        self.assertEqual({capture["stage"] for capture in report["captures"]}, {"0.0050", "0.0025", "0.0005"})
        rows, winner, _, _ = rank_direct(report["captures"], bands=("PEAK",))
        self.assertEqual(winner["pa"], .0495)
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(winner["scores"]), 2)

    def test_parking_exits_chute_along_y_before_crossing_x(self):
        state = {"print_stats": {"state": "standby"}, "virtual_sdcard": {"is_active": False},
                 "pause_resume": {"is_paused": False}, "webhooks": {"state": "ready"},
                 "toolhead": {"homed_axes": "xyz", "position": [115, 291.5, 5, 0]}}
        sweep = Sweep(f"http://{PRINTER}:7125", CAPTURES, "park_test", "D")
        parked = dict(state, toolhead={"homed_axes": "xyz", "position": [130, 130, 12, 0]})
        sweep.status = Mock(side_effect=[state, parked])
        sweep.send = Mock()
        sweep.read = Mock(return_value={"result": {"status": {"gcode_move": {
            "position": [115, 291.5, 5, 0], "gcode_position": [115, 291.5, 5, 0]}}}})
        sweep.park_after_purge(state)
        self.assertEqual(sweep.send.call_args_list[2].args,
                         ("M220 S100", "G90", "G1 X115.000000 Y291.500000 Z12.000000 F600", "G1 Y130.000000 F2400",
                          "G1 X130.000000 F2400", "M400"))
        self.assertEqual(sweep.send.call_args.args, ("RESTORE_GCODE_STATE NAME=k2_calibration_park MOVE=0",))
        sweep.send.reset_mock()
        sweep.park_after_purge(parked)
        sweep.send.assert_not_called()

    def test_directional_scoring_keeps_valid_edges_and_rejects_gapped_edges(self):
        paths = sorted(CAPTURES.glob("stock_pa_petg_b_short161824_sweep_070_*.json"))
        self.assertEqual(len(paths), 6)
        captures = [score_edges(json.loads(path.read_text(encoding="utf-8"))) for path in paths]
        self.assertEqual(sum(row["rise"] is not None for row in captures), 3)
        self.assertTrue(all(row["fall"] is None for row in captures))
        self.assertTrue(all(row["quality"]["fall"]["reason"] == "sample_gap_at_transition" for row in captures))

    def test_timing_sign_and_gap_at_a_flow_edge(self):
        times = [index / 1000 for index in range(1001)]
        for delay, expected_sign in ((.010, -1), (-.010, 1)):
            force = [int(.300 + delay <= t < .700 + delay) for t in times]
            signed = signed_transition_balance(times, force, .300, .700, 1)
            self.assertGreater(signed * expected_sign, .010)
        with_gap = [(t, p) for t, p in zip(times, force) if not .293 < t < .312]
        self.assertIsNone(signed_transition_balance(*zip(*with_gap), .300, .700, 1))

    def test_gapped_cycles_are_replaced_and_timing_must_agree(self):
        for zero, expected in ((.051, "accepted"), (.057, "timing_disagreement"),
                               (.080, "timing_disagreement")):
            with self.subTest(zero=zero):
                sweep = Sweep("", CAPTURES, "", "A")
                sweep.original_limits = (5000, 100)

                def group(setting, count, _smooth):
                    rows = sweep.results.setdefault(setting, [])
                    for _ in range(count):
                        rows.append(None if not rows else
                                    (.10 + abs(setting - 50) * .004, 0, 30000, setting / 1000 - zero))

                sweep.group = group
                result = sweep.adaptive_sweep(CALIBRATION_SMOOTH)
                self.assertTrue(all(len(cycles) == 4 for cycles in sweep.results.values()))
                self.assertEqual(result["reason"], expected)

    def test_missing_transition_coverage_retries_are_bounded(self):
        sweep = Sweep("", CAPTURES, "", "A")
        sweep.original_limits = (5000, 100)
        sweep.group = lambda k, count, _: sweep.results.setdefault(k, []).extend([None] * count)
        self.assertEqual(sweep.adaptive_sweep(CALIBRATION_SMOOTH)["reason"], "insufficient_repeats")
        self.assertTrue(all(len(cycles) == MAX_CAPTURE_ATTEMPTS for cycles in sweep.results.values()))

    def test_controlled_run_restores_printer_motion_and_pa_on_error(self):
        with tempfile.TemporaryDirectory() as output:
            sweep = Sweep("", Path(output), "", "A")
            state = {"print_stats": {"state": "standby"}, "virtual_sdcard": {"is_active": False},
                     "pause_resume": {"is_paused": False}, "webhooks": {"state": "ready"},
                     "extruder": {"target": 0, "nozzle_diameter": .4, "pressure_advance": .047, "smooth_time": .038},
                     "heater_bed": {"target": 0}, "k2_stock_pa_capture": {"windows": 22},
                     "toolhead": {"homed_axes": "xyz", "position": [130, 130, 12, 0],
                                  "max_accel": 5000, "max_accel_to_decel": 100}}
            sweep.status = lambda: state
            with patch.object(sweep, "check_filament"), patch.object(sweep, "check_position"), \
                    patch.object(sweep, "send") as send, patch.object(sweep, "measure", side_effect=ValueError("test abort")) as measure:
                with self.assertRaisesRegex(ValueError, "test abort"):
                    sweep.run()
            measure.assert_called_once_with(sweep.feeds, "sweep", CALIBRATION_SMOOTH)
            self.assertEqual(send.call_args.args[-1], limit_command((5000, 100)))
            self.assertNotEqual(send.call_args.args[-1], limit_command(CALIBRATION_LIMITS))

    def test_peak_uses_preset_limit_and_keeps_purge_stroke(self):
        normal, peak = preset_flows(20, 1.75)
        self.assertEqual(normal, (2.0, 5.0, 2.0))
        self.assertAlmostEqual(peak[1], 8.315, places=3)
        moves, lengths = extrusion_moves(peak)
        self.assertEqual([line.split()[1] for line in moves], ["X115.34", "X115.94", "X116.28"])
        commanded_peak = lengths[1] / MOTION[1][2] * math.pi * (1.75 / 2) ** 2
        self.assertLessEqual(commanded_peak, 20)
        self.assertAlmostEqual(commanded_peak, 20, places=4)
        limited_normal, _ = preset_flows(8, 1.75)
        self.assertAlmostEqual(limited_normal[1] * math.pi * (1.75 / 2) ** 2, 6)

    def test_profile_lower_flow_replaces_the_default_at_each_calibration(self):
        area = math.pi * (1.75 / 2) ** 2
        for target in (15.0832, 11.3124):
            with self.subTest(target=target):
                lower, high = preset_flows(22.5, 1.75, target)
                self.assertAlmostEqual(lower[1] * area, target)
                self.assertAlmostEqual(lower[0] * area, .4 * target)
                self.assertAlmostEqual(high[1] * area, 22.5)
                _, lengths = extrusion_moves(lower)
                self.assertAlmostEqual(lengths[1] * area / MOTION[1][2], target, places=4)

    def test_high_flow_capture_uses_its_own_cruise_velocities(self):
        source = CAPTURES / "stock_pa_abs_d_short095338_sweep_047_1.json"
        capture = json.loads(source.read_text(encoding="utf-8"))
        _, peak = preset_flows(20, 1.75)
        for move in capture["moves"]:
            if move[3] == 0 and abs(move[2] - 2) < .01:
                move[2] = peak[0]
            elif move[3] == 0 and abs(move[2] - 5) < .01:
                move[2] = peak[1]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / source.name
            path.write_text(json.dumps(capture), encoding="utf-8")
            self.assertIsNone(score(path))
            capture["filament_feed_mm_s"] = peak
            path.write_text(json.dumps(capture), encoding="utf-8")
            self.assertEqual(score(path), score(source))

    def test_both_flows_must_agree_to_auto_save(self):
        normal = {"reason": "accepted", "best_pa": .047}
        peak = {"reason": "accepted", "best_pa": .057}
        bands = [{"peak_mm3_s": 12}, {"peak_mm3_s": 20}]
        self.assertEqual(combine_flows(normal, peak, bands)["reason"], "flow_disagreement")
        self.assertEqual(combine_flows(normal, normal, bands), normal)
        self.assertEqual(combine_flows(normal, {"reason": "close_scores"}, bands),
                         {"reason": "close_scores", "tested_flow_mm3_s": 20})

    def test_recorded_abs_and_petg_pass(self):
        for slot, stamp, candidate, tested in (
            ("D", "095338", 47, [47, 57, 67, 37]),
            ("A", "094416", 50, [40, 50, 60]),
        ):
            with self.subTest(slot=slot):
                sweep = Sweep("", CAPTURES, stamp, slot)

                def group(setting, count, _smooth):
                    rows = sweep.results.setdefault(setting, [])
                    for repeat in range(len(rows) + 1, len(rows) + count + 1):
                        path = CAPTURES / f"stock_pa_{sweep.run_name}_sweep_{setting:03d}_{repeat}.json"
                        rows.append(score(path))

                sweep.group = group
                outcome = sweep.adaptive_sweep(0.038)
                self.assertEqual(outcome["reason"], "accepted")
                self.assertEqual(outcome["best_pa"], candidate / 1000)
                self.assertEqual(list(sweep.results), tested)

    def test_live_abs_run_reports_close_scores(self):
        sweep = Sweep("", CAPTURES, "120129", "D")

        def group(setting, count, _smooth):
            rows = sweep.results.setdefault(setting, [])
            for repeat in range(len(rows) + 1, len(rows) + count + 1):
                path = CAPTURES / f"stock_pa_{sweep.run_name}_sweep_{setting:03d}_{repeat}.json"
                rows.append(score(path))

        sweep.group = group
        assessment = sweep.adaptive_sweep(0.038)
        self.assertEqual(assessment["reason"], "close_scores")
        self.assertEqual(assessment["best_pa"], .047)
        self.assertEqual(assessment["second_pa"], .057)
        self.assertAlmostEqual(assessment["score_lead"], .0184, places=4)
        self.assertEqual(assessment["minimum_lead"], .020)

    def test_live_petg_replay_extends_past_the_first_edge_winner(self):
        sweep = Sweep("", CAPTURES, "153840", "A")
        # Replay the recorded run up to 0.070, then simulate the first
        # measurement the earlier implementation never scheduled.
        def group(setting, count, _smooth):
            rows = sweep.results.setdefault(setting, [])
            for index in range(len(rows) + 1, len(rows) + count + 1):
                path = CAPTURES / f"stock_pa_{sweep.run_name}_sweep_{setting:03d}_{index}.json"
                rows.append((.16, 0, 1) if setting == 80 else score(path))

        sweep.group = group
        result = sweep.adaptive_sweep(.04)
        self.assertIn(80, sweep.results)
        self.assertEqual(result["best_pa"], .070)
        self.assertEqual(result["reason"], "accepted")

    def test_sweep_stops_at_search_budget_without_accepting_an_edge(self):
        for direction, edge, count in ((1, .100, 7), (-1, .010, 6)):
            with self.subTest(direction=direction):
                sweep = Sweep("", CAPTURES, "", "A")

                def group(setting, count, _smooth):
                    sweep.results.setdefault(setting, []).extend([(.5 - setting * direction * .003, 0, 1)] * count)

                sweep.group = group
                result = sweep.adaptive_sweep(.04)
                self.assertEqual(len(sweep.results), count)
                self.assertEqual((result["reason"], result["best_pa"]), ("unbracketed", edge))

    def test_missing_neighbor_does_not_pass(self):
        sweep = Sweep("", CAPTURES, "", "D")

        def group(setting, _count, _smooth):
            points = {47: [None, None], 57: [(0.05, 0, 1)] * 2, 67: [(0.15, 0, 1)] * 2}
            sweep.results[setting] = points[setting]

        sweep.group = group
        self.assertEqual(sweep.adaptive_sweep(0.038)["reason"], "unbracketed")

    def test_inconsistent_repeats_do_not_pass(self):
        sweep = Sweep("", CAPTURES, "", "D")

        def group(setting, _count, _smooth):
            errors = {47: (0.10, 0.10), 57: (0.11, 0.03), 67: (0.15, 0.15)}
            sweep.results[setting] = [(error, 0, 1) for error in errors[setting]]

        sweep.group = group
        self.assertEqual(sweep.adaptive_sweep(0.038)["reason"], "inconsistent_repeats")


class HelperTest(unittest.TestCase):
    def test_request_only_accepts_the_configured_device_and_verified_settings(self):
        self.assertEqual(check_request(PAYLOAD, PRINTER), ("D", "ABS"))
        self.assertEqual(check_request({**PAYLOAD, "slot": "1B", "material": "PETG", "temperature_c": 240},
                                       PRINTER), ("B", "PETG"))
        self.assertEqual(check_request({**PAYLOAD, "nozzle_mm": .6}, PRINTER), ("D", "ABS"))
        self.assertEqual(check_request({**PAYLOAD, "temperature_c": 250}, PRINTER), ("D", "ABS"))
        self.assertEqual(check_request({**PAYLOAD, "slot": "1B", "material": "PETG", "temperature_c": 255}, PRINTER),
                         ("B", "PETG"))
        for change in ({"printer": "different"}, {"slot": "2D"}, {"material": "PLA"},
                       {"temperature_c": 400}, {"temperature_c": 100}, {"temperature_c": "250"}, {"nozzle_mm": 0.2}, {"nozzle_mm": "0.6"}, {"max_flow_mm3_s": 0},
                       {"max_flow_mm3_s": 20}, {"filament_diameter_mm": 1.75}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_request({**PAYLOAD, **change}, PRINTER)
        self.assertEqual(check_request({**PAYLOAD, "max_flow_mm3_s": 20, "filament_diameter_mm": 1.75},
                                       PRINTER), ("D", "ABS"))
        request = {**PAYLOAD, "max_flow_mm3_s": 22.5, "filament_diameter_mm": 1.75}
        self.assertEqual(check_request({**request, "lower_flow_mm3_s": 15.0832}, PRINTER), ("D", "ABS"))
        for lower in (0, 22.5, 24, float("nan"), "15.0832"):
            with self.subTest(lower=lower), self.assertRaises(ValueError):
                check_request({**request, "lower_flow_mm3_s": lower}, PRINTER)

    def test_preparation_uses_the_selected_stock_slot(self):
        state = {"toolhead": {"homed_axes": "xyz"}, "box": {"T1": {"filament": "A"}},
                 SENSOR: {"filament_detected": True}}
        with patch("orca_auto_pa.Sweep") as factory:
            sweep = factory.return_value
            sweep.slot, sweep.temperature = "D", 280
            sweep.status.side_effect = [{SENSOR: {"filament_detected": False}}, state]
            prepare_slot(sweep, state)
            commands = [line for call in sweep.send.call_args_list for line in call.args]
        self.assertLess(commands.index("BOX_QUIT_MATERIAL"),
                        commands.index("BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL TNN=T1D"))
        self.assertIn("BOX_LOAD_MATERIAL_MATERIAL_FLUSH TNN=T1D", commands)
        self.assertIn("BOX_NOZZLE_CLEAN", commands)
        self.assertEqual(commands[-2:], ["M109 S280", "M400"])

    def test_continuous_pa_recovers_unassigned_cfs_filament_before_selecting_slot_c(self):
        state = {"toolhead": {"homed_axes": ""}, "box": {"enable": 1, "filament": 1, "T1": {"filament": "None"}},
                 SENSOR: {"filament_detected": True}}
        with patch("orca_auto_pa.Sweep") as factory:
            sweep = factory.return_value
            sweep.slot, sweep.temperature = "C", 230
            sweep.status.side_effect = [{SENSOR: {"filament_detected": False}}, state, state]
            prepare_slot(sweep, state, recover_unassigned=True)
            commands = [line for call in sweep.send.call_args_list for line in call.args]
        self.assertIn("G28", commands)
        self.assertEqual(commands[4:8], ["BOX_LOAD_MATERIAL_HEATING", "BOX_LOAD_MATERIAL_CUT_MATERIAL",
                                         "BOX_LOAD_MATERIAL_RETRUDE_MATERIAL", "M400"])
        self.assertLess(commands.index("BOX_LOAD_MATERIAL_RETRUDE_MATERIAL"),
                        commands.index("BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL TNN=T1C"))
        self.assertNotIn("BOX_QUIT_MATERIAL", commands)
        self.assertIn("BOX_LOAD_MATERIAL_MATERIAL_FLUSH TNN=T1C", commands)

    def test_cfs_recovery_stops_if_filament_still_blocks_the_sensor(self):
        state = {"toolhead": {"homed_axes": "xyz"}, "box": {"enable": 1, "filament": 1, "T1": {"filament": "None"}},
                 SENSOR: {"filament_detected": True}}
        with patch("orca_auto_pa.Sweep") as factory:
            sweep = factory.return_value
            sweep.slot = "C"
            sweep.status.return_value = state
            with self.assertRaisesRegex(RuntimeError, "did not clear"):
                prepare_slot(sweep, state, recover_unassigned=True)
            commands = [line for call in sweep.send.call_args_list for line in call.args]
            self.assertNotIn("BOX_LOAD_MATERIAL_EXTRUDE_MATERIAL TNN=T1C", commands)
            self.assertEqual(commands[-1], "BOX_RESTORE_FAN")
            sweep.send.reset_mock()
            state["box"]["enable"] = 0
            with self.assertRaisesRegex(RuntimeError, "cannot confirm CFS unit 1"):
                prepare_slot(sweep, state, recover_unassigned=True)
            sweep.send.assert_not_called()

    def test_inconclusive_or_cancelled_run_does_not_publish(self):
        state = {"extruder": {"target": 0, "nozzle_diameter": 0.4,
                              "pressure_advance": 0.038, "smooth_time": 0.038},
                 "heater_bed": {"target": 0}, "k2_stock_pa_capture": {"windows": 22}}
        for cancellation in (False, True):
            cancel = threading.Event()
            with self.subTest(cancellation=cancellation), patch("orca_auto_pa.Sweep") as factory, \
                    patch("orca_auto_pa.prepare_slot"):
                sweep = factory.return_value
                sweep.status.return_value = state

                def finish(preheated, nozzle_mm):
                    self.assertTrue(preheated)
                    self.assertEqual(nozzle_mm, .4)
                    if cancellation:
                        cancel.set()
                    return {"candidate": None}

                sweep.run.side_effect = finish
                args = (f"http://{PRINTER}:7125", CAPTURES, PAYLOAD, cancel, lambda _: None)
                if cancellation:
                    with self.assertRaises(Cancelled):
                        run_calibration(*args)
                else:
                    self.assertIsNone(run_calibration(*args)["candidate"])
                sweep.read.assert_not_called()
                self.assertEqual(sweep.send.call_args.args[0], "M104 S0")

    def test_candidate_is_returned_and_published_after_cleanup(self):
        state = {"extruder": {"target": 0, "nozzle_diameter": 0.4,
                              "pressure_advance": 0.038, "smooth_time": 0.038},
                 "heater_bed": {"target": 0}, "k2_stock_pa_capture": {"windows": 22}}
        with patch("orca_auto_pa.Sweep") as factory, patch("orca_auto_pa.prepare_slot"):
            sweep = factory.return_value
            sweep.status.return_value = state
            sweep.run.return_value = {"candidate": 0.047}
            sweep.run_name = "abs_d_short_test"
            sweep.material_code = "000004"
            sweep.temperature = 280

            def publish(path, body):
                self.assertEqual(path, "/server/database/item")
                self.assertEqual(body["key"], "slot_d_abs")
                self.assertEqual(body["value"]["pressure_advance"], 0.047)
                self.assertEqual(sweep.send.call_args.args[0], "M104 S0")

            sweep.read.side_effect = publish
            result = run_calibration(f"http://{PRINTER}:7125", CAPTURES, PAYLOAD,
                                     threading.Event(), lambda _: None)
            self.assertEqual(result, {"candidate": .047, "slot": "1D", "material": "ABS",
                                      "nozzle_mm": .4, "temperature_c": 280})
            sweep.read.assert_called_once()

    def test_nozzle_mismatch_stops_before_printer_commands(self):
        with patch("orca_auto_pa.Sweep") as factory:
            sweep = factory.return_value
            sweep.status.return_value = {"extruder": {"nozzle_diameter": .4}}
            request = {**PAYLOAD, "nozzle_mm": .6}
            with self.assertRaisesRegex(RuntimeError, "Orca selected 0.6 mm, but the K2 reports 0.4 mm"):
                run_calibration(f"http://{PRINTER}:7125", CAPTURES, request, threading.Event(), Mock())
            sweep.send.assert_not_called()
            sweep.read.assert_not_called()

    def test_preset_flow_is_forwarded_and_firmware_limit_checked_first(self):
        state = {"extruder": {"target": 0, "nozzle_diameter": 0.4,
                              "pressure_advance": 0.038, "smooth_time": 0.038},
                 "heater_bed": {"target": 0}, "k2_stock_pa_capture": {"windows": 22}}
        config = {"filament_diameter": 1.75, "max_extrude_cross_section": 100}
        settings = {"result": {"status": {"configfile": {"settings": {"extruder": config}}}}}
        request = {**PAYLOAD, "max_flow_mm3_s": 20, "filament_diameter_mm": 1.75, "lower_flow_mm3_s": 15.0832}

        def notify(_):
            pass

        with patch("orca_auto_pa.Sweep") as factory, patch("orca_auto_pa.prepare_slot"):
            sweep = factory.return_value
            sweep.status.return_value = state
            sweep.read.return_value = settings
            sweep.run.return_value = {"candidate": None}
            args = (f"http://{PRINTER}:7125", CAPTURES, request, threading.Event(), notify)
            run_calibration(*args)
            sweep.run.assert_called_once_with(preheated=True, max_flow=20, diameter=1.75, update=notify,
                                              lower_flow=15.0832, nozzle_mm=.4)
            sweep.send.reset_mock()
            config["max_extrude_cross_section"] = 1
            with self.assertRaisesRegex(RuntimeError, "firmware's extrusion limit"):
                run_calibration(*args)
            sweep.send.assert_not_called()

    def test_continuous_gui_run_uses_slot_c_and_its_preset_temperature(self):
        request = {**PAYLOAD, "slot": "1C", "material": "PETG", "temperature_c": 230, "reader": "continuous", "nozzle_mm": .6,
                   "max_flow_mm3_s": 20, "filament_diameter_mm": 1.75}
        state = {"extruder": {"target": 0, "nozzle_diameter": .6, "pressure_advance": .038, "smooth_time": .038},
                 "heater_bed": {"target": 0}, "k2_stock_pa_capture": {"samples": 700},
                 "toolhead": {"homed_axes": "xyz", "max_accel": 5000, "max_accel_to_decel": 100}}

        def read(route, body=None):
            if route == "/printer/gcode/help":
                return {"result": {"STOCK_PA_DIRECT_PA_STROKE": "", "STOCK_PA_CAPTURE": ""}}
            if route == "/printer/objects/query?configfile=settings":
                extruder = {"filament_diameter": 1.75, "max_extrude_cross_section": 100}
                return {"result": {"status": {"configfile": {"settings": {"extruder": extruder}}}}}
            self.assertEqual(route, "/server/database/item")
            self.assertEqual(body["key"], "slot_c_petg_0.6mm")
            self.assertEqual(body["value"]["nozzle_mm"], .6)
            self.assertEqual(body["value"]["validation"], "continuous_pressure_sweep")
            self.assertEqual(body["value"]["outer_wall_flow_mm3_s"], 20)

        def measure(sweep, flows, _stamp, report, *, update, diameter, extended_stroke):
            self.assertEqual(sweep.temperature, 230)
            self.assertEqual(flows, {"PEAK": 20})
            self.assertEqual(diameter, 1.75)
            self.assertFalse(extended_stroke)
            self.assertEqual(report["prime_mm_per_stroke"], 3)
            for pa, error in ((.0600, .06), (.0625, .045)):
                for band in flows:
                    for _ in range(2):
                        report["captures"].append({"pa": pa, "band": band,
                                                   "edges": {"rise": {"rms": error}, "fall": {"rms": error}}})

        with tempfile.TemporaryDirectory() as output, patch("orca_auto_pa.Sweep") as factory, \
                patch("orca_auto_pa.prepare_slot") as prepare, patch("orca_auto_pa.measure_direct", side_effect=measure):
            sweep = factory.return_value
            sweep.status.return_value = state
            sweep.read.side_effect = read
            sweep.folder = Path(output)
            sweep.run_name = "slot_c"
            sweep.material_code = "000003"
            result = run_calibration(f"http://{PRINTER}:7125", Path(output), request, threading.Event(), lambda _: None)
            self.assertEqual(result["candidate"], .0625)
            self.assertEqual(result["nozzle_mm"], .6)
            prepare.assert_called_once_with(sweep, state, recover_unassigned=True)
            self.assertEqual(result["slot"], "1C")
            self.assertEqual(result["selected_scores"], [.045] * 2)
            self.assertEqual(result["flow_sweeps"], [{"peak_mm3_s": 20}])
            saved = json.loads(Path(result["report_file"]).read_text())
            self.assertEqual(saved["nozzle_mm"], .6)
            self.assertEqual(len(saved["captures"]), 4)
            self.assertEqual(sweep.check_filament.call_count, 1)
            self.assertTrue(sweep.send.call_args.args[0].startswith("STOCK_PA_CAPTURE "))

    def test_loopback_job_status_cancel_and_overlap(self):
        ready = threading.Event()

        def fake_calibration(_host, _output, payload, cancel, update):
            update("Measuring.")
            ready.set()
            if payload["slot"] == "1D":
                if not cancel.wait(3):
                    raise RuntimeError("Test timed out")
                raise Cancelled("Canceled by Orca.")
            return {"candidate": 0.05, "slot": payload["slot"]}

        with patch("orca_auto_pa.run_calibration", side_effect=fake_calibration):
            with CalibrationServer(0, f"http://{PRINTER}:7125", CAPTURES) as server:
                base = f"http://127.0.0.1:{server.server_port}/calibrations"
                thread = threading.Thread(target=server.serve_forever)
                thread.start()

                def request(url, method, data=None):
                    encoded = json.dumps(data).encode() if data is not None else None
                    query = Request(url, data=encoded, method=method,
                                    headers={"Content-Type": "application/json"})
                    with urlopen(query, timeout=2) as response:
                        return json.load(response)

                def wait_for(url, state):
                    for _ in range(100):
                        received = request(url, "GET")
                        if received["state"] == state:
                            return received
                        time.sleep(.01)
                    self.fail(f"Job did not reach {state}.")

                try:
                    self.assertEqual(request(base.replace("/calibrations", "/health"), "GET"),
                                     {"service": "k2-auto-pa", "printer": PRINTER, "preset_flow": True,
                                      "outer_wall_flow": True, "direct_pa_refinement": True,
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
                                  "supported_nozzles_mm": [.4, .6],
                                  "pa_peak_limit_mm3_s": 24})
                    first = request(base, "POST", PAYLOAD)
                    first_url = f"{base}/{first['id']}"
                    self.assertTrue(ready.wait(2))
                    self.assertEqual(request(first_url, "GET")["message"], "Measuring.")
                    with self.assertRaises(HTTPError) as overlapping:
                        request(base, "POST", PAYLOAD)
                    self.assertEqual(overlapping.exception.code, 409)
                    request(first_url, "DELETE")
                    wait_for(first_url, "canceled")
                    second = request(base, "POST", {**PAYLOAD, "slot": "1B"})
                    result = wait_for(f"{base}/{second['id']}", "complete")["result"]
                    self.assertEqual(result, {"candidate": .05, "slot": "1B"})
                    self.assertEqual(request(first_url, "GET")["state"], "canceled")
                finally:
                    server.cancel.set()
                    server.shutdown()
                    thread.join()


if __name__ == "__main__":
    unittest.main()
