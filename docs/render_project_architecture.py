from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 2800, 1900
BG = "#F5F7FB"
INK = "#172033"
MUTED = "#596579"
BLUE = "#2563EB"
GREEN = "#059669"
ORANGE = "#D97706"
RED = "#DC2626"
PURPLE = "#7C3AED"
CYAN = "#0891B2"
WHITE = "#FFFFFF"

font_path = Path("C:/Windows/Fonts/msyh.ttc")
font_bold_path = Path("C:/Windows/Fonts/msyhbd.ttc")

def font(size, bold=False):
    return ImageFont.truetype(str(font_bold_path if bold else font_path), size)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

def rounded(box, fill, outline="#D7DEEA", radius=22, width=3):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

def text(x, y, value, size=28, color=INK, bold=False, anchor="la"):
    d.text((x, y), value, font=font(size, bold), fill=color, anchor=anchor)

def box(x, y, w, h, title, lines, accent=BLUE, badge=None):
    rounded((x, y, x+w, y+h), WHITE)
    d.rounded_rectangle((x, y, x+12, y+h), radius=8, fill=accent)
    text(x+32, y+34, title, 31, INK, True)
    if badge:
        bw = d.textbbox((0, 0), badge, font=font(21, True))[2] + 30
        d.rounded_rectangle((x+w-bw-20, y+18, x+w-20, y+54), radius=18, fill=accent)
        text(x+w-bw/2-20, y+36, badge, 21, WHITE, True, "mm")
    yy = y+82
    for line in lines:
        text(x+34, yy, "• " + line, 23, MUTED)
        yy += 38

def arrow(points, color=BLUE, width=5, label=None, label_xy=None, dashed=False):
    if dashed:
        for i in range(len(points)-1):
            x1,y1=points[i]; x2,y2=points[i+1]
            steps=max(1,int(((x2-x1)**2+(y2-y1)**2)**0.5/22))
            for s in range(0,steps,2):
                a=s/steps; b=min((s+1)/steps,1)
                d.line((x1+(x2-x1)*a,y1+(y2-y1)*a,x1+(x2-x1)*b,y1+(y2-y1)*b),fill=color,width=width)
    else:
        d.line(points, fill=color, width=width, joint="curve")
    x1,y1=points[-2]; x2,y2=points[-1]
    import math
    a=math.atan2(y2-y1,x2-x1)
    l=18
    p1=(x2-l*math.cos(a-0.55),y2-l*math.sin(a-0.55))
    p2=(x2-l*math.cos(a+0.55),y2-l*math.sin(a+0.55))
    d.polygon([(x2,y2),p1,p2],fill=color)
    if label and label_xy:
        text(label_xy[0],label_xy[1],label,21,color,True,"mm")

text(100, 70, "Still Water QuantTrading — 当前项目详细架构", 54, INK, True)
text(100, 135, "代码证据快照 · 2026-08-02  |  蓝色=主数据/控制流  绿色=持久化/输出  虚线=可选或尚未完成", 25, MUTED)

# External and entry layers
box(100, 220, 500, 190, "外部输入与基础设施", ["Synthetic / Yahoo Finance / CCXT", "交易所 REST API（现货/合约）", "config/params.yaml + 环境变量"], CYAN, "BOUNDARY")
box(760, 220, 500, 190, "运行入口", ["main.py：交互式/CLI 回测", "run_live.py：轮询式实盘/沙箱", "analysis/、research/：研究与优化"], PURPLE, "ENTRY")
box(1420, 220, 560, 190, "数据接入与治理", ["DataFetcher：抓取、回退、合成场景", "DataHandler：校验、重采样、质量报告", "timeframes：周期解析与对齐"], CYAN, "DATA")
box(2140, 220, 560, 190, "配置与装配", ["config/config.py：YAML 加载与默认值", "system_factory：状态机/风控/策略/路由", "logger：统一日志"], PURPLE, "COMPOSITION")

# Core signal pipeline
box(100, 550, 500, 250, "指标与市场状态", ["Indicators：SMA / EMA / ATR / ADX / BBANDS", "MarketStateMachine：四种 Regime", "稳定性过滤与高/低周期对齐"], BLUE, "CORE")
box(760, 550, 500, 250, "策略路由 Router", ["Regime → Strategy 映射", "状态切换：撤销旧单、平仓、冷却", "每根 bar 记录 routing_log"], BLUE, "CONTROL")
box(1420, 550, 560, 250, "策略插件层", ["Strategy 抽象基类", "TrendBreakout / TrendBreakdown", "TrendUp / TrendDown / RangeMeanReversion"], BLUE, "SIGNAL")
box(2140, 550, 560, 250, "风险与组合", ["RiskManager：仓位、集中度、杠杆、流动性", "Portfolio：现金、持仓、权益、敞口", "valuation/domain：快照与共享领域对象"], ORANGE, "RISK")

