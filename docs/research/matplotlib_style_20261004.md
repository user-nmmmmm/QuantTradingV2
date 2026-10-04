# Matplotlib 原生图表样式

已将标准回测报告和当前研究图表接入可复用的 Matplotlib 样式。默认采用浅底、青绿色策略线、灰蓝色基准线和珊瑚色负值，配合轻水平网格、中文字体回退、精简坐标与外置图例。

## 使用入口

共享实现位于 `backtest/plot_style.py`，支持上下文管理器及函数装饰器。样式退出后恢复调用者的 `rcParams`，正常返回与异常退出都不会遗留全局字体、配色或网格设置。

```python
import matplotlib.pyplot as plt
from backtest.plot_style import chart_style, style_axes, money_axis, save_figure

with chart_style():  # 可选 chart_style("dark")
    fig, ax = plt.subplots(figsize=(12, 6))
    try:
        ax.plot(equity.index, equity.values, label="策略净值")
        ax.set_title("账户净值")
        ax.set_ylabel("USDT")
        style_axes(ax, dates=True)
        money_axis(ax)
        ax.legend()
        save_figure(fig, "equity.png")
    finally:
        plt.close(fig)
```

百分比原始数据为小数比例时使用 `style_axes(ax, percent=True)`，已经乘以100的数据使用 `percent_scale=100`。导出格式由扩展名决定；保存函数只保存指定Figure，关闭责任由调用者承担。

## 已接入的输出

| 绘图入口 | 输出与变化 |
| --- | --- |
| `backtest/reporting/render/charts.py` | 标准权益四联图、月度收益热图、滚动风险、交易盈亏分布。原PNG名及报告调用契约保留。 |
| `scripts/plot_return_followup.py` | 原生净值／回撤上下双图、共用日期轴、末端引线标注；默认同时导出浅色和深色PNG、SVG、PDF。 |
| `scripts/run_paper_roadmap.py::make_plots` | 冻结窗口横条比较、容量敏感度图；正确比例刻度、统一颜色与图例。 |
| `scripts/plot_strategy_review.py` | 固定登记顺序的矩阵热图、验证收益与缺失种子分布。 |

标准回测的 `full` 和 `compact` 图像使用新样式；`workbook` 仍使用其原生Excel图。既有历史实验产物保留。重绘当前比较图只需运行：

```powershell
.venv\Scripts\python.exe scripts/plot_return_followup.py
```

结果保存在 `reports/paper_return_followup_spot_20261004/matplotlib_v3/`。标准报告四类图的真实样本预览位于 `reports/matplotlib_style_20261004/core/`。

## 数据语义与验证

- 保留原始每日路径及计算公式，所有原始CSV保持不变；本轮只重绘已有结果，没有重跑策略。
- “每周期收益率”兼容日内和日线；“非现金净权益”表达权益减现金，不将保证金账户的该差额误称为持仓名义市值。
- 月度热图缺失月份显示灰格与横线，和真实零收益分别呈现；零点居中，单元格文字按实际底色选择黑／白，256级最小对比度4.588:1。
- 无定义的滚动夏普保留为断线；真实样本的928个无定义观测没有被连接成斜线，有效数值保持一致。
- 四类绘图方法在保存成功或失败后均只关闭自己创建的Figure，保留调用者已有图形。
- 标准图与研究报告32项现有测试、PDF与报告profile10项现有测试通过，共42项不同用例；并完成静态检查、源CSV哈希核对及实际图像视觉检查。并行保存造成的一次快照读取失败已在源文件稳定后补跑通过，详见验收记录。

验证记录位于 `reports/matplotlib_style_20261004/validation.json`；当前比较图的数据与指标核对位于 `reports/paper_return_followup_spot_20261004/matplotlib_v3/validation.json`。
