"""Reproduce linear/piecewise fits of a saved open-output calibration.

Run with a Python containing matplotlib. No device access or firmware changes:
    python prototype/calibration_fit_analysis.py

The reported residuals quantify approximation of the saved calibration table;
they do not establish instrument accuracy or validate another supply voltage.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALIBRATION = ROOT / "output/smoke-test/calibration-68k-installed-20261102.json"
DEFAULT_PREFIX = ROOT / "output/review/calibration-fit-5v-20261102"


def linear_fit(points, xkey, ykey):
    xs, ys = [point[xkey] for point in points], [point[ykey] for point in points]
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = (sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys)) /
             sum((x - xmean) ** 2 for x in xs))
    intercept = ymean - slope * xmean
    errors = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    worst = max(range(len(errors)), key=lambda index: abs(errors[index]))
    return {"slope": slope, "intercept": intercept,
            "max_abs_residual": abs(errors[worst]),
            "rms_residual": math.sqrt(sum(error ** 2 for error in errors) / len(errors)),
            "worst_terminal_v": points[worst]["terminal_v"], "residuals": errors}


def interpolate(xs, ys, selected):
    """Evaluate retained-knot interpolation at every original abscissa."""
    result = [ys[0]] * len(xs)
    for lower, upper in zip(selected, selected[1:]):
        for index in range(lower, upper + 1):
            fraction = (xs[index] - xs[lower]) / (xs[upper] - xs[lower])
            result[index] = ys[lower] + fraction * (ys[upper] - ys[lower])
    return result


def compact_baseline(points, resistance, tolerance_a):
    """Keep endpoints and repeatedly retain the worst-error original point.

    Selected knots are original table points. The difference between full and
    reduced piecewise interpolants is linear on each original interval, so its
    largest absolute error is attained at an original point, not hidden between
    them. This bound concerns the table representation only.
    """
    xs = [point["terminal_v"] for point in points]
    ys = [point["shunt_v"] / resistance for point in points]
    selected = [0, len(points) - 1]
    while True:
        predicted = interpolate(xs, ys, selected)
        errors = [actual - estimate for actual, estimate in zip(ys, predicted)]
        worst = max(range(len(errors)), key=lambda index: abs(errors[index]))
        if abs(errors[worst]) <= tolerance_a:
            return {"requested_tolerance_a": tolerance_a, "knot_count": len(selected),
                    "selected_indices": selected, "max_abs_residual_a": abs(errors[worst]),
                    "residuals_a": errors, "predicted_current_a": predicted,
                    "knots": [{"terminal_v": xs[index], "baseline_current_a": ys[index]}
                              for index in selected]}
        selected.append(worst)
        selected.sort()


def analyze(path):
    raw = path.read_bytes()
    calibration = json.loads(raw)
    result = {"source": str(path.resolve()), "source_bytes": len(raw),
              "source_sha256": hashlib.sha256(raw).hexdigest(),
              "driver_supply_v": calibration["metadata"]["driver_supply_v"],
              "calibration_version": calibration["version"],
              "interpretation": "Residuals approximate the saved calibration table; they are not measurement accuracy.",
              "channels": {}}
    for role, channel in calibration["channels"].items():
        points, resistance = channel["points"], channel["shunt_ohm"]
        drive_points = points[:-1] if channel["capability"]["limit_reason"] == "plateau" else points
        baseline = linear_fit(points, "terminal_v", "shunt_v")
        baseline["max_abs_residual_a"] = baseline["max_abs_residual"] / resistance
        baseline["rms_residual_a"] = baseline["rms_residual"] / resistance
        baseline["residuals_a"] = [error / resistance for error in baseline["residuals"]]
        tolerances = (0.25e-6, 0.5e-6, 1e-6)
        result["channels"][role] = {
            "point_count": len(points), "shunt_ohm": resistance,
            "drive_fit_nonplateau_including_zero": linear_fit(drive_points, "duty", "terminal_v"),
            "drive_fit_nonplateau_nonzero": linear_fit([point for point in drive_points if point["duty"] > 0], "duty", "terminal_v"),
            "global_baseline_line": baseline,
            "piecewise_baselines": [compact_baseline(points, resistance, tolerance) for tolerance in tolerances],
        }
    return calibration, result


def plot_gate(calibration, summary, prefix):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    points = calibration["channels"]["gate"]["points"]
    channel = summary["channels"]["gate"]
    fit = channel["global_baseline_line"]
    piecewise = next(item for item in channel["piecewise_baselines"]
                     if item["requested_tolerance_a"] == 0.25e-6)
    xs = [point["terminal_v"] for point in points]
    ys = [point["shunt_v"] / channel["shunt_ohm"] * 1e6 for point in points]
    fitted = [(fit["slope"] * x + fit["intercept"]) / channel["shunt_ohm"] * 1e6 for x in xs]
    red, blue, ink = "#bd4835", "#087ea4", "#26323c"
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.edgecolor": "#a7afb6", "axes.labelcolor": ink,
                         "xtick.color": ink, "ytick.color": ink})
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10.5, 9),
                             gridspec_kw={"height_ratios": [2.5, 1, 1]})
    fig.subplots_adjust(top=0.87, bottom=0.13, left=0.12, right=0.97, hspace=0.13)
    fig.suptitle("Gate bias needs more than one straight line", x=0.12, y=0.975,
                 ha="left", fontsize=19, fontweight="bold", color=ink)
    fig.text(0.12, 0.93, "Saved open-output calibration  |  %.1f V supply  |  %g Ω shunt  |  %d measured points" %
             (summary["driver_supply_v"], channel["shunt_ohm"], len(points)), color="#56636d", fontsize=10.5)
    top, middle, lower = axes
    top.plot(xs, ys, "o", color=ink, markersize=3.5, label="Saved measurements", zorder=4)
    top.plot(xs, fitted, "--", color=red, linewidth=2, label="One fitted line")
    knots = piecewise["knots"]
    top.plot([item["terminal_v"] for item in knots],
             [item["baseline_current_a"] * 1e6 for item in knots],
             color=blue, linewidth=1.8, label="%d retained knots" % piecewise["knot_count"])
    top.legend(loc="upper left", frameon=False, ncols=3, fontsize=9)
    top.set_ylabel("Apparent gate current with no load (µA)")
    top.set_ylim(-1.15, 8.1)
    top.annotate("First transition", xy=(1.29, 0.93), xytext=(0.62, 3.6),
                 arrowprops={"arrowstyle": "->", "color": "#69747b"}, color="#56636d")
    top.annotate("Upper-range transition", xy=(3.34, 4.5), xytext=(2.05, 6.5),
                 arrowprops={"arrowstyle": "->", "color": "#69747b"}, color="#56636d")
    middle.axhline(0, color="#818b92", linewidth=0.8)
    middle.plot(xs, [error * 1e6 for error in fit["residuals_a"]], "o-", color=red, markersize=3, linewidth=1.3)
    middle.set_ylabel("One-line error\n(µA)")
    middle.text(0.02, 0.83, "Maximum |error|: %.3f µA" % (fit["max_abs_residual_a"] * 1e6),
                transform=middle.transAxes, color=red, fontsize=10, fontweight="bold")
    lower.axhline(0, color="#818b92", linewidth=0.8)
    lower.axhspan(-0.25, 0.25, color=blue, alpha=0.07)
    lower.plot(xs, [error * 1e6 for error in piecewise["residuals_a"]], "o-", color=blue, markersize=3, linewidth=1.3)
    lower.set_ylim(-0.325, 0.375)
    lower.set_ylabel("%d-knot error\n(µA)" % piecewise["knot_count"])
    lower.text(0.02, 0.83, "Maximum |error|: %.3f µA  (budget: 0.25 µA)" % (piecewise["max_abs_residual_a"] * 1e6),
               transform=lower.transAxes, color=blue, fontsize=10, fontweight="bold")
    lower.set_xlabel("Measured gate output voltage to ground (V)")
    for axis in axes:
        axis.grid(axis="both", color="#d8dde1", linewidth=0.6, alpha=0.65)
        axis.set_axisbelow(True)
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_xlim(0, 3.6)
    fig.text(0.12, 0.047, "Errors are approximation residuals against the saved table, not independently verified accuracy.\n"
             "Residual panels have different vertical scales. This is the saved 5 V calibration, not an 18.8 V result.",
             fontsize=9, color="#56636d", linespacing=1.5)
    fig.savefig(str(prefix) + ".png", dpi=170, facecolor="white")
    fig.savefig(str(prefix) + ".svg", facecolor="white")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_PREFIX)
    args = parser.parse_args()
    calibration, summary = analyze(args.calibration)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    output = args.output_prefix.with_suffix(".json")
    output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    plot_gate(calibration, summary, args.output_prefix)
    print(json.dumps({"source_sha256": summary["source_sha256"], "summary": str(output),
                      "plot": str(args.output_prefix) + ".png"}))


if __name__ == "__main__":
    main()
