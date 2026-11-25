"""Predict the extruder servo current from flow, nozzle size and nozzle temperature.

Each Max Flow run is close to a straight line, Iq = a + b * flow, where Iq is the median absolute
servo current. The intercept a stays near zero. The slope b changes with the nozzle and with how
runny the plastic is at the nozzle temperature:

    b(d, T) = b0 * (0.4 / d) ** n * exp(-k * (T - 250) / 10)

So the model keeps three numbers per material family (ln b0, n, k) and fits them by least squares
on ln b. The file keeps only the running sums for that fit, so it stays the same size however many
runs are added. Until runs exist at other nozzles or temperatures, priors hold n and k in place.
Priors: n from the archived 0.4 and 0.6 mm ABS runs at 280 °C (slopes .0305 and .021). k is a
starting guess, about 14% more current per 10 °C cooler, and is replaced once runs at other
temperatures exist.
"""

import json
import math
import os
from pathlib import Path

CURRENT_CUTOFF = 1.0
REF_NOZZLE_MM = .4
REF_TEMPERATURE_C = 250
PRIORS = {1: (.9, 2.), 2: (.13, 2.)}  # feature index: (value, weight in runs) for n and k
INTERCEPT_PRIOR = (-.05, 1.)
MIN_RUN_POINTS = 3
MIN_RUN_SPREAD = 8  # mm³/s between the lowest and highest probe of a run
SCHEMA = 1


def features(nozzle_mm, temperature_c):
    return (1., -math.log(nozzle_mm / REF_NOZZLE_MM), -(temperature_c - REF_TEMPERATURE_C) / 10)


def fit_line(points):
    """Least-squares (intercept, slope) through [(flow, iq)], or None without a rising line."""
    if len(points) < 2:
        return None
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denominator = n * sxx - sx * sx
    if denominator <= 0:
        return None
    slope = (n * sxy - sx * sy) / denominator
    if not math.isfinite(slope) or slope <= 0:
        return None
    return (sy - slope * sx) / n, slope


def cutoff(intercept, slope):
    return (CURRENT_CUTOFF - intercept) / slope


def solve(matrix, vector):
    """Solve a small linear system by Gaussian elimination. Returns None if it is singular."""
    size = len(vector)
    rows = [list(matrix[i]) + [vector[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(size):
            if r != col:
                factor = rows[r][col] / rows[col][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[col])]
    return [rows[i][size] / rows[i][i] for i in range(size)]


class FlowModel:
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema") != SCHEMA:
                raise ValueError("Unknown flow model schema.")
        except (OSError, ValueError):
            data = {"schema": SCHEMA, "materials": {}}
        self.data = data

    def parameters(self, material):
        stats = self.data["materials"].get(material)
        if not stats or not stats["runs"]:
            return None
        xtx = [row[:] for row in stats["xtx"]]
        xty = stats["xty"][:]
        for index, (value, weight) in PRIORS.items():
            xtx[index][index] += weight
            xty[index] += weight * value
        theta = solve(xtx, xty)
        if theta is None:
            return None
        value, weight = INTERCEPT_PRIOR
        intercept = (stats["intercept_sum"] + value * weight) / (stats["runs"] + weight)
        return {"ln_slope_ref": theta[0], "nozzle_exponent": theta[1], "temperature_per_10c": theta[2],
                "intercept": intercept, "runs": stats["runs"]}

    def predict(self, material, nozzle_mm, temperature_c):
        """Return (intercept, slope) for this nozzle and temperature, or None without data."""
        params = self.parameters(material)
        if params is None:
            return None
        x = features(nozzle_mm, temperature_c)
        theta = (params["ln_slope_ref"], params["nozzle_exponent"], params["temperature_per_10c"])
        return params["intercept"], math.exp(sum(a * b for a, b in zip(theta, x)))

    def add_run(self, material, nozzle_mm, temperature_c, points):
        """Fold one run's probes into the model. Returns the fitted line or None if unusable."""
        if len(points) < MIN_RUN_POINTS or max(f for f, _ in points) - min(f for f, _ in points) < MIN_RUN_SPREAD:
            return None
        line = fit_line(points)
        if line is None:
            return None
        intercept, slope = line
        x = features(nozzle_mm, temperature_c)
        y = math.log(slope)
        stats = self.data["materials"].setdefault(
            material, {"runs": 0, "xtx": [[0.] * 3 for _ in range(3)], "xty": [0.] * 3, "intercept_sum": 0.})
        for i in range(3):
            stats["xty"][i] += x[i] * y
            for j in range(3):
                stats["xtx"][i][j] += x[i] * x[j]
        stats["runs"] += 1
        stats["intercept_sum"] += intercept
        self.save()
        return line

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def rebuild(paths, model_path):
    """Build a fresh model from archived Max Flow summaries. Returns the number of runs used."""
    model = FlowModel(model_path)
    model.data = {"schema": SCHEMA, "materials": {}}
    used = 0
    for path in sorted(paths):
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        if report.get("reason") not in ("current_cutoff", "unbracketed_current", "current_below_threshold"):
            continue
        points = [(row["flow_mm3_s"], row["motor_median_abs_iq"]) for row in report.get("steps", [])
                  if row.get("motor_median_abs_iq") is not None]
        if model.add_run(report["material"], report["nozzle_mm"], report["temperature_c"], points):
            used += 1
    model.save()
    return used
