import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
from typing import List, Dict, Any, Tuple

from core.logger import get_logger

logger = get_logger(__name__)

"""
回测报告（ReportGenerator）模块

输出内容（写入 output_dir）：
- equity.csv：权益曲线（timestamp,equity,cash）
- benchmark.csv：基准曲线（可选）
- trades.csv：成交明细（来自 Broker.trades）
- report.txt：指标摘要与文件说明
- equity.png：净值、回撤、日收益、资金占用四联图

指标计算：
- 权益曲线：CAGR、最大回撤、月均收益、夏普（按 252 日年化）
- 交易明细：基于 FIFO 重建已平仓交易，计算胜率、盈亏比、期望值等
"""


class ReportGenerator:
    def __init__(self, output_dir: str):
        """
        参数：
        - output_dir：报告输出目录（不存在则创建）
        """
        self.output_dir = output_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

    def generate(
        self,
        trades: List[Dict],
        equity_curve: pd.DataFrame,
        metadata: Dict[str, Any] = None,
        benchmark_curve: pd.Series = None,
    ):
        """
        生成报告并返回指标字典。

        参数：
        - trades：Broker 记录的成交列表（每条为 dict）
        - equity_curve：以 timestamp 为索引的权益曲线 DataFrame
        - metadata：回测配置元信息（写入 report.txt）
        - benchmark_curve：基准曲线（Series，可选）
        """
        # 1. Save CSVs
        equity_curve.to_csv(os.path.join(self.output_dir, "equity.csv"))

        if benchmark_curve is not None:
            benchmark_curve.to_csv(os.path.join(self.output_dir, "benchmark.csv"))

        trades_df = pd.DataFrame(trades)
        if not trades_df.empty:
            trades_df.to_csv(os.path.join(self.output_dir, "trades.csv"), index=False)

        # 2. Calculate Metrics
        trade_metrics = self._analyze_trades(trades_df)
        equity_metrics = self._calculate_equity_metrics(equity_curve)

        metrics = {**equity_metrics, **trade_metrics}

        # 3. Save Report Text
        self._save_report_text(metrics, metadata)

        # 4. Generate Plots
        self._plot_equity(equity_curve, benchmark_curve)

        return metrics

    def _calculate_equity_metrics(self, equity_curve: pd.DataFrame) -> Dict[str, Any]:
        """
        根据权益曲线计算绩效指标。

        约定：
        - equity_curve.index 为 DatetimeIndex
        - 252 日年化用于夏普（适用于日线；若为其他周期需要自行调整）
        """
        equity = equity_curve["equity"]
        if equity.empty:
            return {}

        returns = equity.pct_change().dropna()

        start_val = equity.iloc[0]
        end_val = equity.iloc[-1]

        days = (equity.index[-1] - equity.index[0]).days
        years = max(days / 365.25, 0.01)

        cagr = (end_val / start_val) ** (1 / years) - 1

        # Max Drawdown
        rolling_max = equity.cummax()
        drawdown_pct = (equity - rolling_max) / rolling_max
        max_dd_pct = drawdown_pct.min()

        drawdown_amount = equity - rolling_max
        max_dd_amount = drawdown_amount.min()

        # Monthly Returns
        equity_curve["month"] = equity_curve.index.to_period("M")
        monthly_returns = equity_curve.groupby("month")["equity"].apply(
            lambda x: (x.iloc[-1] / x.iloc[0]) - 1
        )
        avg_monthly_return = monthly_returns.mean()

        # Sharpe
        if len(returns) < 2 or returns.std() == 0:
            sharpe = 0.0
        else:
            sharpe = returns.mean() / returns.std() * np.sqrt(252)

        return {
            "CAGR": cagr,
            "MaxDrawdownPct": max_dd_pct,
            "MaxDrawdownAmount": max_dd_amount,
            "AvgMonthlyReturn": avg_monthly_return,
            "SharpeRatio": sharpe,
            "EndEquity": end_val,
            "TotalReturn": (end_val - start_val) / start_val,
        }

    def _analyze_trades(self, trades_df: pd.DataFrame) -> Dict[str, Any]:
        """
        基于 trades.csv 重建已平仓交易并统计交易级指标。

        方法：
        - 按 symbol 分组
        - 使用 FIFO 栈（long_stack/short_stack）配对开平仓，计算每一笔闭合交易的 PnL
        - 统计全局指标与按策略归因指标（Strat_{name}_*）
        """
        if trades_df.empty:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": 0.0,
                "AvgTrade": 0.0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
            }

        # Reconstruct PnL using FIFO
        closed_trades = []  # List of dicts with pnl details

        # Group by symbol
        for symbol, group in trades_df.groupby("symbol"):
            long_stack: List[
                Tuple[float, float, str, float, float]
            ] = []  # (qty, price, strategy_id, unit_comm, unit_slip)
            short_stack: List[
                Tuple[float, float, str, float, float]
            ] = []  # (qty, price, strategy_id, unit_comm, unit_slip)

            for _, row in group.iterrows():
                side = row["side"]
                qty = row["qty"]
                price = row["fill_price"]
                comm = row["commission"]
                # Broker stores 'slip' as unit price difference (absolute)
                unit_slip = row.get("slip", 0.0)

                unit_comm = comm / qty if qty > 0 else 0.0

                strategy_id = row.get("strategy_id", "Unknown")

                if side == "buy":
                    # Check if covering short
                    remaining = qty
                    while remaining > 0 and short_stack:
                        s_qty, s_price, s_strat, s_unit_comm, s_unit_slip = (
                            short_stack.pop(0)
                        )
                        matched = min(remaining, s_qty)

                        # Short PnL: (Entry - Exit) * qty
                        gross_pnl = (s_price - price) * matched

                        # Commission: Entry + Exit
                        trade_comm = (s_unit_comm + unit_comm) * matched

                        # Slippage: Entry + Exit
                        # Note: Slippage is always a cost (positive value in record)
                        trade_slip = (s_unit_slip + unit_slip) * matched

                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": s_strat,
                            }
                        )

                        remaining -= matched
                        if s_qty > matched:
                            short_stack.insert(
                                0,
                                (
                                    s_qty - matched,
                                    s_price,
                                    s_strat,
                                    s_unit_comm,
                                    s_unit_slip,
                                ),
                            )

                    if remaining > 0:
                        long_stack.append(
                            (remaining, price, strategy_id, unit_comm, unit_slip)
                        )

                elif side == "sell":
                    # Close Long
                    remaining = qty
                    while remaining > 0 and long_stack:
                        l_qty, l_price, l_strat, l_unit_comm, l_unit_slip = (
                            long_stack.pop(0)
                        )
                        matched = min(remaining, l_qty)

                        # Long PnL: (Exit - Entry) * qty
                        gross_pnl = (price - l_price) * matched
                        trade_comm = (l_unit_comm + unit_comm) * matched
                        trade_slip = (l_unit_slip + unit_slip) * matched
                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": l_strat,
                            }
                        )

                        remaining -= matched
                        if l_qty > matched:
                            long_stack.insert(
                                0,
                                (
                                    l_qty - matched,
                                    l_price,
                                    l_strat,
                                    l_unit_comm,
                                    l_unit_slip,
                                ),
                            )

                    if remaining > 0:
                        short_stack.append(
                            (remaining, price, strategy_id, unit_comm, unit_slip)
                        )

                elif side == "short":
                    # Open Short
                    short_stack.append((qty, price, strategy_id, unit_comm, unit_slip))

                elif side == "cover":
                    # Close Short (Buy to Cover)
                    remaining = qty
                    while remaining > 0 and short_stack:
                        s_qty, s_price, s_strat, s_unit_comm, s_unit_slip = (
                            short_stack.pop(0)
                        )
                        matched = min(remaining, s_qty)

                        gross_pnl = (s_price - price) * matched
                        trade_comm = (s_unit_comm + unit_comm) * matched
                        trade_slip = (s_unit_slip + unit_slip) * matched
                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": s_strat,
                            }
                        )

                        remaining -= matched
                        if s_qty > matched:
                            short_stack.insert(
                                0,
                                (
                                    s_qty - matched,
                                    s_price,
                                    s_strat,
                                    s_unit_comm,
                                    s_unit_slip,
                                ),
                            )

                    if remaining > 0:
                        long_stack.append(
                            (remaining, price, strategy_id, unit_comm, unit_slip)
                        )

        if not closed_trades:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": 0.0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
            }

        # 1. Global Metrics
        all_net_pnls = [t["net_pnl"] for t in closed_trades]
        all_gross_pnls = [t["gross_pnl"] for t in closed_trades]
        all_comms = [t["commission"] for t in closed_trades]
        all_slips = [t["slippage"] for t in closed_trades]

        if not all_net_pnls:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": 0.0,
                "Expectancy": 0.0,
                "AvgWin": 0.0,
                "AvgLoss": 0.0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
            }

        wins = [p for p in all_net_pnls if p > 0]
        losses = [p for p in all_net_pnls if p <= 0]

        win_rate = len(wins) / len(all_net_pnls)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss != 0 else float("inf")

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

        metrics = {
            "TotalTrades": len(all_net_pnls),
            "WinRate": win_rate,
            "ProfitFactor": profit_factor,
            "Expectancy": expectancy,
            "AvgWin": avg_win,
            "AvgLoss": avg_loss,
            "GrossPnL": sum(all_gross_pnls),
            "TotalCommission": sum(all_comms),
            "TotalSlippage": sum(all_slips),
            "NetPnL": sum(all_net_pnls),
        }

        # 2. Per Strategy Metrics
        strat_map = {}
        for t in closed_trades:
            s = t["strategy"]
            if s not in strat_map:
                strat_map[s] = []
            strat_map[s].append(t)

        for s, trades in strat_map.items():
            pnls = [t["net_pnl"] for t in trades]
            s_wins = [p for p in pnls if p > 0]
            s_losses = [p for p in pnls if p <= 0]
            s_wr = len(s_wins) / len(pnls)
            s_pf = (
                sum(s_wins) / abs(sum(s_losses)) if sum(s_losses) != 0 else float("inf")
            )
            s_total = sum(pnls)
            s_comm = sum(t["commission"] for t in trades)
            s_slip = sum(t["slippage"] for t in trades)

            metrics[f"Strat_{s}_Trades"] = len(pnls)
            metrics[f"Strat_{s}_WinRate"] = s_wr
            metrics[f"Strat_{s}_ProfitFactor"] = s_pf
            metrics[f"Strat_{s}_NetPnL"] = s_total
            metrics[f"Strat_{s}_Comm"] = s_comm
            metrics[f"Strat_{s}_Slip"] = s_slip

        return metrics

    def _save_report_text(
        self, metrics: Dict[str, Any], metadata: Dict[str, Any] = None
    ):
        """
        将指标与配置写入 report.txt，并追加输出文件说明。
        """
        # Metrics Translation Map
        METRIC_NAMES = {
            "CAGR": "CAGR (年化收益率)",
            "MaxDrawdownPct": "Max Drawdown % (最大回撤率)",
            "MaxDrawdownAmount": "Max Drawdown $ (最大回撤金额)",
            "AvgMonthlyReturn": "Avg Monthly Return (月均收益率)",
            "SharpeRatio": "Sharpe Ratio (夏普比率)",
            "EndEquity": "End Equity (最终净值)",
            "TotalReturn": "Total Return (总收益率)",
            "TotalTrades": "Total Trades (总交易次数)",
            "WinRate": "Win Rate (胜率)",
            "ProfitFactor": "Profit Factor (盈亏比)",
            "Expectancy": "Expectancy (期望值)",
            "AvgWin": "Avg Win (平均盈利)",
            "AvgLoss": "Avg Loss (平均亏损)",
            "GrossPnL": "Gross PnL (毛利润)",
            "TotalCommission": "Total Commission (总手续费)",
            "TotalSlippage": "Total Slippage (总滑点成本)",
            "NetPnL": "Net PnL (净利润)",
        }

        path = os.path.join(self.output_dir, "report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Backtest Results (回测结果)\n")
            f.write("==========================\n\n")

            if metadata:
                f.write("Configuration (配置信息):\n")
                for k, v in metadata.items():
                    f.write(f"{k}: {v}\n")
                f.write("\n")

            f.write("Metrics (核心指标):\n")
            f.write("-----------------\n")
            for k, v in metrics.items():
                # Handle strategy specific metrics dynamically
                display_key = METRIC_NAMES.get(k, k)

                if isinstance(v, float):
                    f.write(f"{display_key:<45}: {v:.4f}\n")
                else:
                    f.write(f"{display_key:<45}: {v}\n")

            f.write("\n")
            f.write("File Descriptions (文件说明):\n")
            f.write("===========================\n")

            f.write("1. report.txt (回测报告概要)\n")
            f.write("   - Contains summary metrics and configuration parameters.\n")
            f.write("   - 包含核心指标汇总与回测参数配置。\n\n")

            f.write("2. equity.csv (净值曲线数据)\n")
            f.write("   - timestamp: Date (日期)\n")
            f.write("   - equity: Total Account Equity (总权益 = 现金 + 持仓市值)\n")
            f.write("   - cash: Available Cash (可用现金)\n\n")

            f.write("3. trades.csv (交易明细记录 - Execution Log)\n")
            f.write("   - signal_time: Time signal was generated (信号产生时间)\n")
            f.write("   - fill_time: Time order was filled (成交时间)\n")
            f.write("   - symbol: Trading Pair (交易标的)\n")
            f.write("   - side: buy/sell/short/cover (交易方向)\n")
            f.write("   - qty: Executed Quantity (成交数量)\n")
            f.write("   - fill_price: Executed Price (成交价格)\n")
            f.write("   - commission: Transaction Fee (手续费)\n")
            f.write("   - slip: Slippage Value (滑点金额)\n")
            f.write("   - slip_dir: Slippage Direction (滑点方向)\n")
            f.write("   - strategy_id: Strategy Name (策略名称)\n")
            f.write(
                "   - exit_reason: Reason for order (成交原因: signal/stop/takeprofit)\n"
            )

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

            plt.tight_layout()
            output_path = os.path.join(self.output_dir, "equity.png")
            plt.savefig(output_path, dpi=300)
            logger.info("Plot saved to: %s", output_path)
            plt.close()
        except Exception as e:
            logger.error("Error saving plot: %s", e)
