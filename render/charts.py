"""图表绘制：把 writer 的"图表规格 + 真实数据"画成图。

每个函数对应一种图型（number_cards / bar / line / bar_line /
bubble / timeline / table），输入规格与数据点，输出 matplotlib Figure。
风格统一走 render.style。renderer 再把这些图排进一页通。
"""

from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

from . import style as S


# ⚠ **图的渲染高度只由长宽比决定，与 figsize 的绝对大小无关**（#83 实测）。
# 一页通里图是并排放的，`.chart-row .chart img { width:100% }` 把显示宽度
# 定死在容器宽（约 363px），图画多大都会被缩到这个宽度——
# 所以"把 figsize 整体缩小"对省版面**毫无作用**（#82 那次缩 15% 白做了）。
# 真正管用的是**压扁**：保持宽度、降低高度，长宽比变大，渲染高度按比例下降。
# 各图按**标注密度**分别定压缩幅度，不能一刀切：
#   bar / line / hist_band  只有一组标注 → 压 20%（2.1 → 1.68）
#   bar_line                柱值+线值两组，最挤 → 只压 9%（2.2 → 2.00）
# 实测把 bar_line 也压 20% 时，"21.4"撞上轴标签、柱内值与线值糊成一片——
# 我给标注做的"分区"方案（柱值进柱内、线值在柱顶上方）需要一定纵向余量，
# 压太扁就没地方分了。

def _fmt(v: float) -> str:
    """柱顶/点上标注的数值格式：按量级定小数位。

    统一用 `{v:g}` 会把 22.010912 原样印成 "22.0109"——图上的标注是给人扫一眼的，
    多出来的位数只是噪声，还会挤到相邻柱子上。
    """
    if v is None:
        return ""
    a = abs(v)
    return f"{v:,.0f}" if a >= 100 else (f"{v:,.1f}" if a >= 10 else f"{v:,.2f}")


def number_cards(cards: list[dict], *, title: str | None = None, height: float = 1.9):
    """大数字卡片组。cards=[{label, value, sub?, color?}]。

    value 已是格式化好的可读串（如 "1.46倍"/"54.60%"），照排即可。
    """
    S.apply_style()
    n = max(1, len(cards))
    # 卡片宽度随**最长的值**伸缩。定宽（1.7 英寸）时，"净流入42.74亿元" 这类长值
    # 会溢出卡片边框、与相邻卡片的字叠在一起——实测成品上出现过：
    # 框没框住字、两张卡的数字重叠成一团。宽度与字号都得跟着内容走。
    vmax = max((len(str(c.get("value", ""))) for c in cards), default=4)
    lmax = max((len(str(c.get("label", ""))) for c in cards), default=6)
    per = max(1.5, min(2.6, 0.16 * max(vmax, lmax * 0.8) + 0.9))
    fig, axes = plt.subplots(1, n, figsize=(min(5.4, per * n * 0.85), height * 0.70))
    if n == 1:
        axes = [axes]

    # 值的字号按最长值缩，保证再长也留在框内（下限 11pt，不小于正文观感）
    vsize = 21 if vmax <= 6 else (17 if vmax <= 9 else (14 if vmax <= 12 else 11))

    for ax, c in zip(axes, cards):
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        # 圆角卡片底
        ax.add_patch(FancyBboxPatch(
            (0.04, 0.06), 0.92, 0.88,
            boxstyle="round,pad=0,rounding_size=0.06",
            facecolor=S.CARD, edgecolor=S.GRID, linewidth=1.0,
        ))
        color = c.get("color", S.PRIMARY)
        ax.text(0.5, 0.60, str(c.get("value", "")), ha="center", va="center",
                fontsize=vsize, fontweight="bold", color=color)
        ax.text(0.5, 0.28, str(c.get("label", "")), ha="center", va="center",
                fontsize=9, color=S.INK)
        if c.get("sub"):
            ax.text(0.5, 0.14, str(c["sub"]), ha="center", va="center",
                    fontsize=7.5, color=S.MUTED)

    if title:
        fig.suptitle(title, fontsize=13, fontweight="bold", color=S.INK, y=1.02)
    fig.tight_layout()
    return fig


