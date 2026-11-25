import unittest
from pathlib import Path
from unittest.mock import patch

import orca_auto_pa
from run_short_pa_sweep import Sweep


def status(codes):
    letters = "ABCD"
    return {"result": {"status": {
        "extruder": {"nozzle_diameter": 0.6},
        "box": {"state": "connect", "map": {f"T1{x}": f"T1{x}" for x in letters},
                "T1": {"filament": "None", "material_type": list(codes)}}}}}


class MaterialCodeTests(unittest.TestCase):
    def test_creality_abs_code_is_listed_as_abs(self):
        with patch.object(Sweep, "read", return_value=status(("000003", "000003", "007001", "000004"))):
            slots = orca_auto_pa.calibration_setup("http://192.0.2.10:7125", Path("."))["slots"]
        self.assertEqual([(s["slot"], s["material"]) for s in slots],
                         [("1A", "PETG"), ("1B", "PETG"), ("1C", "ABS"), ("1D", "ABS")])

    def test_unknown_code_is_still_skipped(self):
        with patch.object(Sweep, "read", return_value=status(("000003", "000003", "009999", "000004"))):
            slots = orca_auto_pa.calibration_setup("http://192.0.2.10:7125", Path("."))["slots"]
        self.assertEqual([s["slot"] for s in slots], ["1A", "1B", "1D"])

    def test_sweep_accepts_creality_abs_in_slot_c(self):
        sweep = Sweep("http://192.0.2.10:7125", Path("."), "t", "C", "ABS")
        sweep.check_slot(status(("000003", "000003", "007001", "000004"))["result"]["status"])
        with self.assertRaises(RuntimeError):
            Sweep("http://192.0.2.10:7125", Path("."), "t", "C", "PETG").check_slot(
                status(("000003", "000003", "007001", "000004"))["result"]["status"])


if __name__ == "__main__":
    unittest.main()
