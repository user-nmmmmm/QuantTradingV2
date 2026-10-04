"""Bounded comparisons; metric calculations always use the complete report."""
from pathlib import Path

from dashboard.visual_data import load_backtest


def compare_experiments(reports_dir: Path, run_ids: list[str]) -> dict:
    if not isinstance(run_ids, list) or not 2 <= len(run_ids) <= 4:
        raise ValueError("Select 2 to 4 experiments")
    if any(not isinstance(item, str) for item in run_ids) or len(set(run_ids)) != len(run_ids):
        raise ValueError("Experiment identifiers must be unique")
    runs = []
    for run_id in run_ids:
        report = load_backtest(reports_dir, run_id)
        points = report["points"]
        base = points[0]["equity"]
        indices = set(range(len(points))) if len(points) <= 800 else {
            round(index * (len(points) - 1) / 799) for index in range(800)
        }
        indices.add(min(range(len(points)), key=lambda index: points[index]["drawdown"]))
        runs.append({
            "id": run_id, "parameters": report["parameters"], "metrics": report["metrics"],
            "configuration": report["configuration"],
            "period_start": report["period_start"], "period_end": report["period_end"],
            "point_count": len(points),
            "points": [{"timestamp": points[index]["timestamp"],
                        "nav": points[index]["equity"] / base * 100,
                        "drawdown": points[index]["drawdown"]} for index in sorted(indices)],
        })
    fields = ["source", "symbols", "start", "end", "capital", "slippage_bps", "seed", "strategy"]
    differences = [{"field": field, "values": [run["parameters"].get(field) for run in runs]}
                   for field in fields]
    differences = [item for item in differences if any(value != item["values"][0] for value in item["values"][1:])]
    warnings = []
    for field in ("base_config_sha256", "config_sha256"):
        values = [run["configuration"].get(field) for run in runs]
        if any(value is None for value in values):
            warnings.append(f"{field}: configuration snapshot identity is missing in some reports")
        elif len(set(values)) > 1:
            differences.append({"field": field, "values": values})
            warnings.append(f"{field}: experiments have different configuration snapshots")
    for field in ("source", "symbols", "slippage_bps"):
        values = [run["parameters"].get(field) for run in runs]
        if any(value is None for value in values):
            warnings.append(f"{field}: some reports do not record this parameter")
        elif any(value != values[0] for value in values[1:]):
            warnings.append(f"{field}: experiments use different settings")
    if len({(run["period_start"], run["period_end"]) for run in runs}) > 1:
        warnings.append("Experiments cover different observed periods; returns are not directly comparable")
    return {"runs": runs, "differences": differences, "warnings": warnings,
            "methodology": "Each NAV starts at 100 on its own first observed date. Calendar-time axis; no forward filling. Metrics use all observations; charts retain at most 801 points, including the deepest drawdown. Fees and full configuration identity may be absent in older reports."}