def table(headers: list[str], rows: list[list], *, title: str | None = None):
    """对比表：深红表头、隔行浅底、细网格。rows 为二维文本。"""
    S.apply_style()
    nrows = len(rows) + 1
    fig, ax = plt.subplots(figsize=(4.4, 0.30 * nrows + 0.30))
    ax.axis("off")
    tbl = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="left")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1, 1.7)
    ncol = len(headers)
    for j in range(ncol):
        h = tbl[0, j]
        h.set_facecolor(S.PRIMARY)
        h.set_text_props(color="white", fontweight="bold")
        h.set_edgecolor("white")
    for i in range(1, nrows):
        for j in range(ncol):
            cell = tbl[i, j]
            cell.set_facecolor(S.CARD if i % 2 else S.SURFACE)
            cell.set_edgecolor(S.GRID)
            cell.set_text_props(color=S.INK)
    if title:
        ax.set_title(title, color=S.INK, fontweight="bold", fontsize=13, pad=12)
    fig.tight_layout()
    return fig


def bar(labels: list[str], values: list[float], *, title: str | None = None,
        ylabel: str | None = None, signed: bool = False):
    """柱状图。signed=True 时按红涨绿跌上色；柱顶直接标值。"""
    S.apply_style()
    fig, ax = plt.subplots(figsize=(min(5.1, max(3.7, 0.68 * len(labels))), 1.68))
    colors = [S.signed_color(v) for v in values] if signed else [S.PRIMARY] * len(values)
    bars = ax.bar(labels, values, color=colors, width=0.6)
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v, _fmt(v), ha="center",
                va="bottom" if v >= 0 else "top", fontsize=9, color=S.INK)
    ax.axhline(0, color=S.GRID, linewidth=1)
    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, fontweight="bold", color=S.INK)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return fig


def _ylabel_top(ax, text: str) -> None:
    """把 Y 轴单位标签**横排放在轴顶**，而不是竖排贴在轴侧。

    「PB(倍)」「换手率(%)」这类标签只有三五个字，竖排后每个字转 90°，
    读起来要歪头，而且占掉一条竖向长条的宽度（图本就只有 4.8 英寸宽）。
    财经图表的通行做法是横排放在纵轴顶端——一眼可读，且几乎不占地方。
    """
    ax.set_ylabel("")
    ax.text(0.0, 1.02, text, transform=ax.transAxes, ha="left", va="bottom",
            fontsize=7.5, color=S.MUTED)


def _pad_axis(ax, values: list[float], frac: float = 0.18,
              include_zero: bool = False) -> None:
    """给坐标轴上下留白，避免柱顶/折线点上的数值标注顶到标题或被裁掉。

    include_zero：柱状轴必须传 True。柱子是从 0 轴长出来的，
    若把下限抬到最小值之上，负值柱看起来会"浮空"，读者以为柱底就是零点——
    这是会误导人的画法，不只是难看。
    """
    if not values:
        return
    lo, hi = min(values), max(values)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = (hi - lo) or (abs(hi) or 1.0)
    ax.set_ylim(lo - span * frac, hi + span * frac)


