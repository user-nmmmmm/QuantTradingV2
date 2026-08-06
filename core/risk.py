from typing import Optional, Dict

from core.portfolio import Portfolio
from core.logger import get_logger

logger = get_logger(__name__)

"""
RiskManager（风险管理）模块

本模块对“准备下单”的交易做风控校验，并提供仓位计算工具。

核心职责：
- 仓位规模：按风险百分比（Risk%）或按固定资金占比（Fixed%）换算数量 qty
- 风险约束：流动性上限、杠杆上限、单标的集中度上限
- 熔断器：当出现超限回撤时，阻止后续开仓（将 qty 置 0 或直接拒单）

注意：
- 本 RiskManager 不直接下单；它只返回“是否允许交易/建议 qty”等结果。
- 回测与实盘共享同一套接口，但实盘更建议结合交易所真实仓位、保证金与订单簿深度做更严格校验。
"""

class RiskManager:
    def __init__(
        self, 
        risk_per_trade: float = 0.01,
        max_leverage: float = 3.0,
        max_drawdown_limit: float = 0.20,
        liquidity_limit_pct: float = 0.01, # Max 1% of bar volume
        max_pos_size_pct: float = 0.20 # Max 20% equity per position
    ):
        """
        初始化风控参数。

        参数：
        - risk_per_trade：单笔风险占权益比例（用于按止损距离定仓）
        - max_leverage：最大杠杆（gross exposure / equity）
        - max_drawdown_limit：日内回撤熔断阈值（示例：0.20 表示 -20%）
        - liquidity_limit_pct：流动性上限（单笔 qty 不超过当根 bar 成交量的比例）
        - max_pos_size_pct：单标的最大仓位占比（position value / equity）
        """
        self.risk_per_trade = risk_per_trade
        self.max_leverage = max_leverage
        self.max_drawdown_limit = max_drawdown_limit
        self.liquidity_limit_pct = liquidity_limit_pct
        self.max_pos_size_pct = max_pos_size_pct
        
        self.circuit_breaker_triggered = False

    def reset_daily_breaker(self) -> None:
        if self.circuit_breaker_triggered:
            logger.info("Resetting daily circuit breaker state")
        self.circuit_breaker_triggered = False

    def calculate_position_size(self, equity: float, entry_price: float, stop_loss_price: float) -> float:
        """
        按“固定风险比例”计算仓位数量（Risk-based Sizing）。

        直觉：
        - 先确定单笔最多愿意亏多少钱：risk_amount = equity * risk_per_trade
        - 再用止损距离换算数量：
          qty = risk_amount / |entry_price - stop_loss_price|

        返回：
        - qty：建议下单数量；若参数不合法或熔断器触发，则返回 0。
        """
        if self.circuit_breaker_triggered:
            return 0.0

        if entry_price <= 0 or stop_loss_price <= 0:
            return 0.0
            
        risk_amount = equity * self.risk_per_trade
        price_diff = abs(entry_price - stop_loss_price)
        
        if price_diff == 0:
            return 0.0
            
        qty = risk_amount / price_diff
        return qty

    def calculate_position_size_fixed_pct(self, equity: float, entry_price: float, pct: float = 0.10) -> float:
        """
        按“固定资金占比”计算仓位数量（Notional-based Sizing）。

        直觉：
        - allocation = equity * pct
        - qty = allocation / entry_price

        适用场景：
        - 没有明确止损价（无法按风险比例定仓）
        - 快速原型/简化回测
        """
        if self.circuit_breaker_triggered:
            return 0.0
            
        if entry_price <= 0:
            return 0.0
            
        allocation = equity * pct
        return allocation / entry_price

    def check_entry_risk(
        self, 
        portfolio: Portfolio, 
        symbol: str, 
        qty: float, 
        price: float,
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
        pending_open_notional: Optional[Dict[str, float]] = None,
    ) -> bool:
        """
        校验一笔“拟进入的交易”是否违反风控规则。

        输入：
        - portfolio：当前账户状态（现金、持仓）
        - symbol/qty/price：拟下单信息（qty 为绝对数量，side 在上层决定）
        - current_volume：当前 bar 的成交量（用于流动性上限）
        - current_prices：symbol -> 当前价格（用于敞口/权益估算）

        返回：
        - True：允许交易
        - False：拒绝交易
        """
        if self.circuit_breaker_triggered:
            logger.warning("Trade Rejected: Circuit Breaker Active")
            return False

        if qty <= 0 or price <= 0:
            return False
            
        # 1. Liquidity Check
        if current_volume > 0:
            max_qty = current_volume * self.liquidity_limit_pct
            if qty > max_qty:
                logger.warning(f"Trade Rejected: Liquidity Limit. Qty {qty:.4f} > Max {max_qty:.4f} (1% of {current_volume})")
                return False
                
        # 2. Leverage Check
        # Estimate new exposure
        trade_value = qty * price
        
        # Current exposure
        if current_prices is None:
            # 若缺少市价，Portfolio 内部会用 avg_price 回退估值（较粗略）
            current_exposure = portfolio.get_total_exposure({}) 
            current_equity = portfolio.get_total_value({})
        else:
            current_exposure = portfolio.get_total_exposure(current_prices)
            current_equity = portfolio.get_total_value(current_prices)
            
        if current_equity <= 0:
            return False
            
        reserved_by_symbol = pending_open_notional or {}
        reserved_exposure = sum(
            max(float(value), 0.0) for value in reserved_by_symbol.values()
        )
        new_exposure = current_exposure + reserved_exposure + trade_value
        projected_leverage = new_exposure / current_equity
        
        if projected_leverage > self.max_leverage:
            logger.warning(f"Trade Rejected: Leverage Limit. Projected {projected_leverage:.2f} > Max {self.max_leverage}")
            return False

        # 3. Concentration Check (Max Position Size)
        # Check if adding this trade makes this single position too large
        current_pos = portfolio.get_position(symbol)
        current_pos_value = abs(current_pos['qty']) * price # Approximate current value
        reserved_symbol_value = max(float(reserved_by_symbol.get(symbol, 0.0)), 0.0)
        new_pos_value = current_pos_value + reserved_symbol_value + trade_value
        
        if new_pos_value > (current_equity * self.max_pos_size_pct):
            logger.warning(f"Trade Rejected: Concentration Limit. Symbol {symbol} would be {new_pos_value/current_equity:.1%} > Max {self.max_pos_size_pct:.1%}")
            return False
            
        return True

    def check_circuit_breaker(self, current_equity: float, daily_start_equity: float) -> bool:
        """
        检查日内回撤并触发熔断器。

        规则：
        - drawdown = 1 - current_equity / daily_start_equity
        - 当 drawdown > max_drawdown_limit 时触发熔断：
          - circuit_breaker_triggered = True
          - 后续 calculate_position_size / check_entry_risk 会拒绝开仓

        返回：
        - True：熔断器处于触发状态（本次触发或此前已触发）
        - False：未触发
        """
        if self.circuit_breaker_triggered:
            return True # Already triggered

        if daily_start_equity <= 0:
            return False

        drawdown = 1 - (current_equity / daily_start_equity)
        
        if drawdown > self.max_drawdown_limit:
            self.circuit_breaker_triggered = True
            logger.error(
                "Circuit breaker triggered: intraday drawdown %.2f%% > limit %.2f%%",
                drawdown * 100,
                self.max_drawdown_limit * 100,
            )
            return True
            
        return False
