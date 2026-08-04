from datetime import datetime
from typing import Dict

from core.domain import PortfolioSnapshot
from core.portfolio import Portfolio


def build_portfolio_snapshot(
    portfolio: Portfolio,
    prices: Dict[str, float],
    price_times: Dict[str, datetime],
    synced_at: datetime,
) -> PortfolioSnapshot:
    missing = [symbol for symbol in portfolio.positions if symbol not in prices or prices[symbol] <= 0]
    if missing:
        raise ValueError(f"Missing fresh prices for positions: {', '.join(sorted(missing))}")
    equity = portfolio.get_equity(prices)
    gross = portfolio.get_total_exposure(prices)
    net = sum(pos["qty"] * prices[symbol] for symbol, pos in portfolio.positions.items())
    return PortfolioSnapshot(portfolio.cash, equity, gross, net, dict(prices), dict(price_times), synced_at)
