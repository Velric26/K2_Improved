import json
import math
import tempfile
import unittest
from pathlib import Path

from flow_model import FlowModel, cutoff, fit_line, rebuild


def run_points(slope, intercept=-.05, flows=(15, 25, 30, 32, 33)):
    return [(flow, intercept + slope * flow) for flow in flows]


class FlowModelTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "flow_model.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_no_runs_means_no_prediction(self):
        self.assertIsNone(FlowModel(self.path).predict("ABS", .4, 250))

    def test_one_run_predicts_its_own_conditions_and_scales_with_the_priors(self):
        model = FlowModel(self.path)
        model.add_run("ABS", .4, 280, run_points(.0305))
        intercept, slope = model.predict("ABS", .4, 280)
        self.assertAlmostEqual(slope, .0305, places=6)
        self.assertAlmostEqual(intercept, -.05)
        # Bigger nozzle and hotter plastic both lower the slope; cooler plastic raises it.
        self.assertLess(model.predict("ABS", .6, 280)[1], slope)
        self.assertGreater(model.predict("ABS", .4, 250)[1], slope)
        self.assertIsNone(model.predict("PETG", .4, 240))

    def test_runs_at_two_nozzles_fit_the_nozzle_exponent(self):
        model = FlowModel(self.path)
        for _ in range(4):
            model.add_run("ABS", .4, 280, run_points(.0305))
            model.add_run("ABS", .6, 280, run_points(.021, flows=(15, 39, 44, 47, 48)))
        params = model.parameters("ABS")
        measured = math.log(.0305 / .021) / math.log(.6 / .4)
        self.assertAlmostEqual(params["nozzle_exponent"], measured, delta=.1)
        self.assertAlmostEqual(model.predict("ABS", .6, 280)[1], .021, delta=.001)

    def test_storage_does_not_grow_with_runs(self):
        model = FlowModel(self.path)
        model.add_run("ABS", .4, 280, run_points(.03))
        size = self.path.stat().st_size
        for _ in range(20):
            model.add_run("ABS", .4, 280, run_points(.03))
        self.assertLess(abs(self.path.stat().st_size - size), 64)
        self.assertEqual(json.loads(self.path.read_text())["materials"]["ABS"]["runs"], 21)

    def test_short_or_flat_runs_are_ignored(self):
        model = FlowModel(self.path)
        self.assertIsNone(model.add_run("ABS", .4, 280, [(15, .4), (17, .5), (19, .6)]))
        self.assertIsNone(model.add_run("ABS", .4, 280, [(15, .5), (25, .5), (35, .5)]))
        self.assertFalse(self.path.exists())

    def test_rebuild_uses_only_finished_runs(self):
        folder = Path(self.folder.name)
        good = {"reason": "current_cutoff", "material": "ABS", "nozzle_mm": .4, "temperature_c": 280,
                "steps": [{"flow_mm3_s": f, "motor_median_abs_iq": iq} for f, iq in run_points(.03)]}
        bad = {**good, "reason": "interrupted"}
        (folder / "a.json").write_text(json.dumps(good))
        (folder / "b.json").write_text(json.dumps(bad))
        self.assertEqual(rebuild([folder / "a.json", folder / "b.json"], self.path), 1)

    def test_line_helpers(self):
        self.assertEqual(fit_line([(15, .5)]), None)
        intercept, slope = fit_line([(10, .3), (20, .6)])
        self.assertAlmostEqual(cutoff(intercept, slope), 100 / 3)


if __name__ == "__main__":
    unittest.main()
