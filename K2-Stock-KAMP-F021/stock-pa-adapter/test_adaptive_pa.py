import json
import math
import unittest
from unittest.mock import Mock, patch

from adaptive_pa_motion import COUPON_CENTER, coupon_span, wall_ring
from run_adaptive_pa import BED_SOAK_SECONDS, FOUNDATION_RAMP, RESET_OBJECTS, TOWER_OBJECT
from run_adaptive_pa import first_layer_paths, grid, prepare_bed, print_first_layer, print_foundation, rank
from run_adaptive_pa import score_printed_wall, soak_bed


class AdaptivePATest(unittest.TestCase):
    def setUp(self):
        self.request = {
            "max_flow_mm3_s": 26., "filament_diameter_mm": 1.75, "pa_seed": .045,
            "walls": {"outer": {"flow": 15.08, "speed": 200, "acceleration": 2500},
                      "inner": {"flow": 24.42, "speed": 300, "acceleration": 6000}},
        }

    def test_grid_crosses_both_wall_targets_with_each_acceleration(self):
        result = grid(self.request, 40)
        self.assertEqual([(p["flow"], p["acceleration"]) for p in result],
                         [(15.08, 2500), (24.42, 2500), (15.08, 6000), (24.42, 6000)])
        self.assertEqual(coupon_span(result), 170)

    def test_actual_tower_outline_and_perimeter_spacing(self):
        start, outer = wall_ring(70, 0, 15.08, 200, 1.75)
        _, inner = wall_ring(70, .42, 15.08, 200, 1.75)
        self.assertEqual(start, (COUPON_CENTER, COUPON_CENTER + 35))
        self.assertEqual([tuple(move[:2]) for move in outer],
                         [(95, 164.75), (95, 130), (130, 95), (165, 130), (165, 164.75), start])
        self.assertAlmostEqual(inner[0][0], outer[0][0] + .42)
        self.assertEqual(len(inner), len(outer))
        first, second = outer[:2]
        self.assertAlmostEqual(first[2] / math.dist(start, first[:2]), second[2] / math.dist(first[:2], second[:2]))

    def test_equal_wall_targets_still_have_two_flows_and_accelerations(self):
        self.request["walls"]["inner"] = self.request["walls"]["outer"].copy()
        result = grid(self.request, 40)
        self.assertEqual({p["flow"] for p in result}, {7.54, 15.08})
        self.assertEqual({p["acceleration"] for p in result}, {1250, 2500})

    def test_rejects_setting_past_geometry_or_filament_limit(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            grid(self.request, 20)
        with self.assertRaisesRegex(ValueError, "longer straight"):
            coupon_span([{"speed": 400., "acceleration": 500.}])

    def test_preheat_soaks_before_probing_the_skirt_with_no_chamber_wait(self):
        for chamber in (0, 55):
            with self.subTest(chamber=chamber):
                sweep = Mock(temperature=280)
                events = Mock()
                events.attach_mock(sweep, "sweep")
                flow, inset, _ = first_layer_paths(.62, .3, .53)
                _, skirt = wall_ring(70, inset, flow, 30, 1.75)
                mesh = {"profile_name": "default", "mesh_min": [86, 83], "mesh_max": [174, 174],
                        "probed_matrix": [[0.] * 7] * 7}

                def read(path):
                    if "exclude_object" in path:
                        define = sweep.send.call_args.args[-1]
                        polygon = json.loads(define.split("POLYGON=")[1])
                        status = {"exclude_object": {"objects": [{"name": TOWER_OBJECT, "polygon": polygon}]}}
                    else:
                        status = {"bed_mesh": mesh, "configfile": {"settings": {
                            "bed_mesh": {"mesh_min": [5, 5], "mesh_max": [255, 255]}}}}
                    return {"result": {"status": status}}

                sweep.read.side_effect = read
                with patch("run_adaptive_pa.soak_bed") as soak:
                    events.attach_mock(soak, "soak")
                    result = prepare_bed(sweep, skirt, 100, chamber, Mock())
                    soak.assert_called_once_with(sweep, 100)

                commands = [call.args for call in sweep.send.call_args_list]
                heat = ["M140 S100"]
                if chamber:
                    heat.append("SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET=55")
                self.assertEqual(commands[0], (*heat, "M104 S140", "M190 S100"))
                names = [call[0] for call in events.mock_calls]
                first_send = names.index("sweep.send")
                self.assertLess(first_send, names.index("soak"))
                self.assertLess(names.index("soak"), names.index("sweep.send", first_send + 1))
                self.assertEqual(commands[3][0], RESET_OBJECTS)
                self.assertIn("POLYGON=[[", commands[3][1])
                self.assertEqual(commands[4:], [("BED_MESH_CLEAR", "BED_MESH_CALIBRATE", "M400"),
                                               (RESET_OBJECTS,), ("M109 S280", "M400")])
                self.assertEqual(result["probe_count"], [7, 7])

    def test_first_layer_primes_with_a_separate_skirt_and_uses_brim_settings(self):
        flow, skirt, borders = first_layer_paths(.62, .3, .53)
        self.assertEqual(len(borders) - 2, 9)
        self.assertAlmostEqual(-borders[0], 5., places=1)
        self.assertAlmostEqual(borders[0] - skirt - .62, 2.)
        self.assertEqual(borders[-2:], (.53, 0.))
        expected = (skirt, *borders)
        sweep = Mock()
        sweep.status.return_value = {"toolhead": {"max_accel": 20000, "max_accel_to_decel": 20000}}
        with patch("run_adaptive_pa.print_ring") as ring:
            print_first_layer(sweep, 70, expected, flow, 1.75, .3)
        self.assertEqual([call.args[2] for call in ring.call_args_list], list(expected))
        self.assertEqual(sweep.send.call_args_list[0].args[0], "SET_VELOCITY_LIMIT ACCEL=500 ACCEL_TO_DECEL=250")
        self.assertIn("ADVANCE=0", sweep.send.call_args_list[0].args[1])
        self.assertEqual(sweep.send.call_args.args, ("SET_VELOCITY_LIMIT ACCEL=20000 ACCEL_TO_DECEL=20000",))

    def test_foundation_ramps_both_walls_before_measuring(self):
        sweep = Mock()
        sweep.status.return_value = {"toolhead": {"max_accel": 20000, "max_accel_to_decel": 20000}}
        walls = self.request["walls"]
        walls["outer"]["acceleration"] = 1200
        with patch("run_adaptive_pa.print_ring") as ring:
            used = print_foundation(sweep, 170, walls, .5, 1.75, .3, .2, .045)
        self.assertEqual(used, len(FOUNDATION_RAMP))
        rings = [call.args for call in ring.call_args_list]
        self.assertEqual(len(rings), 2 * used)
        self.assertEqual([r[4] for r in rings], [30, 30, 45, 45, 60, 60])
        for z in {r[6] for r in rings}:
            self.assertIn(round(z, 6), (.5, .7, .9))
        # Each ring keeps its wall's line width at the slower speed.
        self.assertAlmostEqual(rings[0][3] / rings[0][4], walls["inner"]["flow"] / walls["inner"]["speed"])
        accels = [c.args[0] for c in sweep.send.call_args_list[:-1]]
        self.assertEqual(accels, ["SET_VELOCITY_LIMIT ACCEL=1000 ACCEL_TO_DECEL=500",
                                  "SET_VELOCITY_LIMIT ACCEL=1200 ACCEL_TO_DECEL=600",
                                  "SET_VELOCITY_LIMIT ACCEL=1200 ACCEL_TO_DECEL=600"])
        self.assertEqual(sweep.send.call_args.args, ("SET_VELOCITY_LIMIT ACCEL=20000 ACCEL_TO_DECEL=20000",))

    def test_bed_soak_checks_cancellation_and_holds_for_180_seconds(self):
        sweep = Mock()
        sweep.status.return_value = {"heater_bed": {"target": 100}}
        delays = []
        with patch("run_adaptive_pa.time.monotonic", side_effect=lambda: sum(delays)), \
                patch("run_adaptive_pa.time.sleep", side_effect=delays.append):
            soak_bed(sweep, 100)
        self.assertEqual(sum(delays), 180)
        self.assertEqual(sum(delays), BED_SOAK_SECONDS)
        self.assertLessEqual(max(delays), 2)
        self.assertEqual(sweep.check_cancel.call_count, len(delays) + 1)

    def test_scores_both_directions_and_records_cruise_and_corner_flow(self):
        area = math.pi * (.875 ** 2)
        moves = [[.35, .1, 0, 100]]
        for start in (.45, .93, 1.41):
            moves.append([start, .30, 10, 0])
            if start < 1.41:
                moves.extend([[start + .30, .09, 10, -100], [start + .39, .09, 1, 100]])
        moves.append([1.71, .1, 10, -100])
        plan = {"filament_diameter_mm": 1.75, "wall_flow_mm3_s": 10 * area,
                "xy_speed": 250, "xy_acceleration": 2500}
        data = {"moves": moves, "planned_segments": plan, "samples": []}
        for n in range(1900):
            t = n / 1000
            v = next((v + a * (t - start) for start, elapsed, v, a in moves if start <= t < start + elapsed), 0)
            data["samples"].append((t, 100000 + 6000 * v))
        scores, flow, error = score_printed_wall(data)
        self.assertIsNone(error)
        self.assertEqual(len(scores), 2)
        self.assertLess(max(scores), .1)
        self.assertAlmostEqual(flow["cruise_mm3_s"], 10 * area)
        self.assertEqual(flow["corner_flow_mm3_s"], [area, area])
        self.assertEqual(len(flow["usable_corners"]), 2)
        plan["xy_acceleration"] = 1700
        self.assertEqual(score_printed_wall(data)[2], "insufficient_usable_corners")
        plan["wall_flow_mm3_s"] = 12 * area
        self.assertEqual(score_printed_wall(data)[2], "target_flow_not_reached")

    def test_ranking_favors_a_balanced_repeated_candidate(self):
        captures = [{"pa": pa, "scores": scores} for pa, scores, count in (
            (.040, [.05, .20], 2), (.045, [.09, .09], 2), (.050, [.01, .01], 1)) for _ in range(count)]
        rows, winner = rank(captures)
        self.assertEqual(len(rows), 2)
        self.assertEqual(winner["pa"], .045)
