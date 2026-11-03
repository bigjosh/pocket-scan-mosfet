"""Compare two complete, matching 100-point DOT_US captures without USB access.

Example:
  python prototype/compare_scan_timings.py before.log after.log --output comparison.json

The boundary is point acceptance through that UI tick; plot backlog can delay
visibility. Phase buckets partition wall time. Work counters overlap: ENGINE
includes I2C, and neither work totals nor phases may be added to one another.
Reported EMIT belongs to the previous point's synchronous USB log operation.
The failed field is sticky, while dropped is a cumulative failed-report count.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics


POINTS = 100
PHASES = ("other", "drive", "park_settle", "gate_settle", "drain_settle", "convert", "process")
WORK = ("input", "engine", "i2c", "record", "render", "sleep", "emit")
CONTROLS = ("index", "curve", "adc_blocks")
REQUIRED = frozenset(("index", "curve", "wall", "backlog", "dropped", "failed",
                      "adc_blocks", "polls") + WORK + PHASES)
TIME_FIELDS = ("wall",) + WORK + ("engine_excluding_i2c",) + PHASES
GROUPS = ("all", "ordinary", "new_gate")
_FIELD = re.compile(r"([a-z][a-z0-9_]*)=([0-9]+)\Z")


def parse_lines(lines, label="log"):
    """Ignore unrelated text; refuse incomplete, duplicated or malformed DOTs."""
    rows = []
    previous_drop = previous_failed = 0
    for line_number, line in enumerate(lines, 1):
        line = line.strip()
        if not line.startswith("DOT_US"):
            continue
        parts = line.split()
        if not parts or parts[0] != "DOT_US":
            raise ValueError(f"{label}:{line_number}: malformed DOT marker")
        values = {}
        for token in parts[1:]:
            match = _FIELD.fullmatch(token)
            if match is None or match[1] in values:
                raise ValueError(f"{label}:{line_number}: invalid or duplicate DOT field")
            values[match[1]] = int(match[2])
        if values.keys() != REQUIRED:
            raise ValueError(f"{label}:{line_number}: missing or unknown DOT fields")
        if values["index"] != len(rows):
            raise ValueError(f"{label}:{line_number}: indexes must be unique and ordered 0..99")
        if values["failed"] not in (0, 1) or values["adc_blocks"] < 1:
            raise ValueError(f"{label}:{line_number}: invalid failure flag or ADC block count")
        if values["dropped"] < previous_drop or values["failed"] < previous_failed:
            raise ValueError(f"{label}:{line_number}: cumulative report status moved backwards")
        if values["engine"] < values["i2c"]:
            raise ValueError(f"{label}:{line_number}: I2C exceeds its enclosing ENGINE counter")
        if sum(values[field] for field in PHASES) != values["wall"]:
            raise ValueError(f"{label}:{line_number}: phase buckets do not partition wall time")
        if (not rows and values["curve"] != 0 or rows and
                values["curve"] not in (rows[-1]["curve"], rows[-1]["curve"] + 1)):
            raise ValueError(f"{label}:{line_number}: curve order is not contiguous")
        previous_drop, previous_failed = values["dropped"], values["failed"]
        values["engine_excluding_i2c"] = values["engine"] - values["i2c"]
        rows.append(values)
        if len(rows) > POINTS:
            raise ValueError(f"{label}: more than one complete 100-point capture")
    if len(rows) != POINTS:
        raise ValueError(f"{label}: expected 100 complete DOT records; found {len(rows)}")
    return rows


def _quality(rows):
    return {"max_plot_backlog": max(row["backlog"] for row in rows),
            "max_dropped_reports": max(row["dropped"] for row in rows),
            "max_failed_flag": max(row["failed"] for row in rows),
            "records_with_failed_flag": sum(row["failed"] for row in rows)}


def _stats(rows):
    if not rows:
        return {"count": 0, "mean_ms": None, "median_wall_ms": None,
                "mean_counts": None, "quality": None}
    return {"count": len(rows),
            "mean_ms": {field: round(statistics.fmean(row[field] for row in rows) / 1000, 6)
                        for field in TIME_FIELDS},
            "median_wall_ms": statistics.median(row["wall"] for row in rows) / 1000,
            "mean_counts": {field: statistics.fmean(row[field] for row in rows)
                            for field in ("adc_blocks", "polls")},
            "quality": _quality(rows)}


def _summarize(rows):
    new_gate = [row for index, row in enumerate(rows)
                if index == 0 or row["curve"] != rows[index - 1]["curve"]]
    ordinary = [row for index, row in enumerate(rows)
                if index > 0 and row["curve"] == rows[index - 1]["curve"]]
    return {"groups": {"all": _stats(rows), "ordinary": _stats(ordinary),
                       "new_gate": _stats(new_gate)},
            "new_gate_indexes": [row["index"] for row in new_gate],
            "quality": _quality(rows),
            "timing_reports_clean": not any(row["dropped"] or row["failed"] for row in rows)}


def compare(before_path, after_path):
    """Validate capture identity before calculating any before/after ratios."""
    captures, rows_by_side = {}, {}
    for side, path in (("before", before_path), ("after", after_path)):
        path = Path(path)
        data = path.read_bytes()
        rows = parse_lines(data.decode("utf-8-sig").splitlines(), label=str(path))
        rows_by_side[side] = rows
        captures[side] = {"path": str(path), "log_sha256": hashlib.sha256(data).hexdigest(),
                          **_summarize(rows)}
    for before, after in zip(rows_by_side["before"], rows_by_side["after"]):
        if any(before[key] != after[key] for key in CONTROLS):
            raise ValueError(f"Capture controls differ at index {before['index']}: index/curve/adc_blocks")
    changes = {}
    for group in GROUPS:
        before, after = (captures[side]["groups"][group] for side in ("before", "after"))
        if not before["count"]:
            changes[group] = None
            continue
        old_wall, new_wall = before["mean_ms"]["wall"], after["mean_ms"]["wall"]
        changes[group] = {
            "mean_ms_delta_after_minus_before": {
                field: round(after["mean_ms"][field] - before["mean_ms"][field], 6)
                for field in TIME_FIELDS},
            "mean_wall_speedup": old_wall / new_wall if new_wall else None,
            "mean_wall_reduction_percent": (old_wall - new_wall) / old_wall * 100 if old_wall else None,
            "median_wall_ms_delta_after_minus_before": round(
                after["median_wall_ms"] - before["median_wall_ms"], 6)}
    return {"version": 1, "point_count": POINTS,
            "identical_control_fields": list(CONTROLS),
            "group_definition": "new_gate is index 0 or a curve transition; ordinary is every other point",
            "timing_boundary": "point accepted through that UI tick; backlog tracks delayed plotting",
            "counter_interpretation": "phases partition wall; work overlaps; engine_excluding_i2c = engine - i2c",
            "report_status_interpretation": "dropped is cumulative; failed is sticky, not a per-record failure count",
            **captures, "changes": changes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path, help="Write the detailed comparison JSON")
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.resolve() in (args.before.resolve(), args.after.resolve()):
            raise ValueError("Output must not overwrite an input log")
        report = compare(args.before, args.after)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            print(json.dumps(report, indent=2, sort_keys=True))
        for group in GROUPS:
            before, after = (report[side]["groups"][group] for side in ("before", "after"))
            if not before["count"]:
                continue
            print("%s n=%d mean_wall_ms %.3f -> %.3f; median_wall_ms %.3f -> %.3f; engine_without_i2c_ms %.3f -> %.3f" % (
                group, before["count"], before["mean_ms"]["wall"], after["mean_ms"]["wall"],
                before["median_wall_ms"], after["median_wall_ms"],
                before["mean_ms"]["engine_excluding_i2c"], after["mean_ms"]["engine_excluding_i2c"]))
        for side in ("before", "after"):
            print(side + " quality " + json.dumps(report[side]["quality"], sort_keys=True))
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    return report


if __name__ == "__main__":
    main()
