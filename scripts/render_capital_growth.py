"""Render the completed 35-scenario capital comparison without rerunning research."""
from pathlib import Path
import json
import html
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'reports/deep_recovery_20260909'
OUT = ROOT / 'reports/capital_charts_20260910'
OUT.mkdir(parents=True, exist_ok=True)
font = FontProperties(fname='C:/Windows/Fonts/msyh.ttc')
plt.rcParams['font.family'] = font.get_name()
plt.rcParams['axes.unicode_minus'] = False
df = pd.read_csv(SOURCE / 'scenario_comparison.csv').fillna('')
named = {
    'main_1': ('主回测与旧政策', '自动恢复 · 第一次'),
    'main_2': ('主回测与旧政策', '自动恢复 · 第二次'),
    'main_3': ('主回测与旧政策', '自动恢复 · 第三次'),
    'legacy': ('主回测与旧政策', '旧政策 · 永久暂停'),
    'train60': ('独立时间分段', '前 60% 区间'),
    'validation20': ('独立时间分段', '中间 20% 区间'),
    'final20': ('独立时间分段', '最后 20% 区间'),
    'end_exit_sensitivity': ('成本与期末退出', '期末有成本强制退出'),
    'reversed_symbols': ('顺序、截断与行情故障', '反转币种输入顺序'),
    'prefix_2023': ('顺序、截断与行情故障', '截断至 2023-12-21'),
    'prefix_2024': ('顺序、截断与行情故障', '截断至 2024-01-06'),
    'liquidity_quarter': ('顺序、截断与行情故障', '成交量缩减至 25%'),
    'missing_one_percent': ('顺序、截断与行情故障', '随机缺失约 1% 行情'),
    'market_outage_7d': ('顺序、截断与行情故障', '全市场七天无行情'),
}
groups = ['主回测与旧政策', '独立时间分段', '固定滚动窗口', '成本与期末退出', '不同年份独立起跑', '顺序、截断与行情故障']
rows = []
for row in df.to_dict('records'):
    name = row['name']
    if name.startswith('rolling_'):
        group, label = groups[2], f"滚动窗口 {int(name.split('_')[-1]) + 1:02d}"
    elif name.startswith('cost_'):
        group, label = groups[3], f"交易与融资成本 {name.split('_')[1]} 倍"
    elif name.startswith('fresh_start_'):
        group, label = groups[4], f"{name[-4:]} 年初独立起跑"
    else:
        group, label = named[name]
    curve = pd.read_csv(SOURCE / 'runs' / name / 'equity_requested_period.csv')
    row.update(group=group, label=label, pnl=row['final_equity'] - row['initial_capital'],
               curve=[[str(r.timestamp)[:10], round(r.equity, 6)] for r in curve.itertuples()])
    assert abs(row['return_pct'] - row['pnl'] / row['initial_capital'] * 100) < 1e-8
    rows.append(row)
rows.sort(key=lambda row: groups.index(row['group']))
assert len(rows) == 35
pd.DataFrame([{k: v for k, v in row.items() if k != 'curve'} for row in rows]).to_csv(OUT / 'capital_growth.csv', index=False, encoding='utf-8-sig')

def draw(items, path, title):
    fig, ax = plt.subplots(figsize=(14, max(5, len(items) * .48 + 2.6)), dpi=170)
    fig.patch.set_facecolor('#f7f9fc'); ax.set_facecolor('#f7f9fc')
    ax.axvline(10000, color='#607086', linestyle='--', linewidth=1.2)
    ax.set_xlim(6500, 29500)
    labels = []
    prev = None
    for i, row in enumerate(items):
        color = '#137c66' if row['pnl'] >= 0 else '#c34f45'
        ax.plot([10000, row['final_equity']], [i, i], color=color, linewidth=5, solid_capstyle='round')
        ax.scatter([row['final_equity']], [i], color=color, s=32, zorder=3)
        ax.text(25000, i, f"{row['final_equity']:,.2f}  |  {row['return_pct']:+.2f}%", fontsize=9, va='center', color=color)
        labels.append(f"{row['label']}\n{row['start'][:10]} — {row['end'][:10]}")
        if prev is not None and prev != row['group']:
            ax.axhline(i - .5, color='#ccd5df', linewidth=1)
        prev = row['group']
    ax.set_yticks(range(len(items)), labels, fontsize=8)
    ax.invert_yaxis(); ax.tick_params(axis='y', length=0, pad=9)
    ax.set_xticks([10000, 15000, 20000], ['10,000 本金', '15,000', '20,000'])
    ax.set_xlabel('资金（USDT）   ·   绿色：盈利；红色：亏损', fontsize=10)
    ax.set_title(title + '\n每个场景独立以 10,000 USDT 起跑；右侧为期末资金与总涨跌幅', loc='left', fontsize=15, pad=22)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.text(.025, .015, '不同场景时间跨度不同，总涨幅不是年化收益。风险停机后的现金尾段保留；结果不构成实盘准入。', fontsize=9, color='#586779')
    fig.tight_layout(rect=(0, .04, 1, 1))
    fig.savefig(path, facecolor=fig.get_facecolor()); plt.close(fig)

