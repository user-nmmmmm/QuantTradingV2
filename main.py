"""Entry point of ``quant-backtest``; the implementation lives in the ``cli`` package (ADR 0019)."""
from cli.run import main

if __name__ == "__main__":
    raise SystemExit(main())
