"""Build equity-curve rows from the historical portfolio state."""

from typing import Any, Dict

import pandas as pd

from core.portfolio import Portfolio


def sample_exposure(
    portfolio: Portfolio,
    prices: Dict[str, float],
    equity_row: Dict[str, Any],
) -> None:
    """Reduce the current book using calculate_exposure's exact rules.

    Retaining only the five reported scalars keeps exposure storage linear
    in bars, independent of how many symbols were held on each bar.
    """
    gross = 0.0
    net = 0.0
    priced_symbols = 0
    for symbol in portfolio.positions:
        qty = float(portfolio.get_position(symbol).get("qty", 0.0))
        if qty == 0.0:
            continue
        price = prices.get(symbol)
        if price is None:
            continue
        notional = qty * float(price)
        gross += abs(notional)
        net += notional
        priced_symbols += 1
    equity = equity_row["equity"]
    equity_row.update(
        gross_exposure=gross,
        net_exposure=net,
        priced_symbols=priced_symbols,
        gross_exposure_pct_equity=gross / equity if equity else None,
        net_exposure_pct_equity=net / equity if equity else None,
    )


def equity_frame(equity_rows: list) -> pd.DataFrame:
    """Build the equity curve with exposure already paired to each row."""
    frame = pd.DataFrame(equity_rows)
    if frame.empty:
        return frame
    return frame.set_index("timestamp")
