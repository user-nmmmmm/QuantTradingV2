"""Chart rendering mixin for backtest reports."""

import os
from typing import Any, Dict, List

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator, PercentFormatter  # noqa: E402

from backtest.plot_style import (
    PALETTE,
    annotation_color,
    chart_style,
    diverging_cmap,
    money_axis,
    save_figure,
    style_axes,
)
from core.logger import get_logger
from core.metrics import infer_periods_per_year, monthly_returns

logger = get_logger(__name__)


def rolling_max_drawdown(equity: pd.Series, window: int) -> pd.Series:
    """Maximum peak-to-later-trough loss using only each trailing window."""
    if window < 2:
        raise ValueError("window must contain at least two observations")
    return equity.rolling(window, min_periods=window).apply(
        lambda values: np.min(values / np.maximum.accumulate(values) - 1), raw=True
    )


class ReportChartsMixin:
    output_dir: str

    @chart_style()
    def _plot_equity(
        self, equity_curve: pd.DataFrame, benchmark_curve: pd.Series = None
    ):
        """
        生成 equity.png 四联图：
        1) 策略净值与基准净值
        2) 回撤曲线
        3) 每周期收益柱状图
        4) 现金/非现金净权益堆叠图（若 equity_curve 含 cash 列）
        """
        fig = None
        try:
            fig = plt.figure(figsize=(14.4, 11.6))
            gs = fig.add_gridspec(4, 1, height_ratios=[2.1, 1, 1, 1.2])

            ax1 = fig.add_subplot(gs[0])
            ax2 = fig.add_subplot(gs[1], sharex=ax1)
            ax3 = fig.add_subplot(gs[2], sharex=ax1)
            ax4 = fig.add_subplot(gs[3], sharex=ax1)

            for ax in (ax1, ax2, ax3, ax4):
                style_axes(ax, dates=True)
                ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
                ax.margins(x=0.015)
            for ax in (ax1, ax2, ax3):
                ax.tick_params(axis="x", labelbottom=False)

            # Plot 1: Equity Curve
            ax1.plot(
                equity_curve.index,
                equity_curve["equity"],
                label="策略净权益",
                color=PALETTE["positive"],
                linewidth=1.9,
            )

            if benchmark_curve is not None:
                # Align benchmark to equity curve (ensure same index range if possible)
                # But usually plotting handles date index fine.
                ax1.plot(
                    benchmark_curve.index,
                    benchmark_curve,
                    label="买入持有基准",
                    color=PALETTE["benchmark"],
                    linewidth=1.25,
                    linestyle=(0, (4, 3)),
                )

            ax1.set_title("账户净权益")
            ax1.set_ylabel("USDT")
            money_axis(ax1)
            ax1.legend(loc="lower right", bbox_to_anchor=(1, 1.02), ncol=2)

            # Plot 2: Drawdown
            rolling_max = equity_curve["equity"].cummax()
            drawdown = (equity_curve["equity"] - rolling_max) / rolling_max

            ax2.fill_between(
                drawdown.index, drawdown, 0, color=PALETTE["negative"], alpha=0.16
            )
            ax2.plot(drawdown.index, drawdown, color=PALETTE["negative"], linewidth=1.25)
            ax2.set_title("历史高点回撤")
            ax2.set_ylabel("回撤率")
            ax2.yaxis.set_major_formatter(PercentFormatter(xmax=1))
            ax2.axhline(0, color=PALETTE["muted"], linewidth=0.7, alpha=0.65)

            # Plot 3: Returns for each observed period (daily or intraday).
            returns = equity_curve["equity"].pct_change().fillna(0)
            colors = [PALETTE["positive"] if x >= 0 else PALETTE["negative"] for x in returns]
            ax3.bar(
                returns.index, returns, color=colors, alpha=0.8, linewidth=0
            )
            ax3.set_title("每周期收益率")
            ax3.set_ylabel("收益率")
            ax3.yaxis.set_major_formatter(PercentFormatter(xmax=1))
            ax3.axhline(0, color=PALETTE["muted"], linewidth=0.7, alpha=0.65)

            # Equity less cash is net non-cash equity, including in margin accounts.
            ax4.set_title("账户权益构成")
            ax4.set_ylabel("USDT")
            money_axis(ax4)
            if "cash" in equity_curve.columns:
                cash = equity_curve["cash"]
                position_val = equity_curve["equity"] - cash

                ax4.stackplot(
                    equity_curve.index,
                    [cash, position_val],
                    labels=["现金", "非现金净权益"],
                    colors=[PALETTE["benchmark"], PALETTE["positive"]],
                    alpha=0.28,
                    linewidth=0,
                )
                ax4.legend(loc="lower right", bbox_to_anchor=(1, 1.02), ncol=2)
            else:
                ax4.text(
                    0.02, 0.82, "未提供现金序列", transform=ax4.transAxes,
                    color=PALETTE["muted"], fontsize=9,
                )

            # BM3: gross leverage on a twin axis. The stack above shows where
            # the money sits; this shows how much risk it carries, which is
            # the difference between a flat curve run in cash and one run at
            # 2x. Only drawn when the engine recorded exposure columns.
            if "gross_exposure_pct_equity" in equity_curve.columns:
                leverage = pd.to_numeric(
                    equity_curve["gross_exposure_pct_equity"], errors="coerce"
                )
                if leverage.notna().any():
                    ax4b = ax4.twinx()
                    style_axes(ax4b, grid_axis=None)
                    ax4b.plot(
                        equity_curve.index,
                        leverage,
                        color=PALETTE["purple"],
                        linewidth=1.3,
                        label="总敞口 / 净权益",
                    )
                    ax4b.set_ylabel("总杠杆", color=PALETTE["purple"])
                    ax4b.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}×"))
                    ax4b.yaxis.set_major_locator(MaxNLocator(nbins=4))
                    ax4b.tick_params(axis="y", colors=PALETTE["purple"])
                    ax4b.set_ylim(bottom=0)
                    ax4b.legend(loc="upper right")

            ax4.set_xlabel("日期")

            # Save through the figure object, not the pyplot global "current
            # figure": a figure leaked by an earlier call would otherwise
            # receive the write, producing a wrong or empty image.
            fig.tight_layout(h_pad=2.3)
            output_path = os.path.join(self.output_dir, "equity.png")
            save_figure(fig, output_path)
            logger.info("Plot saved to: %s", output_path)
        except Exception as e:
            logger.error("Error saving plot: %s", e)
        finally:
            if fig is not None:
                plt.close(fig)

    @chart_style()
    def _plot_monthly_heatmap(self, equity_curve: pd.DataFrame) -> None:
        """月度收益热力图（年 x 月）。数据不足（不足一个完整月）时跳过，不画空图。"""
        fig = None
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
                figsize=(12.8, max(2.8, 0.62 * len(pivot) + 1.9))
            )
            style_axes(ax, grid_axis=None)

            data = pivot.to_numpy(dtype=float)
            vmax = np.nanmax(np.abs(data)) if np.isfinite(data).any() else 1.0
            vmax = vmax if vmax > 0 else 1.0
            im = ax.imshow(
                np.ma.masked_invalid(data), cmap=diverging_cmap(),
                vmin=-vmax, vmax=vmax, aspect="auto", interpolation="nearest",
            )

            ax.set_xticks(range(12))
            ax.set_xticklabels([f"{m:02d}" for m in range(1, 13)])
            ax.set_yticks(range(len(pivot)))
            ax.set_yticklabels(pivot.index.astype(str))
            ax.set_title("月度收益率")
            ax.set_xlabel("月份 · — 表示无数据")
            ax.set_ylabel("年份")
            ax.set_xticks(np.arange(-0.5, 12, 1), minor=True)
            ax.set_yticks(np.arange(-0.5, len(pivot), 1), minor=True)
            ax.grid(which="minor", color=PALETTE["surface"], linewidth=2)
            ax.set_axisbelow(False)

            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    value = data[i, j]
                    if not np.isfinite(value):
                        ax.text(
                            j, i, "—", ha="center", va="center",
                            fontsize=9, color=PALETTE["muted"],
                        )
                        continue
                    ax.text(
                        j, i, f"{value * 100:.1f}%",
                        ha="center", va="center", fontsize=9,
                        color=annotation_color(im.cmap(im.norm(value))),
                    )

            colorbar = fig.colorbar(im, ax=ax, label="月收益率", fraction=0.025, pad=0.025)
            colorbar.ax.yaxis.set_major_formatter(PercentFormatter(xmax=1))
            colorbar.outline.set_visible(False)
            colorbar.ax.tick_params(length=0)
            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "monthly_returns_heatmap.png")
            save_figure(fig, output_path)
            logger.info("Plot saved to: %s", output_path)
        except Exception as e:
            logger.error("Error saving monthly heatmap: %s", e)
        finally:
            if fig is not None:
                plt.close(fig)

    @chart_style()
    def _plot_rolling_metrics(
        self, equity_curve: pd.DataFrame, window: int = 30
    ) -> None:
        """滚动 Sharpe 与滚动最大回撤（trailing window，无前视偏差）。

        样本不足 2*window 时跳过：滚动窗口指标在样本太短时噪声过大，容易被
        误读为有信息量的信号。
        """
        fig = None
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

            rolling_max_dd = rolling_max_drawdown(equity, window).dropna()

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12.8, 7.6), sharex=True)
            style_axes(ax1, dates=True)
            style_axes(ax2, percent=True, dates=True)
            for ax in (ax1, ax2):
                ax.margins(x=0.015)
                ax.yaxis.set_major_locator(MaxNLocator(nbins=5))

            # Leave undefined (e.g. zero-volatility) intervals visibly blank;
            # joining across dropped samples would imply values not calculated.
            ax1.plot(
                returns.index, rolling_sharpe.reindex(returns.index),
                color=PALETTE["blue"],
            )
            ax1.axhline(0, color=PALETTE["muted"], linewidth=0.7, alpha=0.65)
            ax1.set_title(f"滚动夏普 · {window} 期")
            ax1.set_ylabel("年化夏普")

            ax2.fill_between(
                rolling_max_dd.index, rolling_max_dd, 0, color=PALETTE["negative"], alpha=0.16
            )
            ax2.plot(
                rolling_max_dd.index, rolling_max_dd,
                color=PALETTE["negative"], linewidth=1.3,
            )
            ax2.axhline(0, color=PALETTE["muted"], linewidth=0.7, alpha=0.65)
            ax2.set_title(f"滚动最大回撤 · {window} 期")
            ax2.set_ylabel("回撤率")
            ax2.set_xlabel("日期")

            fig.tight_layout(h_pad=2.3)
            output_path = os.path.join(self.output_dir, "rolling_metrics.png")
            save_figure(fig, output_path)
            logger.info("Plot saved to: %s", output_path)
        except Exception as e:
            logger.error("Error saving rolling metrics plot: %s", e)
        finally:
            if fig is not None:
                plt.close(fig)

    @chart_style()
    def _plot_pnl_distribution(self, closed_trades: List[Dict[str, Any]]) -> None:
        """已平仓交易净盈亏分布直方图（区分盈利/亏损配色）。"""
        fig = None
        try:
            if not closed_trades:
                logger.info("PnL distribution skipped: no closed trades")
                return

            net_pnls = [t["net_pnl"] for t in closed_trades
                        if t.get("net_pnl") is not None and np.isfinite(t["net_pnl"])]
            if not net_pnls:
                return

            fig, ax = plt.subplots(figsize=(10.8, 6.2))
            style_axes(ax)

            wins = [p for p in net_pnls if p > 0]
            losses = [p for p in net_pnls if p <= 0]
            bins = np.histogram_bin_edges(net_pnls, bins=min(30, max(5, len(net_pnls) // 2)))

            ax.hist(
                wins, bins=bins, color=PALETTE["positive"], alpha=0.85,
                edgecolor=PALETTE["surface"], linewidth=0.8,
                label=f"盈利 · {len(wins)} 笔",
            )
            ax.hist(
                losses, bins=bins, color=PALETTE["negative"], alpha=0.85,
                edgecolor=PALETTE["surface"], linewidth=0.8,
                label=f"非盈利（含零）· {len(losses)} 笔",
            )
            ax.axvline(0, color=PALETTE["muted"], linewidth=0.9, linestyle=(0, (3, 3)))
            ax.set_title("已平仓交易净盈亏")
            ax.set_xlabel("净盈亏（USDT）")
            ax.set_ylabel("交易笔数")
            ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
            money_axis(ax, axis="x")
            ax.legend(loc="lower right", bbox_to_anchor=(1, 1.02), ncol=2)
            ax.margins(y=0.12)

            fig.tight_layout()
            output_path = os.path.join(self.output_dir, "pnl_distribution.png")
            save_figure(fig, output_path)
            logger.info("Plot saved to: %s", output_path)
        except Exception as e:
            logger.error("Error saving PnL distribution plot: %s", e)
        finally:
            if fig is not None:
                plt.close(fig)
