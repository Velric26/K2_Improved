import json
import math
import unittest
from unittest.mock import Mock, patch

from adaptive_pa_motion import COUPON_CENTER, coupon_span, wall_ring
from run_adaptive_pa import FOUNDATION_RAMP, RESET_OBJECTS, START_PRINT_TIMEOUT, TOWER_OBJECT
from run_adaptive_pa import first_layer_paths, grid, prepare_bed, print_first_layer, print_foundation, rank
from run_adaptive_pa import score_printed_wall, start_print_line


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

    def test_first_layer_primes_with_a_separate_skirt_and_uses_brim_settings(self):
        flow, skirt, borders = first_layer_paths(.62, .3, .53)
        self.assertEqual(len(borders) - 2, 36)
        self.assertAlmostEqual(-borders[0], 20., places=0)
        self.assertAlmostEqual(borders[0] - skirt - .62, 2.)
        self.assertEqual(borders[-2:], (.53, 0.))
        expected = (skirt, *borders)
        sweep = Mock()
        sweep.status.return_value = {"toolhead": {"max_accel": 20000, "max_accel_to_decel": 20000}}
        with patch("run_adaptive_pa.print_ring") as ring:
            print_first_layer(sweep, 70, expected, flow, 1.75, .3)
        self.assertEqual([call.args[2] for call in ring.call_args_list], list(expected))
        self.assertEqual(sweep.send.call_args_list[0].args[:3],
                         ("M106 S0", "M106 P2 S0", "SET_VELOCITY_LIMIT ACCEL=500 ACCEL_TO_DECEL=250"))
        self.assertIn("ADVANCE=0", sweep.send.call_args_list[0].args[3])
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

    def probe(self, mesh_min, mesh_max, line="START_PRINT EXTRUDER_TEMP=250 BED_TEMP=100 MATERIAL=ABS"):
        skirt = [(72.4, 63.0, 0), (187.6, 187.6, 0)]
        sweep = Mock()
        sweep.idle.return_value = True
        defined = []

        def read(path):
            if "exclude_object" in path:
                polygon = json.loads(sweep.send.call_args.args[-1].split("POLYGON=")[1])
                defined.append(polygon)
                status = {"exclude_object": {"objects": [{"name": TOWER_OBJECT, "polygon": polygon}]}}
            else:
                status = {"bed_mesh": {"profile_name": "default", "mesh_min": mesh_min, "mesh_max": mesh_max,
                                       "probed_matrix": [[0.] * 9] * 9},
                          "configfile": {"settings": {"bed_mesh": {"mesh_min": [5, 5], "mesh_max": [255, 255]}}}}
            return {"result": {"status": status}}

        sweep.read.side_effect = read
        report = {}
        result = prepare_bed(sweep, skirt, line, report, Mock())
        self.sends = [call for call in sweep.send.call_args_list]
        self.sweep, self.report, self.defined = sweep, report, defined
        return result

    def test_startup_runs_the_printers_start_print_with_the_tower_as_mesh_object(self):
        line = "START_PRINT EXTRUDER_TEMP=250 BED_TEMP=100 MATERIAL=ABS CHAMBER_TEMP=55 CHAMBER_MIN=50"
        self.probe([71, 62], [189, 189], line)
        define, start, reset = self.sends
        self.assertEqual(define.args[0], RESET_OBJECTS)
        self.assertIn(f"NAME={TOWER_OBJECT}", define.args[1])
        self.assertEqual(self.defined[0], [[71.4, 62.0], [188.6, 62.0], [188.6, 188.6], [71.4, 188.6]])
        self.assertEqual(start.args, ("G90", line, "M400"))
        self.assertEqual(start.kwargs["timeout"], START_PRINT_TIMEOUT)
        self.assertEqual(reset.args, (RESET_OBJECTS,))
        self.assertEqual(self.report["start_print"], line)
        self.sweep.park_after_purge.assert_called_once()

    def test_start_print_line_matches_orca_machine_start_gcode(self):
        self.assertEqual(start_print_line(250, "ABS", 100, 55, 50),
                         "START_PRINT EXTRUDER_TEMP=250 BED_TEMP=100 MATERIAL=ABS CHAMBER_TEMP=55 CHAMBER_MIN=50")
        self.assertEqual(start_print_line(240, "PETG", 80),
                         "START_PRINT EXTRUDER_TEMP=240 BED_TEMP=80 MATERIAL=PETG")

    def test_mesh_rounded_inside_the_margin_still_covers_the_skirt(self):
        # KAMP stopped 0.05 mm short of the 1 mm margin on a real 20 mm brim run.
        self.assertEqual(self.probe([71.38, 62.01], [188.58, 188.57])["probe_count"], [9, 9])
        with self.assertRaisesRegex(RuntimeError, "did not cover"):
            self.probe([72.5, 62.01], [188.58, 188.57])
        with self.assertRaisesRegex(RuntimeError, "did not cover"):
            self.probe([71.38, 62.01], [188.58, 187.5])

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
