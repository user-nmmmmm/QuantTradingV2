"""Output renderers for a completed backtest report.

Each module here turns the already-computed metrics, equity curve and closed
trades into one artifact; none of them calculates a metric of its own. The
computation lives one level up in ``metrics.py`` and ``trades.py``.

- text.py     — report.txt
- charts.py   — equity.png and the companion diagnostic PNGs
- workbook.py — backtest_report.xlsx
- pdf.py      — report.pdf
"""
