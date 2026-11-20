"""Score stock K2 pressure traces from a slow-fast-slow PA sweep."""

import argparse
import bisect
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

from analyze_capture import clean_pressure, cv_segments


MAX_EDGE_GAP = .012
EDGE_WINDOW = .020
TIMING_HALF_WINDOW = .080


def edge_gap(times, edge):
    first = bisect.bisect_right(times, edge - EDGE_WINDOW) - 1
    last = bisect.bisect_left(times, edge + EDGE_WINDOW)
    if first < 0 or last >= len(times):
        return None
    return max(times[i + 1] - times[i] for i in range(first, last))


def edge_covered(times, edge):
    gap = edge_gap(times, edge)
    return gap is not None and gap <= MAX_EDGE_GAP


def edge_area(times, pressure, edge):
    values = []
    for i in range(-16, 17):
        when = edge + i * (TIMING_HALF_WINDOW / 16)
        right = bisect.bisect_left(times, when)
        if right == 0 or right == len(times):
            return None
        left = right - 1
        fraction = (when - times[left]) / (times[right] - times[left])
        values.append(pressure[left] + fraction * (pressure[right] - pressure[left]))
    return (sum(values[1:-1]) + (values[0] + values[-1]) / 2) * TIMING_HALF_WINDOW / 16


def signed_transition_balance(times, pressure, rise, fall, step):
    if not all(edge_covered(times, edge) for edge in (rise, fall)):
        return None
    rising, falling = (edge_area(times, pressure, edge) for edge in (rise, fall))
    if rising is None or falling is None:
        return None
    return (rising - falling) / step


def read_cycle(capture):
    samples = clean_pressure(capture["samples"])
    if not samples:
        return None, "no_pressure_samples"
    times, pressure = zip(*samples)
    moves = cv_segments(capture["moves"])
    reason = "no_matching_motion"
    for slow, fast, recovery in zip(moves, moves[1:], moves[2:]):
        speeds = tuple(move[2] for move in (slow, fast, recovery))
        expected = capture.get("filament_feed_mm_s", (2.0, 5.0, 2.0))
        if not all(math.isclose(speed, target, rel_tol=.002, abs_tol=.01)
                   for speed, target in zip(speeds, expected, strict=True)):
            continue
        if times[0] > fast[0] + .035 or times[-1] < recovery[1] - .05:
            reason = "incomplete_cycle"
            continue
        break
    else:
        return None, reason

    fast_baseline = interval(times, pressure, fast[1] - .16, fast[1] - .025)
    slow_baseline = interval(times, pressure, recovery[1] - .20, recovery[1] - .025)
    if min(len(fast_baseline), len(slow_baseline)) < 40:
        return None, "insufficient_plateau_samples"
    high = statistics.median(fast_baseline)
    low = statistics.median(slow_baseline)
    if abs(high - low) < 20000:
        return None, "weak_pressure_step"
    return (times, pressure, fast[0], recovery[0], low, high), None


def interval(times, pressure, start, end):
    return pressure[bisect.bisect_left(times, start):bisect.bisect_left(times, end)]


def step_errors(times, pressure, edge, target, step):
    errors = []
    for index in range(16):
        begin = edge + .005 + index * .025
        values = interval(times, pressure, begin, begin + .025)
        if values:
            errors.append((statistics.mean(values) - target) / step)
    return errors


def score(path, *, controlled=False):
    cycle, _ = read_cycle(json.loads(path.read_text(encoding="utf-8")))
    if cycle is None:
        return None
    times, pressure, rise, fall, low, high = cycle
    step = high - low
    if controlled:
        timing = signed_transition_balance(times, pressure, rise, fall, step)
        if timing is None:
            return None
    rising = step_errors(times, pressure, rise, high, step)
    falling = step_errors(times, pressure, fall, low, step)
    if min(len(rising), len(falling)) < 10:
        return None
    rms = math.sqrt((statistics.mean(x*x for x in rising) + statistics.mean(x*x for x in falling)) / 2)
    balance = statistics.mean(rising) - statistics.mean(falling)
    return (rms, balance, abs(step), timing) if controlled else (rms, balance, abs(step))


def score_edges(capture):
    """Score each transition with sufficient pressure and timing coverage."""
    edges = {"rise": None, "fall": None}
    cycle, cycle_reason = read_cycle(capture)
    quality = {side: {"usable": False, "reason": cycle_reason} for side in edges}
    if cycle is not None:
        times, pressure, rise, fall, low, high = cycle
        step = high - low
        ideal_area = TIMING_HALF_WINDOW * (low + high)
        for side, edge, target in (("rise", rise, high), ("fall", fall, low)):
            gap = edge_gap(times, edge)
            if gap is None:
                quality[side]["reason"] = "transition_outside_capture"
                continue
            quality[side]["max_gap_ms"] = round(1000 * gap, 2)
            if gap > MAX_EDGE_GAP:
                quality[side]["reason"] = "sample_gap_at_transition"
                continue
            area = edge_area(times, pressure, edge)
            errors = step_errors(times, pressure, edge, target, step)
            if area is None or len(errors) < 10:
                quality[side]["reason"] = "insufficient_response_samples"
                continue
            edges[side] = {"signed_s": (area - ideal_area) / step,
                           "rms": math.sqrt(statistics.mean(x * x for x in errors))}
            quality[side].update(usable=True, reason=None)
    return {**edges, "quality": quality}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    args = parser.parse_args()
    results = defaultdict(list)
    for path in args.captures:
        match = re.search(r"_(?:sweep|peak)_(\d{3})_(\d+)$", path.stem)
        if match is None:
            parser.error(f"{path.name} does not identify the PA and repeat number")
        pa = int(match[1]) / 1000
        result = score(path)
        if result is None:
            print(f"{pa:.3f} repeat {match[2]}: insufficient step coverage or signal")
            continue
        rms, balance, step = result
        results[pa].append(result)
        print(f"{pa:.3f} repeat {match[2]}: RMS={rms:.3f}, balance={balance:+.3f}, step={step:.0f}")

    median_errors = {}
    for pa, rows in sorted(results.items()):
        rms, balance, _ = map(statistics.median, zip(*rows))
        print(f"{pa:.3f}: {len(rows)} valid repeats, median RMS={rms:.3f}, median balance={balance:+.3f}")
        if len(rows) >= 2:
            median_errors[pa] = rms
    if median_errors:
        best = min(median_errors, key=median_errors.get)
        print(f"Lowest median RMS among settings with two or more usable repeats: {best:.3f}")


if __name__ == "__main__":
    main()
