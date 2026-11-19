"""Export fixed-order research figures; never rank or select a parameter winner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
from matplotlib.ticker import MaxNLocator, PercentFormatter
import numpy as np
import pandas as pd

from backtest.plot_style import (
    PALETTE, annotation_color, chart_style, diverging_cmap, save_figure, style_axes,
)


@chart_style()
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--matrix-only", action="store_true")
    args = parser.parse_args()
    batch = args.batch.resolve()
    protocol = json.loads((batch / "review_protocol.json").read_text(encoding="utf-8"))
    runs = pd.read_csv(batch / "all_run_summaries.csv")
    output = batch / "figures"
    output.mkdir(exist_ok=True)
    matrix = runs.loc[runs.phase.eq("matrix")].copy()
    matrix["arm"] = matrix.name.str.rsplit("__", n=1).str[0]
    matrix["window"] = matrix.name.str.rsplit("__", n=1).str[1]
    ordered_arms = [arm["arm"] for arm in protocol["arms"]]
    ordered_windows = [row["name"] for row in protocol["rolling_windows"]]
    values = matrix.pivot(index="arm", columns="window", values="return_pct").reindex(index=ordered_arms, columns=ordered_windows)
    fig, ax = plt.subplots(figsize=(14, 23), layout="constrained")
    try:
        style_axes(ax, grid_axis=None)
        colored = ax.imshow(np.ma.masked_invalid(values.to_numpy(dtype=float)), aspect="auto",
                            cmap=diverging_cmap(), norm=TwoSlopeNorm(0, -20, 20))
        ax.set_xticks(range(11), [name.replace("rolling_", "W") for name in ordered_windows])
        ax.set_yticks(range(52), ordered_arms, fontsize=8.5)
        ax.xaxis.tick_top()
        ax.tick_params(axis="x", labelsize=9, pad=9)
        ax.set_xticks(np.arange(-.5, 11, 1), minor=True)
        ax.set_yticks(np.arange(-.5, 52, 1), minor=True)
        ax.set_axisbelow(False)
        ax.grid(which="minor", color=PALETTE["surface"], linewidth=.7)
        ax.tick_params(which="minor", length=0)
        for row in range(52):
            for column in range(11):
                value = values.iloc[row, column]
                if np.isfinite(value):
                    ax.text(column, row, f"{value:.1f}", ha="center", va="center", fontsize=7.5,
                            color=annotation_color(colored.cmap(colored.norm(value))))
                else:
                    ax.text(column, row, "—", ha="center", va="center", fontsize=8,
                            color=PALETTE["muted"])
        ax.set_title("52 个预登记配置 × 11 个冻结窗口", fontsize=17, pad=44)
        ax.text(0, 1.014, "净收益 % · 固定登记顺序 · 每格独立资金 · 未筛选赢家 · — 表示缺失",
                transform=ax.transAxes, ha="left", va="bottom", fontsize=9.5, color=PALETTE["muted"])
        colorbar = fig.colorbar(colored, ax=ax, fraction=.018, pad=.025, extend="both",
                               label="净收益 · 颜色在 ±20% 饱和，格内为实际数值")
        colorbar.ax.yaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=0))
        colorbar.ax.tick_params(length=0, labelsize=9)
        colorbar.outline.set_visible(False)
        save_figure(fig, output / "matrix_returns.png")
        save_figure(fig, output / "matrix_returns.svg")
    finally:
        plt.close(fig)
    if args.matrix_only:
        return
    validation = runs.loc[runs.phase.eq("validation")].set_index("name")
    names = ["train60", "validation20", "final20", "main_1", "cost_1.5", "cost_2", "cost_3"]
    labels = ["独立训练段", "独立验证段", "独立最终段", "全区间", "成本 1.5×", "成本 2×", "成本 3×"]
    selected = validation.reindex(names)
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), layout="constrained")
    try:
        colors = [PALETTE["positive"] if value > 0 else PALETTE["negative"] for value in selected.return_pct]
        axes[0].bar(range(len(names)), selected.return_pct, width=.62, color=colors, zorder=3)
        style_axes(axes[0], percent=True, percent_scale=100)
        axes[0].axhline(0, color=PALETTE["muted"], linewidth=.8)
        axes[0].set_xticks(range(len(names)), labels, rotation=25, ha="right")
        axes[0].set_ylabel("净收益")
        axes[0].set_title("默认策略：分段与成本压力")
        axes[0].margins(y=.2)
        for index, value in enumerate(selected.return_pct):
            if np.isfinite(value):
                axes[0].annotate(f"{value:.2f}%", (index, value),
                    xytext=(0, 6 if value >= 0 else -6), textcoords="offset points", ha="center",
                    va="bottom" if value >= 0 else "top", fontsize=9, color=PALETTE["ink"])
        missing = validation.loc[validation.index.str.startswith("missing_1pct_seed_")]
        _, bin_edges, patches = axes[1].hist(missing.return_pct, bins=8, edgecolor=PALETTE["surface"], linewidth=1.)
        for left, right, patch in zip(bin_edges[:-1], bin_edges[1:], patches):
            patch.set_facecolor(PALETTE["positive"] if left >= 0 else
                                PALETTE["negative"] if right <= 0 else PALETTE["benchmark"])
            patch.set_alpha(.85)
        style_axes(axes[1])
        axes[1].xaxis.set_major_formatter(PercentFormatter(xmax=100))
        axes[1].yaxis.set_major_locator(MaxNLocator(integer=True))
        axes[1].axvline(float(validation.loc["main_1", "return_pct"]), color=PALETTE["benchmark"],
                       linewidth=1.8, linestyle="--", label="无缺失基线")
        axes[1].set_title(f"1% 随机缺失：全部 {len(missing)} 个预登记种子")
        axes[1].set_xlabel("净收益")
        axes[1].set_ylabel("运行次数")
        axes[1].margins(y=.15)
        axes[1].legend(loc="upper right")
        fig.supxlabel("每项保留原冻结区间与运行口径 · 回顾研究结果", fontsize=9, color=PALETTE["muted"])
        save_figure(fig, output / "default_validation.png")
        save_figure(fig, output / "default_validation.svg")
    finally:
        plt.close(fig)
    print(str(output), flush=True)


if __name__ == "__main__":
    main()