def line(labels: list[str], values: list[float], *, title: str | None = None,
         ylabel: str | None = None, baseline: float | None = None,
         baseline_label: str | None = None):
    """折线图：讲**趋势**用，数字卡与柱状图都表达不了"一路怎么走过来的"。

    baseline 可画一条参考横线（如荣枯线 50、零轴、历史均值），
    研报里"当前 vs 历史中枢"这类对比全靠它。
    """
    S.apply_style()
    fig, ax = plt.subplots(figsize=(min(5.3, max(3.9, 0.47 * len(labels))), 1.68))
    ax.plot(labels, values, color=S.PRIMARY, linewidth=2.0,
            marker="o", markersize=4, zorder=3)
    ax.fill_between(range(len(values)), values, min(values + [0]),
                    color=S.PRIMARY, alpha=0.08, zorder=1)

    if baseline is not None:
        ax.axhline(baseline, color=S.MUTED, linewidth=1.2, linestyle="--", zorder=2)
        if baseline_label:
            ax.text(len(labels) - 1, baseline, f" {baseline_label}", va="bottom",
                    ha="right", fontsize=8, color=S.MUTED)

    # 只标首尾两个点，中间标满会糊成一片
    for i in (0, len(values) - 1):
        ax.annotate(f"{values[i]:g}", (i, values[i]), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=9, color=S.INK)

    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, fontweight="bold", color=S.INK, pad=12)
    ax.grid(axis="x", visible=False)
    _pad_axis(ax, values + ([baseline] if baseline is not None else []))
    if len(labels) > 6:
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    return fig


