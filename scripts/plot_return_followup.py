"""Native Matplotlib comparison from existing accounts; no resampling or backtest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import PercentFormatter
from matplotlib.transforms import blended_transform_factory
import numpy as np
import pandas as pd

from backtest.plot_style import chart_style, money_axis, save_figure, style_axes

INITIAL = 10_000.
CANDIDATE = 'ensemble_equal_no_trade'
NAMES = ['trend_60','trend_20','trend_120',CANDIDATE,'annual_selected']
LABELS = {'trend_60':'60 日趋势', 'trend_20':'20 日趋势', 'trend_120':'120 日趋势',
          CANDIDATE:'多周期组合', 'annual_selected':'逐年选择'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(report):
    hashes, data, metrics = {}, {}, {}
    comparison=report/'comparison.csv'
    hashes[comparison]=sha(comparison)
    summary=pd.read_csv(comparison)
    for name in NAMES:
        path=report/'runs'/f'continuous__{name}__cost1'/'equity.csv'
        hashes[path]=sha(path)
        frame=pd.read_csv(path,usecols=['timestamp','equity'])
        index=pd.DatetimeIndex(pd.to_datetime(frame.timestamp,utc=True))
        equity=frame.equity.to_numpy(float)
        if (index.has_duplicates or not index.is_monotonic_increasing or
                not np.isfinite(equity).all() or (equity<=0).any()):
            raise ValueError(f'Invalid equity path: {name}')
        if index[0]!=pd.Timestamp('2021-01-01',tz='UTC') or index[-1]!=pd.Timestamp('2026-09-19',tz='UTC'):
            raise ValueError('Unexpected registered comparison interval')
        dd=equity/np.maximum.accumulate(np.r_[INITIAL,equity])[1:]-1
        stats={'end_equity':float(equity[-1]),'return_pct':float((equity[-1]/INITIAL-1)*100),
               'max_drawdown_pct':float(-dd.min()*100),'daily_points':len(frame)}
        row=summary[(summary.name==name)&(summary.window=='continuous')&
                    (summary.cost_multiplier==1)].iloc[0]
        if not (np.isclose(stats['return_pct'],100*row.net_return) and
                np.isclose(stats['max_drawdown_pct'],-100*row.max_drawdown)):
            raise ValueError('Chart and stored metrics disagree')
        data[name]=(index,equity,dd)
        metrics[name]=stats
    return hashes,data,metrics


def render(data, metrics, output, theme):
    with chart_style(theme) as palette:
        colors={CANDIDATE:palette['positive'],'trend_60':palette['benchmark'],
                'trend_20':palette['blue'],'trend_120':palette['gold'],
                'annual_selected':palette['negative']}
        styles={'trend_60':(0,(5,2)),'annual_selected':(0,(2,2))}
        fig,(ax,dd_ax)=plt.subplots(2,1,figsize=(14.5,8.8),sharex=True,
            gridspec_kw={'height_ratios':[1.85,1],'hspace':.28})
        try:
            fig.subplots_adjust(left=.085,right=.78,top=.80,bottom=.13)
            fig.suptitle('趋势策略：资金曲线与回撤',x=.085,y=.972,ha='left',fontsize=21,weight='semibold')
            fig.text(.085,.927,'BTC / ETH  ·  2021.01.01—2026.09.19  ·  初始 10,000 USDT  ·  基准成本 1×',
                     fontsize=10.5,color=palette['muted'])
            handles={}
            for name in [n for n in NAMES if n!=CANDIDATE]+[CANDIDATE]:
                index,equity,dd=data[name]
                line_kw={'color':colors[name], 'linewidth':2.0 if name==CANDIDATE else 1.35,
                         'linestyle':styles.get(name,'-'),'zorder':4 if name==CANDIDATE else 3}
                handles[name],=ax.plot(index,equity,label=LABELS[name],**line_kw)
                dd_ax.plot(index,dd,**line_kw)
                ax.scatter(index[-1],equity[-1],s=18,color=colors[name],zorder=5)
            style_axes(ax)
            style_axes(dd_ax,percent=True)
            money_axis(ax)
            ax.set_title('账户净值',fontsize=12)
            ax.set_ylabel('USDT')
            ax.set_ylim(0,max(v[1].max() for v in data.values())*1.10)
            ax.set_yticks([0,20000,40000,60000])
            ax.axhline(INITIAL,color=palette['benchmark'],lw=.8,linestyle=(0,(3,4)),alpha=.7)
            ax.annotate('初始资金 10,000',xy=(data[NAMES[0]][0][0],INITIAL),xytext=(7,-14),
                        textcoords='offset points',fontsize=8.5,color=palette['muted'])
            dd_ax.set_title('历史回撤',fontsize=12)
            dd_ax.set_ylabel('距此前权益高点')
            dd_ax.set_ylim(min(v[2].min() for v in data.values())*1.10,.025)
            dd_ax.yaxis.set_major_formatter(PercentFormatter(1,decimals=0))
            dd_ax.axhline(0,color=palette['benchmark'],lw=.8,alpha=.65)
            dd_ax.xaxis.set_major_locator(mdates.YearLocator())
            dd_ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
            dd_ax.set_xlabel('日期 / UTC')
            ax.set_xlim(data[NAMES[0]][0][0],data[NAMES[0]][0][-1]+pd.Timedelta(days=20))
            ax.tick_params(labelbottom=False)
            fig.legend([handles[n] for n in NAMES],[LABELS[n] for n in NAMES],
                loc='upper left',bbox_to_anchor=(.079,.90),ncol=5,fontsize=9.5)

            # Move labels, never endpoints; leaders connect to actual daily values.
            ordered=sorted(NAMES,key=lambda n:metrics[n]['end_equity'])
            label_y={}
            previous=-np.inf
            for name in ordered:
                position=max(metrics[name]['end_equity'],previous+4200.)
                label_y[name]=position
                previous=position
            transform=blended_transform_factory(ax.transAxes,ax.transData)
            for name in ordered:
                index,equity,_=data[name]
                ax.annotate(f"{LABELS[name]}  {equity[-1]:,.0f}",
                    xy=(index[-1],equity[-1]),xycoords='data',
                    xytext=(1.045,label_y[name]),textcoords=transform,
                    ha='left',va='center',fontsize=9.5,color=colors[name],annotation_clip=False,
                    arrowprops={'arrowstyle':'-','color':colors[name],'linewidth':.65,
                                'connectionstyle':'angle,angleA=0,angleB=90,rad=0'})
            c=metrics[CANDIDATE]
            fig.text(.795,.368,'多周期组合',fontsize=11,color=palette['positive'],weight='semibold')
            fig.text(.795,.335,f"累计净收益  +{c['return_pct']:.2f}%",fontsize=9.5,color=palette['muted'])
            fig.text(.795,.306,f"最大回撤      {c['max_drawdown_pct']:.2f}%",fontsize=9.5,color=palette['muted'])
            fig.text(.085,.058,'多周期组合：20 / 60 / 120 日等权＋无交易区。五个方案均使用完整每日净值；期末持仓按市价计值。',
                     fontsize=9,color=palette['muted'])
            fig.text(.085,.032,'历史回测，已计入模型交易成本，非实盘。120 日趋势的全期收益与最大回撤仍优于新组合。',
                     fontsize=9,color=palette['muted'])
            stem='return_comparison' if theme=='light' else 'return_comparison_dark'
            paths=[]
            for extension in ('png','svg','pdf'):
                path=output/f'{stem}.{extension}'
                save_figure(fig,path,dpi=220)
                paths.append(str(path))
            return paths
        finally:
            plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=ROOT/'reports/paper_return_followup_spot_20261004')
    parser.add_argument('--theme',choices=['light','dark','both'],default='both')
    args=parser.parse_args()
    report=args.report.resolve()
    output=report/'matplotlib_v3'
    output.mkdir(exist_ok=True)
    hashes,data,metrics=load(report)
    themes=['light','dark'] if args.theme=='both' else [args.theme]
    exports={theme:render(data,metrics,output,theme) for theme in themes}
    if not all(sha(path)==digest for path,digest in hashes.items()):
        raise ValueError('Input changed during rendering')
    evidence={'schema':'native-matplotlib-chart/v1','source_data_unchanged':True,
        'source_hashes':{str(p):v for p,v in hashes.items()},'metrics':metrics,'exports':exports,
        'daily_data_retained':True,'smoothed':False,'renderer_hash':sha(Path(__file__)),
        'theme_hash':sha(ROOT/'backtest/plot_style.py')}
    (output/'validation.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'source_data_unchanged':True},ensure_ascii=False))


if __name__=='__main__':
    main()
