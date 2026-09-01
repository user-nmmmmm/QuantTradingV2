"""Compact, directly viewable PDF report for backtest research runs."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from backtest.reporting.risk_metrics import (
    METRIC_LABELS,
    PERCENT_METRIC_KEYS,
    calculate_active_risk_metrics,
    calculate_portfolio_risk_metrics,
)
from core.metrics import monthly_returns


def _display(key: str, value: Any) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "N/A"
    if key in PERCENT_METRIC_KEYS:
        return f"{float(value):.2%}"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    if isinstance(value, (float, np.floating)):
        return f"{float(value):,.3f}"
    return str(value)


def _table(ax, title: str, values: Mapping[str, Any], labels: Mapping[str, str]) -> None:
    ax.axis("off")
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=10)
    rows = [[labels.get(key, key), _display(key, value)] for key, value in values.items() if key not in {"status", "sample_size"}]
    if not rows:
        rows = [["Status", str(values.get("status", "not available"))]]
    table = ax.table(cellText=rows, colLabels=["Metric", "Value"], loc="upper left", cellLoc="left", colLoc="left", bbox=[0, 0, 1, 0.93])
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor("#d9e0e6")
        cell.set_facecolor("#edf2f6" if row == 0 else "white")
        if row == 0:
            cell.set_text_props(weight="bold")


def _overview_page(metrics, risk, active, metadata):
    fig = plt.figure(figsize=(11.69, 8.27), facecolor="#f4f6f8")
    fig.suptitle("Backtest Research Dashboard", fontsize=22, fontweight="bold", x=0.055, ha="left", y=0.97)
    subtitle = " | ".join(f"{k}: {v}" for k, v in (metadata or {}).items())
    fig.text(0.055, 0.925, subtitle[:180], fontsize=8.5, color="#5f6f7c")
    gs = fig.add_gridspec(1, 3, left=0.05, right=0.97, bottom=0.08, top=0.87, wspace=0.12)
    core_keys = ("TotalReturn", "CAGR", "MaxDrawdownPct", "SharpeRatio", "TotalTrades", "WinRate", "ProfitFactor", "NetPnL", "EndEquity")
    _table(fig.add_subplot(gs[0]), "Performance", {k: metrics.get(k) for k in core_keys}, METRIC_LABELS)
    _table(fig.add_subplot(gs[1]), "Portfolio risk", risk, METRIC_LABELS)
    _table(fig.add_subplot(gs[2]), "Benchmark-relative", active, METRIC_LABELS)
    fig.text(0.055, 0.025, "VaR/CVaR are historical one-period estimates. Alpha and beta use aligned periodic returns.", fontsize=8, color="#6d4c00")
    return fig


def _analysis_tables_page(metrics: Mapping[str, Any], equity: pd.Series):
    fig = plt.figure(figsize=(11.69, 8.27), facecolor="white")
    fig.suptitle("Calendar Returns and Drawdown Episodes", fontsize=18, fontweight="bold", x=0.05, ha="left")
    gs = fig.add_gridspec(2, 1, left=0.05, right=0.97, bottom=0.06, top=0.90, hspace=0.34)
    ax1, ax2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    ax1.axis("off")
    monthlies = monthly_returns(equity)
    if len(monthlies):
        frame = monthlies.to_frame("return"); frame["year"], frame["month"] = frame.index.year, frame.index.month
        pivot = frame.pivot(index="year", columns="month", values="return").reindex(columns=range(1, 13))
        rows = [[str(year)] + ["" if pd.isna(v) else f"{v:.1%}" for v in row] for year, row in pivot.iterrows()]
        table = ax1.table(cellText=rows, colLabels=["Year"] + [f"{m:02d}" for m in range(1, 13)], loc="center", cellLoc="center")
        table.auto_set_font_size(False); table.set_fontsize(7.5); table.scale(1, 1.25)
    ax1.set_title("Monthly returns", loc="left", fontweight="bold")
    ax2.axis("off")
    events = (metrics.get("ExtendedAnalytics") or {}).get("drawdown_events") or []
    ranked = sorted(events, key=lambda x: x.get("depth_pct") or 0)[:12]
    rows = [[str(e.get("peak"))[:10], str(e.get("trough"))[:10], str(e.get("recovery"))[:10], _display("MaxDrawdownPct", e.get("depth_pct")), f"{e.get('duration_days', 0):.0f}", "OPEN" if e.get("is_open") else "Recovered"] for e in ranked]
    if rows:
        table = ax2.table(cellText=rows, colLabels=["Peak", "Trough", "Recovery", "Depth", "Days", "Status"], loc="center", cellLoc="center")
        table.auto_set_font_size(False); table.set_fontsize(8); table.scale(1, 1.32)
    ax2.set_title("Largest drawdown episodes", loc="left", fontweight="bold")
    return fig


def _image_page(path: Path, title: str):
    if not path.exists():
        return None
    fig = plt.figure(figsize=(11.69, 8.27), facecolor="white")
    ax = fig.add_axes([0.025, 0.025, 0.95, 0.91])
    ax.imshow(mpimg.imread(path)); ax.axis("off")
    fig.suptitle(title, fontsize=16, fontweight="bold", y=0.98)
    return fig


def write_pdf_report(
    output_dir: str | Path,
    metrics: Mapping[str, Any],
    equity_curve: pd.DataFrame,
    closed_trades: Sequence[Mapping[str, Any]],
    benchmark_curve: pd.Series | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Write a multi-page PDF and a one-page PNG dashboard."""
    del closed_trades
    root = Path(output_dir)
    risk = calculate_portfolio_risk_metrics(equity_curve["equity"])
    active = calculate_active_risk_metrics(equity_curve["equity"], benchmark_curve)
    overview = _overview_page(metrics, risk, active, metadata)
    overview.savefig(root / "dashboard.png", dpi=180, facecolor=overview.get_facecolor())
    target = root / "report.pdf"
    with PdfPages(target, metadata={"Title": "Backtest Research Report", "Author": "QuantTradingV1"}) as pdf:
        pdf.savefig(overview, bbox_inches="tight"); plt.close(overview)
        tables = _analysis_tables_page(metrics, equity_curve["equity"])
        pdf.savefig(tables, bbox_inches="tight"); plt.close(tables)
        for name, title in (
            ("equity.png", "Equity, Drawdown, Returns and Allocation"),
            ("monthly_returns_heatmap.png", "Monthly Return Heatmap"),
            ("rolling_metrics.png", "Rolling Risk Metrics"),
            ("pnl_distribution.png", "Closed Trade PnL Distribution"),
        ):
            page = _image_page(root / name, title)
            if page is not None:
                pdf.savefig(page, bbox_inches="tight"); plt.close(page)
    return target


__all__ = ["calculate_active_risk_metrics", "calculate_portfolio_risk_metrics", "write_pdf_report"]
