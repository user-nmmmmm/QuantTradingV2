"""
Metrics（绩效指标）模块（占位）

历史上指标计算可能在 backtest/reporting.py 内完成；该文件保留为扩展点。
后续可将指标聚合到 core/metrics.py，便于：
- 回测与实盘共享同一套指标口径（Sharpe/MaxDD/Calmar 等）
- 单元测试更聚焦（指标函数纯计算、无 IO）
- 看板与报告复用（避免重复实现）
"""


class Metrics:
    """占位类：用于兼容未来的指标计算抽象。"""

    pass