draw(rows, OUT / 'all_scenarios.png', '35 个回测场景 · 本金涨跌全览')
for i, group in enumerate(groups):
    draw([r for r in rows if r['group'] == group], OUT / f'group_{i+1}.png', group)

page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>35 个场景本金涨跌</title><style>
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f4f7fb;color:#172b42}main{max-width:1180px;margin:auto;padding:30px 22px}h1{font-size:30px;margin-bottom:8px}.muted{color:#617186;line-height:1.8}.card{background:white;border:1px solid #dce4ee;border-radius:16px;padding:24px;margin:22px 0}select,button{font:inherit;padding:9px;border:1px solid #bdcad9;border-radius:7px;background:white}label{display:inline-block;margin:8px 16px 8px 0}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:12px 8px;border-bottom:1px solid #e6ecf3;text-align:right}td:first-child,th:first-child{text-align:left}td small{display:block;color:#6b7889;margin-top:5px}button.link{border:0;color:#245ca4;cursor:pointer;padding:0;text-align:left}.up{color:#137c66}.down{color:#c34f45}.track{position:relative;min-width:170px;height:17px;background:#f2f5f9;border-radius:4px}.zero{position:absolute;left:27%;height:100%;border-left:1px dashed #6b7889}.gain{position:absolute;height:9px;top:4px;border-radius:3px}canvas{width:100%;height:320px;display:block}.scroll{overflow:auto}.stats{font-size:22px;line-height:1.7}#info{white-space:pre-line}a{color:#245ca4}footer{font-size:13px;color:#617186}
</style><main><h1>每个回测场景，本金变成了多少？</h1><p class="muted">固定 60 币种 · 各场景初始资金 10,000 USDT · 35 次独立回测<br>统计来自 2026-09-09 冻结回测。这里展示每个测试场景，不把组合本金重复分配给 60 个币种。</p>
<div class="card"><div class="stats">主回测：10,000 → <b>19,972.92 USDT</b>　<span class="up">+99.73%</span></div><p class="muted">该结果于 2024-01-07 风险清算后停止交易，后续为现金尾段。独立窗口、起点和压力测试的时间长度不同，不应仅按总涨幅判断优劣。</p></div>
<div class="card"><h2>逐场景本金变化</h2><label>分组 <select id="group"><option value="">全部 35 个场景</option></select></label><label>排序 <select id="sort"><option value="original">按测试分组</option><option value="high">涨幅由高到低</option><option value="low">涨幅由低到高</option></select></label><p class="muted">点击场景名称查看完整资金曲线。条形虚线对应初始本金；绿色向右为盈利，红色向左为亏损。</p><div class="scroll"><table><thead><tr><th>场景 / 测试区间</th><th>本金变化</th><th>期末 USDT</th><th>盈亏 USDT</th><th>总涨跌幅</th></tr></thead><tbody id="body"></tbody></table></div></div>
<div class="card" id="detail"><h2 id="title"></h2><canvas id="curve"></canvas><p class="muted" id="info"></p><button id="download">下载当前资金曲线 PNG</button></div>
<footer>数据来源：deep_recovery_20260909/scenario_comparison.csv 及各场景 equity_requested_period.csv。总涨幅 =（期末资金 ÷ 10,000 − 1）× 100%。未年化、未汇总为同一账户。故障场景中的前值填充仅是陈旧估值；不能视为缺失期间已得到报价核验。策略保持 paused_revalidation。</footer></main>
<script>const data=__DATA__, groups=__GROUPS__;const money=n=>n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}), signed=n=>(n>=0?'+':'')+money(n);const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const group=document.getElementById('group'),sort=document.getElementById('sort');groups.forEach(g=>{let o=document.createElement('option');o.value=o.textContent=g;group.append(o)});
function table(){let rows=data.filter(r=>!group.value||r.group===group.value);if(sort.value!=='original')rows.sort((a,b)=>sort.value==='high'?b.return_pct-a.return_pct:a.return_pct-b.return_pct);document.getElementById('body').innerHTML=rows.map(r=>{let width=Math.min(68,Math.abs(r.return_pct)*.55),left=r.return_pct>=0?27:27-width,color=r.return_pct>=0?'#137c66':'#c34f45';return `<tr><td><button class="link" data-name="${esc(r.name)}">${esc(r.label)}</button><small>${r.start.slice(0,10)} — ${r.end.slice(0,10)}</small></td><td><div class="track"><span class="zero"></span><span class="gain" style="left:${left}%;width:${width}%;background:${color}"></span></div></td><td>${money(r.final_equity)}</td><td class="${r.pnl>=0?'up':'down'}">${signed(r.pnl)}</td><td class="${r.pnl>=0?'up':'down'}">${signed(r.return_pct)}%</td></tr>`}).join('');document.querySelectorAll('[data-name]').forEach(b=>b.onclick=()=>{show(data.find(r=>r.name===b.dataset.name));document.getElementById('detail').scrollIntoView({behavior:'smooth'})})}let selected=data[0];
function show(r){selected=r;document.getElementById('title').textContent=r.label+'：资金曲线';document.getElementById('info').textContent=`${r.start.slice(0,10)} 至 ${r.end.slice(0,10)} ｜ 初始 10,000.00 USDT → 期末 ${money(r.final_equity)} USDT\n净盈亏 ${signed(r.pnl)} USDT ｜ 总涨跌幅 ${signed(r.return_pct)}% ｜ 最大回撤 ${money(r.max_drawdown_pct)}%\n`+(r.termination?'风险清算：'+r.termination.slice(0,10)+'，此后保留现金尾段。':'没有终止级清算；期间仍可能有健康暂停和开仓限制。');let c=document.getElementById('curve'),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=320;c.width=w*dpr;c.height=h*dpr;let ctx=c.getContext('2d');ctx.scale(dpr,dpr);ctx.fillStyle='white';ctx.fillRect(0,0,w,h);let vals=r.curve.map(v=>v[1]),lo=Math.min(10000,...vals)*.94,hi=Math.max(10000,...vals)*1.06,L=74,R=w-20,T=20,B=h-38,x=i=>L+i/(vals.length-1)*(R-L),y=v=>B-(v-lo)/(hi-lo)*(B-T);ctx.font='12px Microsoft YaHei';for(let j=0;j<=4;j++){let v=lo+(hi-lo)*j/4;ctx.strokeStyle='#e4eaf2';ctx.beginPath();ctx.moveTo(L,y(v));ctx.lineTo(R,y(v));ctx.stroke();ctx.fillStyle='#617186';ctx.fillText(Math.round(v).toLocaleString(),4,y(v)+4)}ctx.setLineDash([5,4]);ctx.strokeStyle='#7c8796';ctx.beginPath();ctx.moveTo(L,y(10000));ctx.lineTo(R,y(10000));ctx.stroke();ctx.setLineDash([]);ctx.strokeStyle=r.pnl>=0?'#137c66':'#c34f45';ctx.lineWidth=2;ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke();ctx.fillStyle='#617186';ctx.fillText(r.curve[0][0],L,h-10);ctx.fillText(r.curve.at(-1)[0],R-78,h-10)}group.onchange=sort.onchange=table;window.onresize=()=>show(selected);document.getElementById('download').onclick=()=>{let a=document.createElement('a');a.download=selected.name+'_equity.png';a.href=document.getElementById('curve').toDataURL();a.click()};table();show(data[0]);</script></html>'''
page = page.replace('__DATA__', json.dumps(rows, ensure_ascii=False).replace('</', '<\\/')).replace('__GROUPS__', json.dumps(groups, ensure_ascii=False))
(OUT / 'index.html').write_text(page, encoding='utf-8')
print(json.dumps({'scenarios':len(rows),'positive':sum(r['pnl']>0 for r in rows),'negative':sum(r['pnl']<0 for r in rows),'output':str(OUT)},ensure_ascii=False))