def bar_line(labels: list[str], bars: list[float], lines: list[float], *,
             title: str | None = None, bar_label: str = "", line_label: str = "",
             signed: bool = True):
    """双轴柱线图：**背离**类论证的标准画法。

    模板里"股价跌 10~20% 但营收同比 +40~70%"这种量价背离，
    用一张图就能说清——柱子一个方向、折线另一个方向，一眼看出错配。
    单独用柱状图或折线图都表达不了两个量纲不同的序列的对比。
    """
    S.apply_style()
    fig, ax1 = plt.subplots(figsize=(min(5.3, max(4.1, 0.68 * len(labels))), 2.00))
    colors = [S.signed_color(v) for v in bars] if signed else [S.PRIMARY] * len(bars)
    b = ax1.bar(labels, bars, color=colors, width=0.55, zorder=2)
    ax1.axhline(0, color=S.GRID, linewidth=1)

    ax2 = ax1.twinx()
    ax2.plot(labels, lines, color=S.INK, linewidth=1.8, marker="o",
             markersize=4, zorder=3)
    ax2.grid(False)

    # 两轴都留出上下空白：柱顶/折线点上的数值标注否则会顶到标题或被裁掉
    _pad_axis(ax1, bars, include_zero=True)   # 柱状轴须含 0，否则负值柱会浮空
    _pad_axis(ax2, lines, frac=0.22)

    # 柱值进**柱子内部**（白字）：柱身是深色底，对比天然够，
    # 而柱顶以上那块要留给折线与它的标注——两者分区，不争抢同一位置。
    # 柱太矮塞不下字时退回柱顶外侧（此时柱矮，折线通常也不在附近）。
    y0, y1 = ax1.get_ylim()
    span = (y1 - y0) or 1.0
    for rect, v in zip(b, bars):
        x = rect.get_x() + rect.get_width() / 2
        if abs(v) / span > 0.16:
            ax1.text(x, v - (span * 0.05 if v >= 0 else -span * 0.05), _fmt(v),
                     ha="center", va="top" if v >= 0 else "bottom", fontsize=7.5,
                     color="white", fontweight="bold", zorder=5)
        else:
            ax1.text(x, v, _fmt(v), ha="center",
                     va="bottom" if v >= 0 else "top", fontsize=7.5,
                     color=S.INK, zorder=5)

    # 折线只标**首尾两点**，不是每点都标（沿用 `line()` 的成例）。
    # 理由是版面：图并排后只有约 363×177px，柱值 6 个 + 线值 6 个共 12 个标注
    # 挤在里面必然互相压——实测调过三轮偏移方向、加白底、改分区都压不住，
    # 因为空间本身就不够。而两根轴都带刻度，中间点的值读轴即可；
    # 首尾两点标出来是为了给"从哪来、到哪去"一个锚，这正是折线要表达的东西。
    for i in (0, len(lines) - 1):
        v = lines[i]
        ax2.annotate(_fmt(v), (i, v), textcoords="offset points",
                     xytext=(0, 9), ha="center", fontsize=7.5, color=S.INK,
                     zorder=6,
                     bbox=dict(boxstyle="square,pad=0.15", fc=S.SURFACE,
                               ec="none", alpha=0.9))

    # 双轴的单位标签同样横排（同 hist_band 的理由），左轴贴左上、右轴贴右上，
    # 分列两端不会互撞；标题的 pad 相应加大，给这行标签让出位置。
    if bar_label:
        ax1.text(0.0, 1.06, bar_label, transform=ax1.transAxes,
                 ha="left", va="bottom", fontsize=7.5, color=S.MUTED)
    if line_label:
        ax1.text(1.0, 1.06, line_label, transform=ax1.transAxes,
                 ha="right", va="bottom", fontsize=7.5, color=S.MUTED)
    if title:
        ax1.set_title(title, fontweight="bold", color=S.INK, pad=22)
    ax1.grid(axis="x", visible=False)
    if len(labels) > 6:
        plt.setp(ax1.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    return fig


if __name__ == "__main__":  # python -m render.charts  → 输出示例图
    import os

    OUT = os.path.join(os.path.dirname(__file__), "..", "_preview_number_cards.png")
    # 用券商板块 valuation_gap 的真实数据
    fig = number_cards(
        [
            {"label": "归母净利同比", "value": "54.60%", "sub": "2026Q1 · 中信证券", "color": S.UP},
            {"label": "PB", "value": "1.46倍", "sub": "处历史低分位"},
            {"label": "ROE", "value": "11.56%", "sub": "TTM"},
        ],
        title="券商板块盈利与估值关键指标",
    )
    fig.savefig(OUT, dpi=200, bbox_inches="tight")
    print("saved:", os.path.abspath(OUT))


def hist_band(dates: list[str], values: list[float], *, title: str | None = None,
              ylabel: str | None = None, current: float | None = None,
              pctl: float | None = None):
    """历史分位带：整段历史走势 + 当前位置 + 分位参考线。

    专治"分位类字段只能印成一个数字框"。`PB历史分位 = 9.4%分位` 这样一个标量，
    number_cards 只能把 9.4 印进方框，gauge 也只是把它摆到一根横条上——
    **两者都丢掉了"它怎么走到这里的"和"历史上什么形态"**，
    而这恰恰是判断"低分位是常态还是刚跌下来"的关键（同 S6 拥抱度回落的道理）。

    画三样东西：
      - 整段序列的走势线（信息量的主体，726 个点的形状）；
      - 历史 25/50/75 分位横带（读者一眼看出当前落在哪一档）；
      - 当前值的水平线与端点标注。
    """
    import statistics as st

    S.apply_style()
    fig, ax = plt.subplots(figsize=(4.8, 1.68))
    n = len(values)
    xs = list(range(n))

    srt = sorted(values)
    q = [srt[int(len(srt) * f)] for f in (0.25, 0.50, 0.75)]
    ax.axhspan(q[0], q[2], color=S.PRIMARY, alpha=0.06, zorder=1)
    # 分位标注移到**右侧轴外**，并给白底。原先压在左侧的浅色分位带上，
    # 灰字叠浅灰带、又与走势线抢位置，实测糊成一片认不出（#82）。
    # 放轴外既不遮数据，也不必再跟曲线抢地方。
    for v, lab in zip(q, ("25%", "50%", "75%")):
        ax.axhline(v, color=S.MUTED, linewidth=0.7, linestyle=":", zorder=2)
        ax.text(1.008, v, lab, transform=ax.get_yaxis_transform(),
                va="center", ha="left", fontsize=6.5, color=S.MUTED, zorder=6,
                bbox=dict(boxstyle="square,pad=0.12", fc=S.SURFACE, ec="none"))

    ax.plot(xs, values, color=S.PRIMARY, linewidth=1.4, zorder=3)

    cur = current if current is not None else values[-1]
    ax.axhline(cur, color=S.PRIMARY_D, linewidth=1.1, linestyle="--", zorder=4)
    ax.plot([n - 1], [cur], marker="o", markersize=5, color=S.PRIMARY_D, zorder=5)

    # 位数按量级定：PB 要两位小数，成交额几百亿写两位小数纯属噪声
    _d = 0 if abs(cur) >= 100 else (1 if abs(cur) >= 10 else 2)
    lab = f"当前 {cur:,.{_d}f}"
    if pctl is not None:
        lab += f"（{pctl:.1f}%分位）"
    # 标注放**离当前值最远的那个角**，别贴着端点写——贴着写必然压在曲线上
    # （实测当前值在低位时，标注横跨整条走势线，字和线糊成一片看不清）。
    lo, hi = min(values), max(values)
    高位 = (cur - lo) / (hi - lo) > 0.5 if hi > lo else False
    ax.text(0.985, 0.06 if 高位 else 0.94, lab,
            transform=ax.transAxes, ha="right",
            va="bottom" if 高位 else "top",
            fontsize=7.5, fontweight="bold", color=S.INK,
            bbox=dict(boxstyle="round,pad=0.3", fc=S.SURFACE, ec=S.GRID, lw=0.6))

    # x 轴只标首尾与中点：726 个交易日标满必然糊成一片
    ticks = [0, n // 2, n - 1]
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates[i][:7] if i < len(dates) else "" for i in ticks], fontsize=8)
    if ylabel:
        _ylabel_top(ax, ylabel)
    if title:
        ax.set_title(title, fontweight="bold", color=S.INK, pad=14)
    ax.grid(axis="x", visible=False)
    _pad_axis(ax, values + [cur])
    # 右侧留出分位标注的位置（标注放在轴外，不留白会被 tight_layout 裁掉）
    fig.tight_layout(rect=(0, 0, 0.955, 1))
    return fig


