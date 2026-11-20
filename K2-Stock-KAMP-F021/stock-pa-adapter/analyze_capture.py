"""Fit the stock K2's timestamped 2 → 5 → 2 mm/s PA captures."""

import argparse
import bisect
import json
import statistics
from pathlib import Path

from pa_static_offline import cv_segments, fit_exp


def clean_pressure(samples):
    # Match OpenKlipper pa_analysis.clean without requiring NumPy.
    if len(samples) <= 31:
        return samples
    raw = [value for _, value in samples]
    padded = [raw[0]] * 15 + raw + [raw[-1]] * 15
    return [
        row for index, row in enumerate(samples)
        if abs(row[1] - statistics.median(padded[index:index + 31])) < 30000
    ]


def analyze(path):
    capture = json.loads(path.read_text(encoding="utf-8"))
    samples = clean_pressure(capture["samples"])
    times = [time for time, _ in samples]
    print(f"{path.stem}: {len(samples)}/{len(capture['samples'])} pressure samples after filtering")
    results = []
    for start, end, speed, previous in cv_segments(capture["moves"]):
        transition = (round(previous or 0, 1), round(speed, 1))
        if transition not in ((2.0, 5.0), (5.0, 2.0)):
            continue
        lo = bisect.bisect_left(times, start + .005)
        hi = bisect.bisect_right(times, min(end, start + .4))
        window = samples[lo:hi]
        if len(window) < 20:
            continue
        values = [value for _, value in window]
        if max(values) - min(values) < 20000:
            print(f"  {transition}: pressure change below 20,000 counts")
            continue
        tau, error = fit_exp([t - window[0][0] for t, _ in window], values)
        if tau is None:
            continue
        direction = "rising" if speed > previous else "falling"
        print(f"  {direction}: {len(window)} samples, tau={tau:.5f} s, normalized error={error:.5f}")
        results.append((direction, tau))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    args = parser.parse_args()
    by_direction = {"rising": [], "falling": []}
    for path in args.captures:
        for direction, tau in analyze(path):
            by_direction[direction].append(tau)
    for direction, values in by_direction.items():
        if values:
            print(f"{direction}: median={statistics.median(values):.5f} s, range={min(values):.5f}–{max(values):.5f} s")
    rising, falling = by_direction.values()
    if len(rising) < 2 or len(falling) < 2:
        print("No candidate: need two usable fits in each direction.")
        return
    rise = statistics.median(rising)
    fall = statistics.median(falling)
    if not (.01 <= rise <= .15 and .01 <= fall <= .15) or abs(rise - fall) > .05:
        print("No candidate: directional medians did not agree within the calibration limits.")
        return
    print(f"Candidate PA for print comparison: {statistics.median(rising + falling):.5f}")


if __name__ == "__main__":
    main()
