from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# =========================
# 交易窗口工具（北京时间）
# =========================
# 定义北京时区，用于后续时间转换
CN_TZ = ZoneInfo("Asia/Shanghai")


def to_cn(dt: datetime) -> datetime:
    """
    将引擎传入的时间转换为北京时间。

    Args:
        dt (datetime): 引擎传入的时间，可能是 UTC 或无时区。

    Returns:
        datetime: 转换后的北京时间（带时区信息）。
    """
    if dt.tzinfo is None:
        # 如果引擎传进来是“无时区”，默认按北京时间解释（或根据平台实际情况调整）
        return dt.replace(tzinfo=CN_TZ)
    # 如果有时区，则转换到北京时间
    return dt.astimezone(CN_TZ)


def in_trade_window_cn(
    dt_cn: datetime, start_h=20, start_m=0, end_h=8, end_m=0
) -> bool:
    """
    判断当前时间是否在允许交易的窗口内。
    默认窗口：每日 20:00 到次日 08:00（跨天窗口）。

    Args:
        dt_cn (datetime): 当前北京时间。
        start_h (int): 窗口开始小时。
        end_h (int): 窗口结束小时。

    Returns:
        bool: True 表示在窗口内，允许交易。
    """
    t = dt_cn.time()
    start = time(start_h, start_m)
    end = time(end_h, end_m)

    # 跨天窗口逻辑：
    # 例如 20:00 - 08:00
    # 时间 >= 20:00 (当天晚间) OR 时间 < 08:00 (次日凌晨) 均为 True
    return (t >= start) or (t < end)