def scatter(points: list[dict], *, title: str | None = None,
            xlabel: str = "", ylabel: str = "", highlight: str = ""):
    """散点图：一次看清**一批个体的二维分布**。points=[{标签,x,y}]。

    专治"成分股明细被压成一句话"。板块内 30 只股的估值与盈利，
    文字里只能挑三五只举例（"茅台+1.5%、五粮液+82.6%…"），
    读者既看不出整体形态，也不知道被举例的是不是特例。
    散点一画，贵/便宜、赚钱/亏钱四个象限一目了然，离群值自己跳出来。

    highlight：代表标的的标签，单独描红加名，让读者知道"我们讲的那只在哪"。
    """
    S.apply_style()
    fig, ax = plt.subplots(figsize=(5.4, 2.30))
    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    ax.scatter(xs, ys, s=34, color=S.PRIMARY, alpha=0.55,
               edgecolors="white", linewidths=0.6, zorder=3)

    # 中位线分四象限——没有参照线的散点只是一团点，读者无从判断"贵"与"便宜"
    import statistics as st
    mx, my = st.median(xs), st.median(ys)
    ax.axvline(mx, color=S.MUTED, linewidth=0.9, linestyle=":", zorder=2)
    ax.axhline(my, color=S.MUTED, linewidth=0.9, linestyle=":", zorder=2)

    # 只标注离群者与代表标的：全标必然糊成一片
    def _dist(p):
        return abs(p["x"] - mx) / (max(xs) - min(xs) or 1) + \
               abs(p["y"] - my) / (max(ys) - min(ys) or 1)
    named = sorted(points, key=_dist, reverse=True)[:4]
    if highlight:
        named += [p for p in points if p.get("标签") == highlight]
    seen = set()
    for p in named:
        lb = p.get("标签", "")
        if not lb or lb in seen:
            continue
        seen.add(lb)
        hi = lb == highlight
        if hi:
            ax.scatter([p["x"]], [p["y"]], s=70, color=S.PRIMARY_D,
                       edgecolors="white", linewidths=1.0, zorder=4)
        ax.annotate(lb, (p["x"], p["y"]), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8,
                    fontweight="bold" if hi else "normal",
                    color=S.PRIMARY_D if hi else S.INK)

    if xlabel:
        ax.set_xlabel(xlabel, fontsize=8)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8)
    if title:
        ax.set_title(title, fontweight="bold", color=S.INK, pad=10)
    fig.tight_layout()
    return fig


