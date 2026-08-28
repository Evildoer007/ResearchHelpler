"""内部交互复核页：使用 pyecharts/ECharts 展示可溯源的研究数据。

客户版一页通 HTML 已内嵌轻量 ECharts 交互层；本模块保留更完整的分析师复核页，
用于集中复核主题篮子、ETF 候选与历史序列。正式 PDF 仍只打印同一数据的静态回退
图，以保证一页和离线交付。两者共用同一份 ``MarketAnalysis``，不重新取数、不调用
LLM，也不把交互图的结论写回研究报告。

ECharts 运行文件由项目的 ``assets/vendor/echarts.min.js`` 提供，生成的 HTML
通过相对路径引用它，因此断网时仍能在本机直接打开。
"""

from __future__ import annotations

import html
import re
from pathlib import Path


_LOCAL_ECHARTS_HOST = "../assets/vendor/"
_PALETTE = ("#C8102E", "#49647D", "#855E22", "#7E8A99")


def _number(value) -> float | None:
    """只还原用于交互呈现的真实展示值；解析失败绝不补零。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    sign = -1 if text.startswith(("净流出", "流出", "下跌", "减少")) else 1
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) * sign if match else None


def _escape(value) -> str:
    return html.escape(str(value or ""))


def _field_display(ma, name: str) -> str:
    value = (getattr(ma, "field_values", {}) or {}).get(name)
    if value is None or not getattr(value, "ok", False):
        return "—"
    return str(getattr(value, "display", "") or getattr(value, "value", "") or "—")


def _overview_html(ma, rc) -> str:
    """不把混量纲指标硬画图；以数据卡展示本次研究与方向的审计摘要。"""
    theme = str(getattr(ma, "研究主题", "") or getattr(getattr(ma, "plan", None), "主题", "") or "本次研究")
    scope = str(getattr(ma, "研究篮子口径", "") or "见内部底稿")
    basket = [str(item) for item in (getattr(ma, "研究篮子", []) or []) if str(item)]
    cards = [
        ("研究主题", theme),
        ("研究篮子", f"{len(basket)}只" if basket else "未形成可量化篮子"),
        ("市场方向", str(getattr(rc, "推荐方向", "") or "待复核")),
        ("年化波动率", _field_display(ma, "年化波动率")),
        ("波动率历史分位", _field_display(ma, "波动率历史分位")),
        ("区间涨跌幅", _field_display(ma, "板块区间涨跌幅")),
    ]
    card_html = "".join(
        f'<div class="metric"><span>{_escape(label)}</span><b>{_escape(value)}</b></div>'
        for label, value in cards
    )
    roster = "、".join(_escape(item) for item in basket[:12]) or "本次未形成可展示的主题公司篮子"
    if len(basket) > 12:
        roster += "等"
    return f"""
    <header>
      <div class="eyebrow">RESEARCH HELPER · INTERNAL REVIEW</div>
      <h1>{_escape(theme)}｜内部交互复核</h1>
      <p>本页仅用于分析师复核。图表和数据卡均来自本次已验证数据，不重新取数、不产生新结论。</p>
    </header>
    <section class="overview"><h2>本次研究摘要</h2><div class="metrics">{card_html}</div>
      <p class="scope"><b>研究篮子口径：</b>{_escape(scope)}<br><b>主题篮子：</b>{roster}</p>
    </section>
    """


def _constituent_scatter(ma):
    """可用时展示同一成分样本 PB × 净利同比；无同口径样本则不画。"""
    spec = (getattr(ma, "auto_charts", {}) or {}).get("成分股明细") or {}
    points = [point for point in (spec.get("数据点") or [])
              if _number(point.get("x")) is not None and _number(point.get("y")) is not None]
    if len(points) < 5:
        return None
    from pyecharts import options as opts
    from pyecharts.charts import Scatter

    data = [[_number(point.get("x")), _number(point.get("y")), str(point.get("标签") or "")]
            for point in points]
    return (
        Scatter(init_opts=opts.InitOpts(width="100%", height="430px"))
        .add_xaxis([row[0] for row in data])
        .add_yaxis(
            "成分股", [[row[1], row[2]] for row in data],
            label_opts=opts.LabelOpts(is_show=False),
            symbol_size=10,
            itemstyle_opts=opts.ItemStyleOpts(color=_PALETTE[0], opacity=0.65),
        )
        .set_global_opts(
            title_opts=opts.TitleOpts(title=str(spec.get("标题") or "成分股估值与盈利分布"),
                                      subtitle="同一成分样本；悬停查看公司、PB 与净利同比"),
            xaxis_opts=opts.AxisOpts(name=str(spec.get("x轴") or "PB(倍)"), type_="value"),
            yaxis_opts=opts.AxisOpts(name=str(spec.get("y轴") or "净利同比(%)"), type_="value"),
            tooltip_opts=opts.TooltipOpts(
                formatter="公司：{@[2]}<br/>PB：{@[0]} 倍<br/>净利同比：{@[1]}%"),
            toolbox_opts=opts.ToolboxOpts(is_show=True),
        )
    )


def _history_line(ma):
    """仅使用程序自动生成的同字段时间序列；不从 LLM 文本推断趋势。"""
    specs = getattr(ma, "auto_charts", {}) or {}
    spec = next((item for item in specs.values() if item.get("类型") == "hist_band"), None)
    if not spec:
        return None
    points = [(str(point.get("标签") or ""), _number(point.get("值")))
              for point in (spec.get("数据点") or [])]
    points = [(label, value) for label, value in points if label and value is not None]
    if len(points) < 20:
        return None
    from pyecharts import options as opts
    from pyecharts.charts import Line

    # 过长序列交互仍保留全部数据，但标签稀疏显示，由 ECharts 的 dataZoom 浏览。
    return (
        Line(init_opts=opts.InitOpts(width="100%", height="410px"))
        .add_xaxis([label for label, _ in points])
        .add_yaxis(
            str(spec.get("y轴") or "历史序列"), [value for _, value in points],
            is_symbol_show=False, is_smooth=False,
            linestyle_opts=opts.LineStyleOpts(color=_PALETTE[0], width=2),
            areastyle_opts=opts.AreaStyleOpts(opacity=0.08, color=_PALETTE[0]),
        )
        .set_global_opts(
            title_opts=opts.TitleOpts(title=str(spec.get("标题") or "历史序列"),
                                      subtitle="同一字段的历史观测；拖动下方滑块复核区间"),
            xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=25)),
            yaxis_opts=opts.AxisOpts(name=str(spec.get("y轴") or ""), type_="value"),
            datazoom_opts=[opts.DataZoomOpts(type_="slider", range_start=75, range_end=100)],
            tooltip_opts=opts.TooltipOpts(trigger="axis"), toolbox_opts=opts.ToolboxOpts(is_show=True),
        )
    )


def _etf_candidate_scatter(ma):
    """择优路径中以候选自身的波动率与换手率互动比较；缺值候选不伪造。"""
    proposal = getattr(ma, "挂钩择优", None)
    candidates = list(getattr(proposal, "candidates", []) or [])
    rows = []
    for item in candidates:
        metrics = getattr(item, "指标", {}) or {}
        vol, turnover = _number(metrics.get("年化波动率")), _number(metrics.get("换手率"))
        if vol is not None and turnover is not None:
            rows.append((str(getattr(item, "简称", "") or getattr(item, "代码", "")), vol, turnover))
    if len(rows) < 3:
        return None
    from pyecharts import options as opts
    from pyecharts.charts import Scatter

    return (
        Scatter(init_opts=opts.InitOpts(width="100%", height="410px"))
        .add_xaxis([row[1] for row in rows])
        .add_yaxis("ETF候选", [[row[2], row[0]] for row in rows], symbol_size=11,
                   label_opts=opts.LabelOpts(is_show=False),
                   itemstyle_opts=opts.ItemStyleOpts(color=_PALETTE[1], opacity=0.7))
        .set_global_opts(
            title_opts=opts.TitleOpts(title="ETF 候选：波动率与换手率比较",
                                      subtitle="仅展示两项均已取到的候选；悬停查看名称"),
            xaxis_opts=opts.AxisOpts(name="年化波动率(%)", type_="value"),
            yaxis_opts=opts.AxisOpts(name="换手率(%)", type_="value"),
            tooltip_opts=opts.TooltipOpts(formatter="ETF：{@[2]}<br/>年化波动率：{@[0]}%<br/>换手率：{@[1]}%"),
            toolbox_opts=opts.ToolboxOpts(is_show=True),
        )
    )


def render_internal_review(ma, rc, out_path: str | Path) -> str:
    """生成一份离线可打开的交互复核页，返回产物路径。

    当 pyecharts 或本地 ECharts 文件不可用时抛出 RuntimeError，由调用方记录为
    非阻断性产物失败——研究报告、内部底稿和 PDF 不受影响。
    """
    try:
        from pyecharts.charts import Tab
    except ImportError as error:
        raise RuntimeError("未安装 pyecharts，无法生成内部交互复核页") from error

    target = Path(out_path)
    vendor = target.parent.parent / "assets" / "vendor" / "echarts.min.js"
    if not vendor.is_file():
        raise RuntimeError(f"缺少离线 ECharts 资源：{vendor}")
    target.parent.mkdir(parents=True, exist_ok=True)

    tab = Tab(page_title="Research Helper｜内部交互复核")
    scatter = _constituent_scatter(ma)
    if scatter is not None:
        tab.add(scatter, "成分股分布")
    line = _history_line(ma)
    if line is not None:
        tab.add(line, "历史序列")
    candidates = _etf_candidate_scatter(ma)
    if candidates is not None:
        tab.add(candidates, "ETF候选")

    # 即使没有适合的交互图，也输出摘要页；“无数据”本身就是需要复核的信息。
    if not tab._charts:
        from pyecharts import options as opts
        from pyecharts.charts import Bar
        placeholder = (
            Bar(init_opts=opts.InitOpts(width="100%", height="260px"))
            .add_xaxis(["本次可交互数据"])
            .add_yaxis("状态", [0], label_opts=opts.LabelOpts(is_show=False))
            .set_global_opts(title_opts=opts.TitleOpts(title="暂无满足图表规范的同口径交互数据",
                                                        subtitle="请查看上方研究摘要与内部底稿；系统不会用宽行业或混量纲数据替代。"),
                             yaxis_opts=opts.AxisOpts(is_show=False), xaxis_opts=opts.AxisOpts(is_show=False))
        )
        tab.add(placeholder, "数据状态")

    # pyecharts 在创建每个 Chart/Tab 时把 js_host 复制为实例属性；仅修改
    # CurrentConfig.ONLINE_HOST 不会回写这些既有实例，生成页仍会悄悄走 CDN。
    # 因而逐个显式改写，既离线又不污染同一进程随后生成的其它页面。
    tab.js_host = _LOCAL_ECHARTS_HOST
    for chart in tab._charts:
        chart.js_host = _LOCAL_ECHARTS_HOST
    tab.render(str(target))

    page = target.read_text(encoding="utf-8")
    styles = """
    <style>
      body { margin:0; background:#F5F1F0; color:#241D20; font-family:"Source Han Sans SC","Noto Sans CJK SC",sans-serif; }
      header, .overview { max-width:1180px; margin:18px auto 0; background:#FFFDFB; border:1px solid #E7D8DB; padding:18px 24px; }
      .eyebrow { color:#C8102E; font-size:12px; font-weight:700; letter-spacing:1.2px; }
      h1 { font-size:24px; margin:6px 0; } h2 { font-size:16px; margin:0 0 10px; color:#890D26; }
      p { color:#6E5F63; margin:5px 0; line-height:1.55; }
      .metrics { display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:8px; }
      .metric { background:#FBF1F3; border-left:3px solid #C8102E; padding:8px 10px; min-height:58px; }
      .metric span { display:block; color:#6E5F63; font-size:12px; } .metric b { display:block; margin-top:5px; font-size:15px; }
      .scope { margin-top:13px; font-size:13px; } .tab { max-width:1230px; margin:18px auto; background:#FFFDFB; }
      @media (max-width:900px) { .metrics { grid-template-columns:repeat(2,minmax(120px,1fr)); } }
    </style>
    """
    page = page.replace("</head>", styles + "</head>", 1)
    page = page.replace("<body>", "<body>" + _overview_html(ma, rc), 1)
    target.write_text(page, encoding="utf-8")
    return str(target)
