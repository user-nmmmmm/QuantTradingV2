"""Export fixed-order research figures; never rank or select a parameter winner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
import numpy as np
import pandas as pd


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
    plt.rcParams.update({"font.family": "Microsoft YaHei", "axes.unicode_minus": False,
                         "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": "white"})
    matrix = runs.loc[runs.phase.eq("matrix")].copy()
    matrix["arm"] = matrix.name.str.rsplit("__", n=1).str[0]
    matrix["window"] = matrix.name.str.rsplit("__", n=1).str[1]
    ordered_arms = [arm["arm"] for arm in protocol["arms"]]
    ordered_windows = [row["name"] for row in protocol["rolling_windows"]]
    values = matrix.pivot(index="arm", columns="window", values="return_pct").reindex(index=ordered_arms, columns=ordered_windows)
    fig, ax = plt.subplots(figsize=(15, 23), layout="constrained")
    colored = ax.imshow(values, aspect="auto", cmap="RdYlGn", norm=TwoSlopeNorm(0, -20, 20))
    ax.set_xticks(range(11), [name.replace("rolling_", "W") for name in ordered_windows])
    ax.set_yticks(range(52), ordered_arms, fontsize=8)
    for row in range(52):
        for column in range(11):
            value = values.iloc[row, column]
            if np.isfinite(value):
                ax.text(column, row, f"{value:.1f}", ha="center", va="center", fontsize=7,
                        color="white" if abs(value) >= 16 else "#202020")
    ax.set_title("52 个预登记配置 × 11 个冻结窗口\n净收益 % · 固定登记顺序 · 每格独立资金 · 未筛选赢家", fontsize=16, pad=18)
    fig.colorbar(colored, ax=ax, fraction=.018, pad=.02, extend="both", label="净收益 %；颜色在 ±20% 饱和，格内为实际数值")
    fig.savefig(output / "matrix_returns.png", dpi=150)
    fig.savefig(output / "matrix_returns.svg")
    plt.close(fig)
    if args.matrix_only:
        return
    validation = runs.loc[runs.phase.eq("validation")].set_index("name")
    names = ["train60", "validation20", "final20", "main_1", "cost_1.5", "cost_2", "cost_3"]
    labels = ["独立训练段", "独立验证段", "独立最终段", "全区间", "成本 1.5×", "成本 2×", "成本 3×"]
    selected = validation.reindex(names)
    fig, axes = plt.subplots(1, 2, figsize=(15, 5), layout="constrained")
    colors = ["#178773" if value > 0 else "#ba5148" for value in selected.return_pct]
    axes[0].bar(range(len(names)), selected.return_pct, color=colors)
    axes[0].axhline(0, color="#555555", linewidth=.8)
    axes[0].set_xticks(range(len(names)), labels, rotation=25, ha="right")
    axes[0].set_ylabel("净收益 %")
    axes[0].set_title("默认策略：分段与全引擎成本重跑")
    for index, value in enumerate(selected.return_pct):
        axes[0].annotate(f"{value:.2f}%", (index, value), xytext=(0, 4 if value > 0 else -13), textcoords="offset points", ha="center", fontsize=9)
    missing = validation.loc[validation.index.str.startswith("missing_1pct_seed_")]
    axes[1].hist(missing.return_pct, bins=8, color="#527c9e", edgecolor="white")
    axes[1].axvline(float(validation.loc["main_1", "return_pct"]), color="#b37620", linestyle="--", label="无缺失基线")
    axes[1].set_title(f"1% 随机缺失：全部 {len(missing)} 个预登记种子")
    axes[1].set_xlabel("净收益 %")
    axes[1].set_ylabel("运行次数")
    axes[1].legend()
    fig.savefig(output / "default_validation.png", dpi=160)
    fig.savefig(output / "default_validation.svg")
    plt.close(fig)
    print(str(output), flush=True)


if __name__ == "__main__":
    main()