# Runtime split
box(100, 970, 780, 300, "回测运行时", ["BacktestEngine：多标的联合时间线", "Next-Bar Execution，防前视", "Broker：Market/Limit/Stop、TIF、部分成交", "手续费、滑点、冲击成本与交易归因"], BLUE, "SIMULATION")
box(1010, 970, 780, 300, "实盘运行时", ["LiveTradingEngine：初始化、轮询、每标的幂等 bar", "LiveBroker：CCXT 同步账户/持仓并下单", "StateStore v2（SQLite）：日初权益、熔断、bar claim", "现货模式将做空路由映射为 Cash"], RED, "LIVE")
box(1920, 970, 780, 300, "报告、状态与可视化", ["ReportGenerator + core.metrics", "equity/trades/benchmark/routing CSV", "report.txt / equity.png / data_quality_report.json", "live_status.json；dashboard/utils.py 仅主题工具"], GREEN, "OUTPUT")

# Supporting / incomplete
box(100, 1440, 780, 260, "测试与质量门", ["tests/：单元、回归基线、无前视、实盘 mock", "当前项目虚拟环境：60/60 通过", "系统 Python 未安装依赖时：13 个模块导入失败"], GREEN, "VERIFIED")
box(1010, 1440, 780, 260, "研究与分析旁路", ["analysis/optimize.py：网格搜索", "plot_performance.py：报告再绘图", "research/：独立 alpha / reality check 原型"], PURPLE, "OFFLINE")
box(1920, 1440, 780, 260, "未完成/遗留区域", ["models/*：4 个空壳类（pass）", "Trading_V1_Model.py 与 archive/：旧实现并存", "state_store.py 与 state_store_v2.py 重复", "dashboard 尚无完整应用入口"], RED, "DEBT")

# Arrows top
arrow([(600,315),(760,315)], CYAN, label="数据源/凭据", label_xy=(680,292))
arrow([(1260,315),(1420,315)], BLUE, label="参数与命令", label_xy=(1340,292))
arrow([(1980,315),(2140,315)], PURPLE, label="装配依赖", label_xy=(2060,292))
arrow([(1700,410),(1700,480),(350,480),(350,550)], CYAN, label="OHLCV", label_xy=(1000,460))
arrow([(2420,410),(2420,490),(1010,490),(1010,550)], PURPLE, label="工厂构造", label_xy=(1800,472))

# Core flow
arrow([(600,675),(760,675)], BLUE, label="Regime", label_xy=(680,650))
arrow([(1260,675),(1420,675)], BLUE, label="选择策略", label_xy=(1340,650))
arrow([(1980,675),(2140,675)], ORANGE, label="信号/仓位请求", label_xy=(2060,650))
arrow([(2390,800),(2390,880),(490,880),(490,970)], ORANGE, label="通过风控的订单意图", label_xy=(1430,860))
arrow([(2500,800),(2500,905),(1400,905),(1400,970)], RED, label="实盘订单意图", label_xy=(1940,888))

# Outputs and feedback
arrow([(880,1120),(920,1120),(920,1350),(2180,1350),(2180,1270)], GREEN, label="回测结果", label_xy=(1500,1328))
arrow([(1790,1120),(1920,1120)], GREEN, label="状态/成交", label_xy=(1855,1095))
arrow([(1920,1190),(1790,1190)], GREEN, label="监控读取", label_xy=(1855,1218), dashed=True)
arrow([(490,1270),(490,1440)], GREEN, label="验证", label_xy=(535,1355))
arrow([(1400,1440),(1400,1370),(490,1370),(490,1270)], PURPLE, label="参数研究", label_xy=(950,1392), dashed=True)
arrow([(2310,1440),(2310,1270)], RED, label="待整合", label_xy=(2360,1355), dashed=True)

text(100, 1815, "关键边界：回测 Broker 与实盘 LiveBroker 共享策略/风控概念，但执行语义与持久化路径不同；SQLite/JSON/CSV 均为本地单机存储。", 25, INK, True)

out = Path(__file__).with_name("project_architecture_current.png")
img.save(out, optimize=True)
print(out.resolve())
