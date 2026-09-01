"""Chart rendering mixin for backtest reports."""

import os
from typing import Any, Dict, List

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from core.logger import get_logger
from core.metrics import infer_periods_per_year, monthly_returns

logger = get_logger(__name__)


class ReportChartsMixin:
    output_dir: str

    def _plot_equity(
        self, equity_curve: pd.DataFrame, benchmark_curve: pd.Series = None
    ):
        """
        生成 equity.png 四联图：
        1) 策略净值与基准净值
        2) 回撤曲线
        3) 日收益柱状图
        4) 现金/持仓市值堆叠图（若 equity_curve 含 cash 列）
        """
        try:
            # Create a figure with 4 subplots
            fig = plt.figure(figsize=(16, 12))
            gs = fig.add_gridspec(4, 1, height_ratios=[2, 1, 1, 1])

            ax1 = fig.add_subplot(gs[0])
            ax2 = fig.add_subplot(gs[1], sharex=ax1)
            ax3 = fig.add_subplot(gs[2], sharex=ax1)
            ax4 = fig.add_subplot(gs[3], sharex=ax1)

            # Ensure font supports basic text
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            # Plot 1: Equity Curve
            ax1.plot(
                equity_curve.index,
                equity_curve["equity"],
                label="Strategy Equity (策略净值)",
                color="blue",
                linewidth=1.5,
            )

            if benchmark_curve is not None:
                # Align benchmark to equity curve (ensure same index range if possible)
                # But usually plotting handles date index fine.
                ax1.plot(
                    benchmark_curve.index,
                    benchmark_curve,
                    label="Benchmark (Buy & Hold)",
                    color="gray",
                    linewidth=1.0,
                    linestyle="--",
                )

            ax1.set_title("Equity Curve (净值曲线)", fontsize=12, fontweight="bold")
            ax1.set_ylabel("Value (USDT)")
            ax1.legend(loc="upper left")
            ax1.grid(True, which="both", linestyle="--", alpha=0.6)

            # Plot 2: Drawdown
            rolling_max = equity_curve["equity"].cummax()
            drawdown = (equity_curve["equity"] - rolling_max) / rolling_max

            ax2.fill_between(
                drawdown.index, drawdown, 0, color="red", alpha=0.3, label="Drawdown"
            )
            ax2.plot(drawdown.index, drawdown, color="red", linewidth=1)
            ax2.set_title("Drawdown % (回撤率)")
            ax2.set_ylabel("Percentage")
            ax2.axhline(0, color="black", linewidth=0.5)
            ax2.grid(True, which="both", linestyle="--", alpha=0.6)

            # Plot 3: Daily Returns
            returns = equity_curve["equity"].pct_change().fillna(0)
            colors = ["green" if x >= 0 else "red" for x in returns]
            ax3.bar(
                returns.index, returns, color=colors, alpha=0.7, label="Daily Return"
            )
            ax3.set_title("Daily Returns (日收益率)")
            ax3.set_ylabel("Return %")
            ax3.grid(True, axis="y", linestyle="--", alpha=0.6)

            # Plot 4: Cash vs Position (Asset Allocation)
            # Assuming 'cash' column exists, otherwise infer from equity
            if "cash" in equity_curve.columns:
                cash = equity_curve["cash"]
                # Position value = Equity - Cash
                position_val = equity_curve["equity"] - cash

                ax4.stackplot(
                    equity_curve.index,
                    [cash, position_val],
                    labels=["Cash (现金)", "Position Value (持仓市值)"],
                    colors=["lightgray", "orange"],
                    alpha=0.6,
                )
                ax4.set_title("Asset Allocation (资产分布)")
                ax4.set_ylabel("Value (USDT)")
                ax4.legend(loc="upper left")
                ax4.grid(True, which="both", linestyle="--", alpha=0.6)

            ax4.set_xlabel("Date")

            # Save through the figure object, not the pyplot global "current
            # figure": a figure leaked by an earlier call would otherwise
            # receive the write, producing a wrong or empty image.
            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "equity.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving plot: %s", e)

    def _plot_monthly_heatmap(self, equity_curve: pd.DataFrame) -> None:
        """月度收益热力图（年 x 月）。数据不足（不足一个完整月）时跳过，不画空图。"""
        try:
            returns = monthly_returns(equity_curve["equity"])
            if returns.empty:
                logger.info("Monthly heatmap skipped: insufficient monthly samples")
                return

            table = returns.to_frame("ret")
            table["year"] = table.index.year
            table["month"] = table.index.month
            pivot = table.pivot(index="year", columns="month", values="ret")
            pivot = pivot.reindex(columns=range(1, 13))

            fig, ax = plt.subplots(
                figsize=(12, max(2.0, 0.6 * len(pivot) + 1.5))
            )
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            data = pivot.to_numpy(dtype=float)
            vmax = np.nanmax(np.abs(data)) if np.isfinite(data).any() else 1.0
            vmax = vmax if vmax > 0 else 1.0
            im = ax.imshow(data, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")

            ax.set_xticks(range(12))
            ax.set_xticklabels([f"{m:02d}" for m in range(1, 13)])
            ax.set_yticks(range(len(pivot)))
            ax.set_yticklabels(pivot.index.astype(str))
            ax.set_title("Monthly Returns (月度收益热力图)", fontsize=12, fontweight="bold")

            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    value = data[i, j]
                    if np.isnan(value):
                        continue
                    ax.text(
                        j, i, f"{value * 100:.1f}%",
                        ha="center", va="center", fontsize=8, color="black",
                    )

            fig.colorbar(im, ax=ax, label="Return")
            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "monthly_returns_heatmap.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving monthly heatmap: %s", e)

    def _plot_rolling_metrics(
        self, equity_curve: pd.DataFrame, window: int = 30
    ) -> None:
        """滚动 Sharpe 与滚动最大回撤（trailing window，无前视偏差）。

        样本不足 2*window 时跳过：滚动窗口指标在样本太短时噪声过大，容易被
        误读为有信息量的信号。
        """
        try:
            equity = equity_curve["equity"].dropna()
            if len(equity) < window * 2:
                logger.info(
                    "Rolling metrics skipped: %d samples < 2x window (%d)",
                    len(equity), window,
                )
                return

            periods_per_year = infer_periods_per_year(equity.index) or 252.0
            returns = equity.pct_change().dropna()

            rolling_mean = returns.rolling(window).mean()
            rolling_std = returns.rolling(window).std()
            rolling_sharpe = (
                rolling_mean / rolling_std * np.sqrt(periods_per_year)
            ).replace([np.inf, -np.inf], np.nan).dropna()

            rolling_max = equity.cummax()
            rolling_drawdown = (equity - rolling_max) / rolling_max
            rolling_max_dd = rolling_drawdown.rolling(window).min().dropna()

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            ax1.plot(rolling_sharpe.index, rolling_sharpe, color="steelblue")
            ax1.axhline(0, color="black", linewidth=0.5)
            ax1.set_title(
                f"Rolling Sharpe ({window}-period window) (滚动夏普)",
                fontsize=12, fontweight="bold",
            )
            ax1.grid(True, linestyle="--", alpha=0.6)

            ax2.fill_between(
                rolling_max_dd.index, rolling_max_dd, 0, color="red", alpha=0.3
            )
            ax2.plot(rolling_max_dd.index, rolling_max_dd, color="red", linewidth=1)
            ax2.set_title(
                f"Rolling Max Drawdown ({window}-period window) (滚动最大回撤)",
                fontsize=12, fontweight="bold",
            )
            ax2.set_xlabel("Date")
            ax2.grid(True, linestyle="--", alpha=0.6)

            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "rolling_metrics.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving rolling metrics plot: %s", e)

    def _plot_pnl_distribution(self, closed_trades: List[Dict[str, Any]]) -> None:
        """已平仓交易净盈亏分布直方图（区分盈利/亏损配色）。"""
        try:
            if not closed_trades:
                logger.info("PnL distribution skipped: no closed trades")
                return

            net_pnls = [t["net_pnl"] for t in closed_trades if t.get("net_pnl") is not None]
            if not net_pnls:
                return

            fig, ax = plt.subplots(figsize=(10, 6))
            plt.rcParams["font.sans-serif"] = ["SimHei", "Arial", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            wins = [p for p in net_pnls if p > 0]
            losses = [p for p in net_pnls if p <= 0]
            bins = min(30, max(5, len(net_pnls) // 2)) or 5

            ax.hist(wins, bins=bins, color="green", alpha=0.6, label=f"Wins (n={len(wins)})")
            ax.hist(losses, bins=bins, color="red", alpha=0.6, label=f"Losses (n={len(losses)})")
            ax.axvline(0, color="black", linewidth=0.8)
            ax.set_title("Closed Trade Net PnL Distribution (已平仓交易净盈亏分布)",
                         fontsize=12, fontweight="bold")
            ax.set_xlabel("Net PnL (USDT)")
            ax.set_ylabel("Count")
            ax.legend(loc="upper right")
            ax.grid(True, axis="y", linestyle="--", alpha=0.6)

            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "pnl_distribution.png")
            fig.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close(fig)
        except Exception as e:
            logger.error("Error saving PnL distribution plot: %s", e)