def grouped_bar(labels: list[str], series: list[dict], *, title: str | None = None,
                ylabel: str | None = None):
    """分组柱：同一批对象的**多个指标并排比**。series=[{名称, 值:[...]}]，最多 3 组。

    宽口径板块最该有的一张——六个子行业各自的 PB/ROE 摆在一起，
    "哪个强哪个拖后腿"一眼可见；拆成三张单指标柱状图反而看不出结构。
    """
    S.apply_style()
    n, m = len(labels), max(1, len(series))
    fig, ax = plt.subplots(figsize=(min(6.4, max(4.8, 0.95 * n)), 2.15))
    width = 0.8 / m
    palette = [S.PRIMARY, S.PRIMARY_D, S.MUTED]
    for i, s in enumerate(series[:3]):
        xs = [j - 0.4 + width * (i + 0.5) for j in range(n)]
        vals = s.get("值") or []
        ax.bar(xs, vals, width=width * 0.9, label=s.get("名称", ""),
               color=palette[i % len(palette)], zorder=2)
        for x, v in zip(xs, vals):
            if v is None:
                continue
            ax.text(x, v, _fmt(v), ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=7.5, color=S.INK)
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, fontsize=7.5)
    ax.axhline(0, color=S.GRID, linewidth=1)
    if m > 1:
        ax.legend(fontsize=8, frameon=False, ncol=min(3, m), loc="upper right")
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8)
    if title:
        ax.set_title(title, fontweight="bold", color=S.INK, pad=10)
    ax.grid(axis="x", visible=False)
    allv = [v for s in series[:3] for v in (s.get("值") or []) if v is not None]
    _pad_axis(ax, allv, include_zero=True)
    fig.tight_layout()
    return fig


def histogram(values: list[float], *, current: float | None = None,
              title: str | None = None, xlabel: str = "", pctl: float | None = None,
              bins: int = 30):
    """直方图：把"处 X% 分位"画成**分布里的位置**。

    仪表条只告诉你分位数字，看不出分布形状——同样是 85% 分位，
    在集中分布里意味着"略高于常态"，在长尾分布里可能意味着"已到极端区"，
    对波动率这种厚尾变量差别极大，而它恰恰是期权定价的核心输入。
    """
    S.apply_style()
    fig, ax = plt.subplots(figsize=(5.2, 1.92))
    ax.hist(values, bins=bins, color=S.PRIMARY, alpha=0.55,
            edgecolor="white", linewidth=0.6, zorder=2)
    cur = current if current is not None else values[-1]
    ax.axvline(cur, color=S.PRIMARY_D, linewidth=1.6, zorder=3)
    lab = f"当前 {cur:,.1f}"
    if pctl is not None:
        lab += f"（{pctl:.1f}%分位）"
    ax.annotate(lab, (cur, ax.get_ylim()[1] * 0.92), ha="center", fontsize=7.5,
                fontweight="bold", color=S.INK,
                bbox=dict(boxstyle="round,pad=0.3", fc=S.SURFACE, ec=S.GRID, lw=0.6))
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=8)
    ax.set_ylabel("交易日数", fontsize=8)
    if title:
        ax.set_title(title, fontweight="bold", color=S.INK, pad=10)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return fig
