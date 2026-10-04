"""Shared Matplotlib presentation for research figures, scoped to each render.

The theme changes no data or metrics and deliberately does not choose a backend.
Use ``with chart_style():`` or decorate a plotting function with ``@chart_style()``.
"""
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from cycler import cycler
import matplotlib as mpl
from matplotlib import dates as mdates, font_manager
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter, PercentFormatter


PALETTE = {
    'bg': '#FAFBFC', 'surface': '#FFFFFF', 'ink': '#233B42', 'muted': '#576C76',
    'grid': '#E3EAED', 'positive': '#137C72', 'negative': '#C16558',
    'benchmark': '#6C7C8C', 'blue': '#467EA6', 'purple': '#8D78A6',
    'gold': '#AC822E', 'missing': '#EDF1F3',
}
DARK_PALETTE = {
    'bg': '#111D24', 'surface': '#14232B', 'ink': '#E4ECEF', 'muted': '#A5B7C0',
    'grid': '#2A3C47', 'positive': '#71D6BB', 'negative': '#E29586',
    'benchmark': '#B1BECC', 'blue': '#81B4D4', 'purple': '#BDA9D6',
    'gold': '#D8BE86', 'missing': '#263B47',
}


@lru_cache(maxsize=1)
def font_families():
    """Choose an installed CJK font without hard-coded machine-specific paths."""
    available = {item.name for item in font_manager.fontManager.ttflist}
    preferences = ('Microsoft YaHei', 'Noto Sans CJK SC', 'Source Han Sans SC',
                   'WenQuanYi Micro Hei', 'PingFang SC', 'Heiti SC', 'SimHei',
                   'Arial Unicode MS')
    return [name for name in preferences if name in available]+['DejaVu Sans']


@contextmanager
def chart_style(theme='light'):
    """Apply a reproducible rc theme and restore the caller's settings on exit."""
    if theme not in {'light', 'dark'}:
        raise ValueError("theme must be 'light' or 'dark'")
    colors = PALETTE if theme=='light' else DARK_PALETTE
    settings = {
        'font.family': font_families(), 'font.sans-serif': font_families(),
        'font.size': 10, 'axes.unicode_minus': False,
        'figure.facecolor': colors['bg'], 'axes.facecolor': colors['surface'],
        'savefig.facecolor': colors['bg'], 'savefig.edgecolor': 'none',
        'text.color': colors['ink'], 'axes.labelcolor': colors['muted'],
        'axes.edgecolor': colors['grid'], 'axes.linewidth': .7,
        'axes.titlelocation': 'left', 'axes.titlesize': 13, 'axes.titleweight': 'semibold',
        'axes.titlepad': 12, 'axes.labelsize': 10, 'axes.labelpad': 9,
        'axes.spines.top': False, 'axes.spines.right': False,
        'axes.spines.left': False, 'axes.spines.bottom': False,
        'axes.axisbelow': True, 'axes.grid': True, 'axes.grid.axis': 'y',
        'grid.color': colors['grid'], 'grid.linewidth': .65, 'grid.linestyle': '-',
        'grid.alpha': 1., 'xtick.color': colors['muted'], 'ytick.color': colors['muted'],
        'xtick.labelsize': 9, 'ytick.labelsize': 9, 'xtick.major.size': 0,
        'ytick.major.size': 0, 'xtick.major.pad': 7, 'ytick.major.pad': 7,
        'legend.frameon': False, 'legend.fontsize': 9, 'legend.handlelength': 2.5,
        'legend.labelspacing': .65, 'legend.columnspacing': 1.8,
        'lines.linewidth': 1.7, 'lines.solid_capstyle': 'round',
        'lines.markersize': 4.5, 'patch.linewidth': .5,
        'axes.prop_cycle': cycler(color=[colors[k] for k in
            ('positive','blue','gold','purple','negative','benchmark')]),
        'figure.dpi': 110, 'savefig.dpi': 200, 'svg.fonttype': 'path', 'pdf.fonttype': 42,
        'path.simplify': False,
    }
    with mpl.rc_context(settings):
        yield colors


def style_axes(ax, *, percent=False, percent_scale=1., dates=False, grid_axis='y'):
    """Minimal axes with optional ratio-aware percentages and concise dates."""
    if grid_axis not in {'x','y','both',None}:
        raise ValueError('invalid grid axis')
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(False, which='both')
    if grid_axis is not None:
        ax.grid(True, axis=grid_axis, color=mpl.rcParams['grid.color'],linewidth=.65)
    ax.tick_params(which='both', length=0, pad=7)
    if percent:
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=percent_scale))
    if dates:
        # One locator per axis: locators retain their associated axis internally.
        locator = mdates.AutoDateLocator(minticks=4,maxticks=8)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    return ax


def money_axis(ax, *, axis='y'):
    formatter = FuncFormatter(lambda value,_: f'{value:,.0f}')
    (ax.yaxis if axis=='y' else ax.xaxis).set_major_formatter(formatter)


def diverging_cmap():
    """Negative coral / neutral paper / positive teal; missing stays distinct."""
    cmap=LinearSegmentedColormap.from_list('quant_returns',
        [PALETTE['negative'],'#F8F9F8',PALETTE['positive']], N=256)
    cmap.set_bad(PALETTE['missing'])
    return cmap


def annotation_color(rgba):
    """Choose black or white using relative luminance of the actual cell fill."""
    channels = [v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgba[:3]]
    lightness = sum(a*b for a,b in zip(channels,(.2126,.7152,.0722)))
    return '#FFFFFF' if 1.05/(lightness+.05) >= (lightness+.05)/.05 else '#000000'


def save_figure(fig, path, *, dpi=200):
    """Save only this figure; lifecycle and output formats remain with caller."""
    path=Path(path)
    fig.savefig(path,dpi=dpi,facecolor=fig.get_facecolor(),bbox_inches='tight',pad_inches=.18)
    return path
