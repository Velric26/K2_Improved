import ast
import unittest
from pathlib import Path
from unittest.mock import Mock

from orca_auto_pa import Cancelled, unverified_cleanup

HERE = Path(__file__).resolve().parent


def state(print_state="standby"):
    return {"print_stats": {"state": print_state}}


class PrinterSideConstantsTest(unittest.TestCase):
    def test_printer_adapter_holds_no_calibration_constants(self):
        tree = ast.parse((HERE / "k2_stock_pa_direct.py").read_text(encoding="utf-8"))
        names = {target.id for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)}
        for name in ("STROKE_PROFILES", "EQUIVALENT_CODES", "MAP_FLOWS", "ABS_06_CEILING",
                     "PA_STROKE_LIMIT", "MAX_TEST_FLOW", "SERIES_FLOWS"):
            self.assertNotIn(name, names)
        source = (HERE / "k2_stock_pa_direct.py").read_text(encoding="utf-8")
        for code in ("000003", "000004", "007001"):
            self.assertNotIn(code, source)


class CleanupTest(unittest.TestCase):
    def sweep(self, print_state):
        sweep = Mock()
        sweep.status.return_value = state(print_state)
        sweep.idle.return_value = print_state == "standby"
        return sweep

    def test_idle_printer_continues_to_normal_cleanup(self):
        sweep = self.sweep("standby")
        unverified_cleanup(sweep, None, False)
        sweep.send.assert_not_called()

    def test_busy_printer_keeps_the_original_error_and_cuts_heaters(self):
        sweep = self.sweep("error")
        original = RuntimeError("Probe failed.")
        with self.assertRaisesRegex(RuntimeError, "Probe failed. .*heaters were switched off") as caught:
            unverified_cleanup(sweep, original, True)
        self.assertIs(caught.exception.__cause__, original)
        sweep.send.assert_called_once_with("M104 S0", "M140 S0")

    def test_cancel_stays_a_cancel_and_a_running_print_is_left_alone(self):
        sweep = self.sweep("printing")
        with self.assertRaisesRegex(Cancelled, "Check the heaters"):
            unverified_cleanup(sweep, Cancelled("Calibration canceled."), False)
        sweep.send.assert_not_called()

    def test_unreachable_printer_reports_the_original_error(self):
        sweep = Mock()
        sweep.status.side_effect = OSError("timed out")
        with self.assertRaisesRegex(RuntimeError, "Probe failed. Cleanup could not reach the printer"):
            unverified_cleanup(sweep, RuntimeError("Probe failed."), False)


class StallWatchTest(unittest.TestCase):
    def test_reports_only_long_pauses(self):
        import orca_auto_pa
        times = iter([0, .25, 1, 1.3, 2, 4.5])
        logged = []
        orca_auto_pa.watch_for_stalls(clock=lambda: next(times), sleep=lambda s: None, log=logged.append, rounds=3)
        self.assertEqual(len(logged), 1)
        self.assertIn("stalled for 2.2 s", logged[0])


if __name__ == "__main__":
    unittest.main()