def session_id_cn(dt_cn: datetime, start_h=20, start_m=0) -> str:
    """
    生成交易会话 ID (Session ID)，用于标识属于哪一天的交易计划。

    逻辑：
    - 以“窗口起点日期”作为 Session ID。
    - 例如：2026-02-03 20:00 ~ 2026-02-04 08:00 这一整个夜盘时段，
      统一归属为 "2026-02-03" 的 Session。

    Args:
        dt_cn (datetime): 当前北京时间。

    Returns:
        str: 日期字符串 "YYYY-MM-DD"。
    """
    start = time(start_h, start_m)
    # 如果当前时间是凌晨（如 00:00 ~ 07:59），它属于“前一天”启动的夜盘
    if dt_cn.time() < time(8, 0):
        return (dt_cn.date() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 如果是 20:00 之后，属于当天启动的夜盘
    # (08:00~19:59 期间虽然不在窗口，但如果调用此函数，归为当天也无妨)
    return dt_cn.date().strftime("%Y-%m-%d")


# =========================
# 初始化：加密版核心参数
# =========================
def m3_initialize_bigquant_run(context):
    """
    策略初始化函数：设置参数、全局变量和数据预处理。
    """
    # ---------------------------
    # 1. 仓位与资金参数
    # ---------------------------
    context.stock_count = 10  # 目标持仓数量
    context.stock_weights = 1 / context.stock_count  # 单只股票目标权重（等权）
    context.change_num = 1  # 每次轮动/调仓时，最多卖出的股票数量（用于控制换手率）

    # ---------------------------
    # 2. 交易时间窗口设置 (北京时间)
    # ---------------------------
    context.window_start_h, context.window_start_m = 20, 0  # 开始：20:00
    context.window_end_h, context.window_end_m = 8, 0  # 结束：08:00

    # ---------------------------
    # 3. 调仓频率控制
    # ---------------------------
    context.rebalance_every_minutes = 60  # 每 60 分钟检查一次调仓信号
    context.last_rebalance_dt_cn = None  # 记录上次调仓时间

    # ---------------------------
    # 4. 风控参数
    # ---------------------------
    # 是否在交易窗口外执行风控订单（如止损）
    context.enable_risk_orders_outside_window = True
    context.stop_loss_pct = 0.08  # 止损阈值：跌幅超过 8% 止损

    # ---------------------------
    # 5. 信号数据预处理
    # ---------------------------
    # 复制一份信号数据，以免修改原始数据
    # context.data 包含预测结果（instrument, score/rank/position, date/dt）
    context.my_data = context.data.copy()

    # 排序：确保数据按时间(dt/date)和排名(position)有序
    # 加密货币交易建议使用 'dt' (datetime) 字段以支持日内高频信号
    if "dt" in context.my_data.columns:
        context.my_data = context.my_data.sort_values(["dt", "position"])
    else:
        # 兼容旧模式：只有 'date' (日期) 字段
        context.my_data = context.my_data.sort_values(["date", "position"])


# =========================
# （可选）窗口外风控示例
# =========================
def risk_management(context, data):
    """
    风控模块：在非交易窗口期间执行（如紧急止损）。

    注意：
    - 此处仅为示例框架，具体止损逻辑需结合平台 API 实现。
    - 需要获取持仓成本 (cost_basis) 和当前价格 (current_price)。
    """
    # 遍历当前所有持仓
    for ins, pos in context.portfolio.positions.items():
        if pos.amount <= 0:
            continue  # 跳过空仓或空头（如果是现货策略）

        # TODO: 获取持仓成本和现价
        # cost = pos.cost_basis  # 你的平台持仓成本字段
        # price = data.current(ins, "close") # 当前最新价

        # 示例止损逻辑：
        # if cost and price and (price / cost - 1) <= -context.stop_loss_pct:
        #     # 跌幅超过阈值，清仓该标的
        #     print(f"触发止损: {ins}, 成本: {cost}, 现价: {price}")
        #     context.order_target(ins, 0)

        pass


# =========================
# 调仓函数：执行买卖操作
# =========================
def rebalance(context, data, ranker_prediction):
    """
    根据预测信号执行调仓逻辑。

    逻辑：
    1. 卖出：不在预测列表中的股票。
    2. 卖出：在预测列表中但排名靠后的股票（根据 change_num 控制数量）。
    3. 买入：用可用资金买入排名靠前的股票，补齐到目标持仓数量。

    Args:
        ranker_prediction (DataFrame): 当前时刻/Session 的预测信号，已按 position 排序。
    """
    # 获取当前持仓的股票代码列表
    stock_now = {e: p for e, p in context.portfolio.positions.items() if p.amount > 0}
    stock_now_num = len(stock_now)

    # 获取当期建议买入的股票列表
    # 多取 3 只作为备选，防止某些股票停牌或无法买入
    try:
        buy_list = ranker_prediction.instrument.unique()[: context.stock_count + 3]
    except Exception:
        buy_list = []

    sell_num = 0  # 记录本轮已卖出的数量

    if len(stock_now) > 0:
        # ---------------------------
        # 1. 卖出逻辑 A：不在预测集中的股票
        # ---------------------------
        # 如果持仓股票已经不在当期的预测信号里（说明模型不再看好），直接卖出
        pred_set = set(ranker_prediction.instrument.unique())
        need_sell = [x for x in stock_now if x not in pred_set]

        for instrument in need_sell:
            # order_target(ins, 0) 表示将仓位调整为 0（即全卖）
            rv = context.order_target(instrument, 0)
            if rv != 0:
                print(f"{instrument} 不在预测集卖出失败: {context.get_error_msg(rv)}")
                continue
            sell_num += 1

        # ---------------------------
        # 2. 卖出逻辑 B：排名下降的股票 (轮动)
        # ---------------------------
        # 即使股票还在预测集中，但如果排名靠后（position 值大），也可能被替换
        # 获取持仓中且在预测集里的股票
        held = ranker_prediction[
            ranker_prediction.instrument.apply(lambda x: x in stock_now)
        ]
        # 按 position 降序排列（假设 position 越小越好，越大越差）
        # 优先卖出 position 大的
        held_sorted = held.sort_values("position", ascending=False)[
            "instrument"
        ].tolist()

        for instrument in held_sorted:
            # 如果卖出数量已达到限制 (change_num)，停止卖出
            if sell_num >= context.change_num:
                break

            rv = context.order_target(instrument, 0)
            if rv != 0:
                print(f"{instrument} 卖出失败: {context.get_error_msg(rv)}")
                continue

            sell_num += 1
            stock_now_num -= 1
            # 从本地持仓记录中移除
            stock_now.pop(instrument, None)

    # ---------------------------
    # 3. 买入逻辑：补齐仓位
    # ---------------------------
    # 如果有买入信号，且当前持仓数不足目标数量
    if len(buy_list) > 0 and stock_now_num < context.stock_count:
        # 筛选出不在当前持仓中的买入目标
        buy_instruments = [i for i in buy_list if i not in stock_now]

        # 计算每只新买入股票可用的资金
        # 剩余空位数量
        slots_left = max(context.stock_count - stock_now_num, 1)
        # 简单的资金分配：剩余现金 / 剩余空位
        # 注意：这里可能需要优化，防止资金利用率不足
        cash_now = context.portfolio.cash / slots_left

        # 确保单只股票买入金额不超过理论上限（总权益 * 单只权重）
        cash_for_buy = min(
            context.portfolio.portfolio_value * context.stock_weights, cash_now
        )

        for instrument in buy_instruments:
            # 如果已达到最大持仓数，停止买入
            if stock_now_num >= context.stock_count:
                break

            # 下单买入指定金额
            rv = context.order_value(instrument, cash_for_buy)
            if rv != 0:
                print(f"{instrument} 买入失败: {context.get_error_msg(rv)}")
                continue

            stock_now_num += 1


# =========================
# handle_data：主循环逻辑
# =========================
def m3_handle_data_bigquant_run(context, data):
    """
    K线/Bar 事件处理函数。引擎每经过一个时间步（如每分钟/每小时）调用一次。
    """
    # 转换当前时间为北京时间
    dt_cn = to_cn(data.current_dt)

    # ---------------------------
    # 1. 判断是否在交易窗口
    # ---------------------------
    in_window = in_trade_window_cn(
        dt_cn,
        context.window_start_h,
        context.window_start_m,
        context.window_end_h,
        context.window_end_m,
    )

    # ---------------------------
    # 2. 窗口外逻辑
    # ---------------------------
    if not in_window:
        # 如果开启了窗口外风控，执行风控检查
        if getattr(context, "enable_risk_orders_outside_window", False):
            risk_management(context, data)
        return  # 不执行常规调仓

    # ---------------------------
    # 3. 调仓频率控制
    # ---------------------------
    # 如果距离上次调仓时间不足 N 分钟，跳过
    if context.last_rebalance_dt_cn is not None:
        if (dt_cn - context.last_rebalance_dt_cn) < timedelta(
            minutes=context.rebalance_every_minutes
        ):
            return

    # ---------------------------
    # 4. 获取当前 Session 的交易信号
    # ---------------------------
    # 计算当前 Session ID（日期）
    sid = session_id_cn(dt_cn, context.window_start_h, context.window_start_m)

    if "dt" in context.my_data.columns:
        # === 模式 A：基于精确时间 (dt) 的信号 ===
        # 建议信号数据包含 'dt' 字段，精确到分钟或小时

        df = context.my_data
        # 类型转换（如果 dt 是字符串）
        if df["dt"].dtype == object:
            df = df.copy()
            df["dt"] = df["dt"].apply(lambda x: datetime.fromisoformat(x))

        # 辅助函数：安全转换为北京时间
        def _to_cn_safe(x):
            if isinstance(x, datetime):
                return to_cn(x)
            return x

        df = df.copy()
        df["_dt_cn"] = df["dt"].apply(_to_cn_safe)

        # 确定 Session 的起止时间范围
        # 开始：Session日期 20:00
        sid_date = datetime.fromisoformat(sid).replace(tzinfo=CN_TZ)
        s_start = sid_date.replace(
            hour=context.window_start_h,
            minute=context.window_start_m,
            second=0,
            microsecond=0,
        )
        # 结束：Session日期+1天 08:00
        s_end = (sid_date + timedelta(days=1)).replace(
            hour=context.window_end_h,
            minute=context.window_end_m,
            second=0,
            microsecond=0,
        )

        # 筛选信号：
        # 1. 在当前 Session 范围内 (s_start <= t < s_end)
        # 2. 不属于“未来数据” (t <= dt_cn)
        df_sess = df[
            (df["_dt_cn"] >= s_start) & (df["_dt_cn"] < s_end) & (df["_dt_cn"] <= dt_cn)
        ]

        if df_sess.empty:
            return  # 无可用信号

        # 取最新时刻的信号（Latest Signal）
        latest_dt = df_sess["_dt_cn"].max()
        # 按 position 排序
        ranker_prediction = df_sess[df_sess["_dt_cn"] == latest_dt].sort_values(
            "position"
        )
    else:
        # === 模式 B：基于日期 (date) 的信号 (兼容旧模式) ===
        # 整个 Session 共用同一天日期的信号
        ranker_prediction = context.my_data[context.my_data["date"] == sid].sort_values(
            "position"
        )
        if ranker_prediction.empty:
            return

    # ---------------------------
    # 5. 执行调仓
    # ---------------------------
    rebalance(context, data, ranker_prediction)

    # ---------------------------
    # 6. 更新状态
    # ---------------------------
    context.last_rebalance_dt_cn = dt_cn
