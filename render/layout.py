"""一页通排版：把 ReportContent 拼成单页 HTML（贴合 0720 版式），供 → PDF。

内容模型（writer 的 ReportContent + 真实数据图表）→ 自包含 HTML（图表以 base64 内嵌）。
机构名用中性占位符（不冒用模板里的第三方品牌），由调用方/配置传入。
"""

from __future__ import annotations

import base64
import io
import re

from core.planner import SRC_SPINE

from . import charts as C
from . import style as S

# 机构名占位（勿冒用第三方品牌；正式使用时由本机构名替换）
DEFAULT_ORG = "【机构名称】· 金融衍生品业务"


# 图的显示尺寸与渲染精度必须分开算，否则字号会失控。
#   CSS_DPI：按它换算显示宽度（figsize 英寸 × 90），也决定字在屏幕上多大——
#            matplotlib 的 11pt 在 90dpi 下是 13.75px，与正文 13px 基本齐平。
#   RENDER_DPI：实际渲染精度，取 2 倍以免高分屏发虚。
# 此前只设 dpi=180 且 `<img>` 仅有 max-width，图便按**原始像素**显示：
# 3.8 英寸的柱状图占到 684px（正文宽的 91%），11pt 的字渲染成 27.5px——
# 比 13px 的正文大一倍多，整页看上去像几张海报夹着几行小字。
CSS_DPI = 90
RENDER_DPI = CSS_DPI * 2


def _fig_to_datauri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=RENDER_DPI, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _img_html(fig) -> str:
    """把 figure 转成按 CSS_DPI 定宽的 <img>——宽度写死才能让字号与正文对齐。"""
    w = int(round(fig.get_figwidth() * CSS_DPI))
    return f'<div class="chart"><img src="{_fig_to_datauri(fig)}" width="{w}"></div>'


def _units(pts: list) -> set[str]:
    """一组数据点的单位集合（"16.94倍"→"倍"、"14.2%"→"%"、"2330.79亿元"→"亿元"）。

    用来判断这批数**能不能画进同一根轴**。集合里多于一个单位就不能——
    倍与百分比画成并排柱子，柱高的相对关系毫无意义却看着像有意义。
    """
    import re as _re

    out = set()
    for p in pts:
        s = str((p or {}).get("值", "")).strip()
        m = _re.search(r"(万亿元|亿元|万元|万亿|亿|元|倍|%|个百分点|pct|bp)\s*$", s)
        out.add(m.group(1) if m else "")
    return out


def _num(v) -> float | None:
    """把格式化串还原成数值。解析不出返回 **None**（不是 0）。

    ⚠ 两个坑，都实测踩过：
    ① **不能返回 0 顶替**。曾把 "净流出98.34亿元" 解析失败当成 0，
       画出一张标题写着"资金净流出"、柱子却是 0 的图——图与正文自相矛盾，
       而且图看起来完全正常，比不画图更糟。取不到值就不该画这个点。
    ② **方向词在前缀里**。fetcher 为防方向抄反，把资金流格式化成
       "净流出98.34亿元" 而非 "-98.34亿元"（DESIGN 数据可信度四防线之②），
       故须识别前缀方向词并还原符号，否则流出会被画成流入。
    """
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    sign = 1.0
    for w, k in (("净流出", -1.0), ("流出", -1.0), ("下跌", -1.0), ("减少", -1.0),
                 ("净减持", -1.0), ("净流入", 1.0), ("流入", 1.0), ("上涨", 1.0),
                 ("净增持", 1.0)):
        if s.startswith(w):
            sign, s = k, s[len(w):]
            break
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    val = float(m.group()) * sign
    # 单位归一：正文与其它字段普遍以「亿元」计，万亿要换算，否则同图量级对不上
    if "万亿" in s:
        val *= 10000
    return val


def _esc(s) -> str:
    """转义进 HTML 的文本。图表标签来自 LLM 与研报原文，可能含 < & 等字符。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------- HTML 原生图表 ----------------
# 有一类"图"本质是**文字排版**而非按数值定位的图元：分位标尺、两列对照、对比卡片组。
# 这类用 matplotlib 画是走弯路——文字换行/对齐/字重/局部着色都得手算坐标，
# 且输出位图，放大或转 PDF 会糊。而一页通最终就是 HTML，直接出 HTML 更清晰也更简单。
# 故渲染分两条路：数值型图（bar/line/bar_line）仍走 matplotlib 出 PNG；
# 排版型图走下面这几个函数直接产 HTML 片段。

def _html_gauge(spec: dict, pts: list) -> str | None:
    """分位标尺：一条历史区间横条 + 当前位置标记 + 阈值线。

    为什么需要它：估值分位、波动率分位、拥挤度等 13 条论点，整个论证就是
    "当前值处在历史什么位置"，而 number_cards 只印一个"28.5%"，恰恰把**位置**丢了。
    读者得自行脑补历史区间多宽、离阈值还有多远。
    """
    rows = []
    for p in pts:
        raw = str(p.get("值", "")).strip()
        # 分位只能是裸数字或带 %。若带「倍/元/亿」等量纲，说明 LLM 把**原始值**
        # 当成分位填了进来（如 PB "1.32倍"）——它恰好落在 0~100 内，会画出一根
        # "1.3% 分位"的标尺，图看着正常却在说假话。必须在这里拦掉，不能只靠提示词。
        if re.search(r"[倍元亿万点bp]", raw):
            continue
        q = _num(raw)
        if q is None or not 0 <= q <= 100:
            continue
        thr = _num(p.get("阈值"))
        marks = ""
        if thr is not None and 0 <= thr <= 100:
            marks = (f'<span class="g-thr" style="left:{thr:.1f}%"></span>'
                     f'<span class="g-thrlab" style="left:{thr:.1f}%">阈值{thr:g}%</span>')
        lo, hi = p.get("区间低", ""), p.get("区间高", "")
        actual = p.get("实际值", "")
        rows.append(f"""
        <div class="g-row">
          <div class="g-head"><span class="g-name">{_esc(p.get("标签", ""))}</span>
            <span class="g-val">{q:.1f}% 分位{f'　<b>{_esc(actual)}</b>' if actual else ''}</span></div>
          <div class="g-track">{marks}
            <span class="g-fill" style="width:{q:.1f}%"></span>
            <span class="g-dot" style="left:{q:.1f}%"></span>
          </div>
          <div class="g-foot"><span>{_esc(lo) or "历史最低"}</span><span>{_esc(hi) or "历史最高"}</span></div>
        </div>""")
    if not rows:
        return None
    ttl = f'<div class="c-title">{_esc(spec.get("标题", ""))}</div>' if spec.get("标题") else ""
    return f'<div class="htmlchart">{ttl}{"".join(rows)}</div>'


def _html_two_col(spec: dict, pts: list) -> str | None:
    """两列对照表：两组「名称 + 数值」并排，配色区分，底部一句结论。

    用于同业估值折溢价、中外对标、历次泡沫顶 vs 当前等"两组横向比"的论证。
    数据点用 `组` 字段分列；只有一组时退化为单列。
    """
    groups: dict[str, list] = {}
    for p in pts:
        groups.setdefault(str(p.get("组", "")).strip() or "本组", []).append(p)
    if not groups:
        return None
    cols = []
    for i, (name, items) in enumerate(list(groups.items())[:2]):
        li = "".join(f'<li><span>{_esc(p.get("标签", ""))}</span>'
                     f'<b>{_esc(p.get("值", ""))}</b></li>' for p in items)
        cols.append(f'<div class="tc-col tc-{i}"><div class="tc-head">{_esc(name)}</div>'
                    f'<ul>{li}</ul></div>')
    ttl = f'<div class="c-title">{_esc(spec.get("标题", ""))}</div>' if spec.get("标题") else ""
    note = (f'<div class="tc-note">{_esc(spec.get("说明"))}</div>'
            if spec.get("说明") else "")
    return f'<div class="htmlchart">{ttl}<div class="tc">{"".join(cols)}</div>{note}</div>'


def _html_card_compare(spec: dict, pts: list) -> str | None:
    """对比卡片组：N 个并列卡片 = 标题 + 若干要点行 + 底部结论，可高亮其一。

    用于"本轮 vs 历次"这类多情景对比（如 2015互联网+ / 2020新能源 / 本轮AI）。
    """
    cards = []
    for p in pts[:4]:
        pts_li = "".join(f"<li>{_esc(x)}</li>" for x in (p.get("要点") or []))
        concl = (f'<div class="cc-cl">{_esc(p.get("结论"))}</div>'
                 if p.get("结论") else "")
        hi = " cc-hi" if p.get("高亮") else ""
        cards.append(f'<div class="cc-card{hi}"><div class="cc-h">'
                     f'{_esc(p.get("标签", ""))}</div><ul>{pts_li}</ul>{concl}</div>')
    if not cards:
        return None
    ttl = f'<div class="c-title">{_esc(spec.get("标题", ""))}</div>' if spec.get("标题") else ""
    return f'<div class="htmlchart">{ttl}<div class="cc">{"".join(cards)}</div></div>'


_HTML_CHARTS = {"gauge": _html_gauge, "two_col": _html_two_col,
                "card_compare": _html_card_compare}


#  排版型图渲染失败的记录，供缺口清单交代"这条为什么没有图"。
#  静默失败是最难查的一类问题：图凭空消失，成品上看不出、日志里也没有。
CHART_FALLBACKS: list[str] = []


def _one_chart(spec: dict, logic_id: str) -> str:
    """渲染单张图（排版型出 HTML，数值型出 <img>），失败回退到 bar。无数据返回空串。"""
    pts = spec.get("数据点") or []
    fn = _HTML_CHARTS.get(spec.get("类型") or "")
    if fn and pts:
        try:
            html = fn(spec, pts)
        except Exception as e:
            html, err = None, f"{type(e).__name__}: {e}"
        else:
            err = "数据点不符合该图型要求"
        if html:
            return f'<div class="chart">{html}</div>'
        # **回退到数值型图，而不是直接放弃。** gauge 对数据点要求严
        # （值须是 0~100 的百分位、不得带量纲），writer 填错就整张图没了；
        # 而这些数据点本身是真实的，画成柱状图照样能用。
        # 宁可图型退一档，也不要让一条逻辑光秃秃没有图。
        CHART_FALLBACKS.append(f"{logic_id}：{spec.get('类型')} → bar（{err}）")
        spec2 = dict(spec, 类型="bar")
        return _chart_for(type("X", (), {"图表规格": spec2})()) or ""
    return _chart_for(type("X", (), {"图表规格": spec})()) or ""


def _chart_block(lc) -> str:
    """产出该逻辑**全部**图表的 HTML（1~3 张并排），无图则空串。

    一条逻辑最多 3 张图，来自 writer 的 `图表规格列表`——不再固定一张。
    并排而非竖排：每张图已按显示宽度收窄到正文宽的 53~74%（#59），
    两三张并排仍在一页宽度内，且比竖向堆叠更省纵向版面（一页通版面寸土寸金）。
    """
    lid = getattr(lc, "逻辑id", "?")
    specs = (getattr(lc, "图表规格列表", None) or [])[:3]
    blocks = [b for b in (_one_chart(s or {}, lid) for s in specs) if b]
    if not blocks:
        return ""
    if len(blocks) == 1:
        return blocks[0]
    return f'<div class="chart-row">{"".join(blocks)}</div>'


def _chart_for(lc) -> str | None:
    """按图表规格 + 数据点渲染该逻辑的图，返回完整 <img> 块；无数据则 None。"""
    spec = lc.图表规格 or {}
    pts = spec.get("数据点") or []
    if not pts:
        return None
    typ = spec.get("类型") or ""
    try:
        if typ == "number_cards":
            # **机械升级：能画成柱就不画数字卡。**
            # 数字卡是全部图型里信息量最低的一种——它只是把数字换个字体印一遍，
            # 既不表达比较也不表达趋势。而实测模型明明拿到一组同量纲可比的数
            # （各子行业 ROE、各成分股净利同比）仍会选它，提示词调了两轮没用（#65/#67）。
            # 与其反复劝模型，不如在渲染层兜底：≥3 个点且都能解析成数值 → 一律改画柱状图。
            # 放在这里而不是提示词里，是因为这一层的保证与模型怎么想无关（#71）。
            nums = [(p.get("标签", ""), _num(p.get("值"))) for p in pts]
            nums = [r for r in nums if r[1] is not None]
            # ⚠ 只在**量纲一致**时才升级。数字卡的本职就是并排放一组
            # 互不同量纲的孤立指标，无条件改成柱状图等于凭空断言它们可比——
            # 实测把「当前PE 16.9倍、FY1预测PE 14.5倍、FY1折价 14.2%」画成一组柱子，
            # 倍与百分比同轴，柱高看着可比实则是两回事，属于"图对、含义错"。
            if len(nums) >= 3 and len(nums) == len(pts) and len(_units(pts)) == 1:
                return _img_html(C.bar([r[0] for r in nums], [r[1] for r in nums],
                                       title=spec.get("标题"), signed=True))
            cards = [{"label": p.get("标签", ""), "value": p.get("值", ""),
                      "color": S.DOWN if str(p.get("值", "")).startswith("-") else S.PRIMARY}
                     for p in pts]
            return _img_html(C.number_cards(cards))
        if typ == "scatter":
            ps = [{"标签": str(p.get("标签", "")), "x": _num(p.get("x")), "y": _num(p.get("y"))}
                  for p in pts]
            ps = [p for p in ps if p["x"] is not None and p["y"] is not None]
            if len(ps) < 5:
                return None
            return _img_html(C.scatter(ps, title=spec.get("标题"),
                                       xlabel=spec.get("x轴", ""), ylabel=spec.get("y轴", ""),
                                       highlight=spec.get("高亮", "")))
        if typ == "grouped_bar":
            series = []
            for s in spec.get("系列") or []:
                vals = [_num(v) for v in (s.get("值") or [])]
                if any(v is not None for v in vals):
                    series.append({"名称": s.get("名称", ""), "值": vals})
            labels = [str(p.get("标签", "")) for p in pts]
            if not series or not labels:
                return None
            return _img_html(C.grouped_bar(labels, series, title=spec.get("标题"),
                                           ylabel=spec.get("y轴")))
        if typ == "histogram":
            vals = [_num(p.get("值")) for p in pts]
            vals = [v for v in vals if v is not None]
            if len(vals) < 30:
                return None
            return _img_html(C.histogram(vals, current=_num(spec.get("当前值")),
                                         pctl=_num(spec.get("分位")),
                                         title=spec.get("标题"), xlabel=spec.get("x轴", "")))
        if typ == "table":
            rows = [[p.get("标签", ""), p.get("值", "")] for p in pts]
            return _img_html(C.table(["指标", "数值"], rows))
        if typ == "hist_band":
            rows = [(str(p.get("标签", "")), _num(p.get("值"))) for p in pts]
            rows = [r for r in rows if r[1] is not None]
            if len(rows) < 20:              # 点太少画不出"历史形态"，不如不画
                return None
            return _img_html(C.hist_band(
                [r[0] for r in rows], [r[1] for r in rows],
                title=spec.get("标题"), ylabel=spec.get("y轴"),
                current=_num(spec.get("当前值")), pctl=_num(spec.get("分位"))))
        if typ in ("bar", "line", "bar_line"):
            # 解析不出数值的点**整点丢弃**，绝不用 0 顶替（见 _num 的说明）
            rows = [(p.get("标签", ""), _num(p.get("值")), _num(p.get("值2")))
                    for p in pts]
            rows = [r for r in rows if r[1] is not None]
            if not rows:
                return None
            labels = [r[0] for r in rows]
            vals = [r[1] for r in rows]

            if typ == "line":
                return _img_html(C.line(labels, vals, title=spec.get("标题")))
            if typ == "bar_line" and all(r[2] is not None for r in rows):
                return _img_html(C.bar_line(
                    labels, vals, [r[2] for r in rows], title=spec.get("标题"),
                    bar_label=spec.get("柱标签", ""), line_label=spec.get("线标签", "")))
            # bar，以及 bar_line 缺第二列时的退化
            return _img_html(C.bar(labels, vals, signed=True,
                                         title=spec.get("标题")))
    except Exception:
        return None
    return None  # bubble/timeline 等待补


_CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#f0eeec; font-family:"Microsoft YaHei","微软雅黑",sans-serif; color:#2B2B2B; }
.page { width:820px; margin:16px auto; background:#fff; padding:28px 34px; box-shadow:0 2px 12px rgba(0,0,0,.12); }
.top { display:flex; justify-content:space-between; font-size:12px; color:#8A8A8A; border-bottom:1px solid #E6E3E1; padding-bottom:8px; }
h1 { font-size:22px; color:#2B2B2B; margin:14px 0 4px; }
h1 .accent { color:#A32C2C; }
.sub { font-size:12px; color:#8A8A8A; margin-bottom:14px; }
.concl { background:#F8EFEE; border-left:4px solid #A32C2C; padding:12px 14px; margin:10px 0 20px; font-size:13.5px; line-height:1.7; }
.concl .lbl { color:#A32C2C; font-weight:bold; letter-spacing:2px; margin-right:8px; }
.logic { margin:18px 0; }
.tag { display:inline-block; background:#A32C2C; color:#fff; font-size:12px; padding:2px 10px; border-radius:2px; margin-right:8px; }
.ltitle { font-size:16px; font-weight:bold; color:#2B2B2B; }
.body { font-size:13px; line-height:1.75; color:#444; margin:8px 0; }
.chart { text-align:center; margin:10px 0; }
/* 宽度由 <img width> 按 CSS_DPI 定死，这里只兜底防溢出 */
.chart img { max-width:100%; height:auto; }
/* 一条逻辑配 2~3 张图时并排显示（而非竖向堆叠），省纵向版面。
   每张图已收窄到正文宽的 53~74%，故用 wrap 而非硬挤一行——
   两张通常并得下，第三张若挤不下会自动换到下一行，不会溢出页面。 */
.chart-row { display:flex; flex-wrap:wrap; justify-content:center; gap:8px; margin:10px 0; }
.chart-row .chart, .chart-row .htmlchart { margin:0; flex:0 1 auto; }

/* ---- HTML 原生图表（排版型，不走 matplotlib）---- */
/* 限宽并居中，与 matplotlib 图的显示宽度（约 55~70% 正文宽）保持一致，
   否则排版型图会通栏、数值型图偏窄，同一页里两种图一大一小很割裂 */
.htmlchart { text-align:left; border:1px solid #E6E3E1; background:#FCFAF9; padding:10px 12px;
             max-width:560px; margin:0 auto; font-size:12px; }
.c-title { font-size:13px; font-weight:bold; color:#A32C2C; text-align:center; margin-bottom:10px; }

/* 分位标尺 */
.g-row { margin:10px 0; }
.g-head { display:flex; justify-content:space-between; font-size:12px; color:#2B2B2B; margin-bottom:4px; }
.g-head .g-val { color:#A32C2C; font-weight:bold; }
.g-track { position:relative; height:10px; background:#EDE8E6; border-radius:5px; }
.g-fill { position:absolute; left:0; top:0; height:100%; background:#D9BFBF; border-radius:5px 0 0 5px; }
.g-dot { position:absolute; top:-3px; width:4px; height:16px; background:#A32C2C; border-radius:2px; transform:translateX(-2px); }
.g-thr { position:absolute; top:-2px; width:1px; height:14px; background:#8A8A8A; }
.g-thrlab { position:absolute; top:14px; font-size:9px; color:#8A8A8A; transform:translateX(-50%); white-space:nowrap; }
.g-foot { display:flex; justify-content:space-between; font-size:10px; color:#8A8A8A; margin-top:12px; }

/* 两列对照 */
.tc { display:flex; gap:12px; }
.tc-col { flex:1; border:1px solid #E6E3E1; padding:8px 10px; }
.tc-col.tc-0 { background:#FBF3F2; }
.tc-col.tc-1 { background:#F1F7F3; }
.tc-head { font-size:12px; font-weight:bold; color:#6f6f6f; text-align:center; margin-bottom:6px; }
.tc-col ul { list-style:none; margin:0; padding:0; }
.tc-col li { display:flex; justify-content:space-between; font-size:12px; line-height:1.9; }
.tc-col li b { color:#A32C2C; }
.tc-col.tc-1 li b { color:#2E8B57; }
.tc-note { font-size:11.5px; font-weight:bold; color:#A32C2C; text-align:center; margin-top:8px; }

/* 对比卡片组 */
.cc { display:flex; gap:10px; }
.cc-card { flex:1; border:1px solid #E6E3E1; background:#F6F2F0; padding:10px; text-align:center; }
.cc-card.cc-hi { border:1.5px solid #A32C2C; background:#FBF3F2; }
.cc-h { font-size:13px; font-weight:bold; color:#2B2B2B; margin-bottom:8px; }
.cc-card ul { list-style:none; margin:0 0 8px; padding:0; }
.cc-card li { font-size:11.5px; color:#6f6f6f; line-height:1.9; }
.cc-card.cc-hi li { color:#2B2B2B; font-weight:bold; }
.cc-cl { font-size:12px; font-weight:bold; color:#A32C2C; border-top:1px solid #E6E3E1; padding-top:6px; }
.pool { font-size:12px; color:#8A8A8A; border-top:1px dashed #E6E3E1; padding-top:8px; margin-top:14px; }
.src { font-size:11px; color:#6f6f6f; border-top:1px solid #E6E3E1; margin-top:18px; padding-top:8px; line-height:1.7; }
.foot { font-size:11px; color:#9a9a9a; border-top:1px solid #E6E3E1; margin-top:20px; padding-top:8px; line-height:1.6; }
"""

_CN_NUM = ["一", "二", "三", "四", "五", "六"]

# 核心结论缺失时**明写出来**，而不是渲染成一个空框。空框在成品上看不出是"模型漏写"
# 还是"本来就没有"，隔几天回头看更判断不了；写明缺失才符合"如实标记、绝不代笔"。
_MISSING_CONCL = "（核心结论缺失：撰写与补写两轮均未产出，需人工补写后再交付）"


def build_html(ma, rc, *, org: str = DEFAULT_ORG, date: str = "2026年7月") -> str:
    """正文展开"主轴"标记的 2~3 条论点，其余（可选池/自由槽）压成一行补充观察。

    条数与"哪几条算主轴"由 planner 依据触发结果决定（DESIGN §7.2），此处只负责呈现。
    """
    id2plan = {lg.逻辑id: lg for lg in ma.plan.logics}

    def title_of(lc):
        p = id2plan.get(lc.逻辑id)
        return (p.标题 if p else lc.逻辑id), (p.来源 if p else SRC_SPINE)

    # 论述为空的逻辑不进版面。LLM 偶发只写标题、正文留空（writer 已记入 rc.空缺逻辑），
    # 若照排会在成品里留下"策略逻辑二 ▶"这样的空壳——比少一条逻辑更糟：
    # 版面上像是漏印了，而且核心结论里往往还在引用这条，读者会以为报告残缺。
    # 宁可少一条也不留空壳；终端已有 ⚠ 提示，人可据此决定重跑或补写。
    body = [lc for lc in rc.logics if (lc.论述 or "").strip()]
    spine = [lc for lc in body if title_of(lc)[1] == SRC_SPINE]
    pool = [lc for lc in body if title_of(lc)[1] != SRC_SPINE]

    sections = []
    for i, lc in enumerate(spine):
        t, _ = title_of(lc)
        chart_html = _chart_block(lc)
        # `lc.结论` **不再印进成品**：实测它与正文最后一句高度重复
        # （正文已写"高毛利红利期或正走向终结"，加粗句再说一遍"红利期或退却"），
        # 读者读到的是同一句话说两遍。但它本身不删——改作**交给 OptionHelper 的输入**，
        # 由它把每条逻辑的落点汇成观点包（见 §10）。
        sections.append(f"""
        <div class="logic">
          <span class="tag">策略逻辑{_CN_NUM[i]}</span><span class="ltitle">{t}</span>
          <div class="body">{lc.论述}</div>
          {chart_html}
        </div>""")

    pool_html = ""
    if pool:
        items = "；".join(f"{title_of(lc)[0]}（{lc.结论.replace('**','')}）" for lc in pool)
        pool_html = f'<div class="pool">补充观察：{items}</div>'

    sources_html = _sources_block(ma)

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
    <div class="page">
      <div class="top"><span>{org}</span><span>策略研究 · {date}</span></div>
      <h1>场外衍生品投资策略 <span class="accent">—— {ma.plan.主题}</span></h1>
      <div class="sub">策略研究 · {date}</div>
      <div class="concl"><span class="lbl">核心结论</span>{rc.核心结论 or _MISSING_CONCL}</div>
      {''.join(sections)}
      {pool_html}
      {sources_html}
      <div class="foot">本材料仅为策略研究与信息分享，不构成投资建议或销售要约；
      示例数据与测算不代表未来表现。投资有风险，入市需谨慎。</div>
    </div></body></html>"""


def _sources_block(ma) -> str:
    """成品上只保留**第三方研报出处**，其余口径信息移到内部底稿（#70）。

    分析对象、板块口径、选取依据、自有数据源都是**内部工作信息**。
    一页通是发给客户的成品，不该把"我们怎么选的板块""数据是谁家的"摆在客户版面上。
    这些已全部移进 `output/*_内部底稿.md`，内部照样可查，而且比印在版面上更完整。

    研报出处**留在成品上**：它不是内部信息，是对第三方内容的署名。
    正文引用了别家研报的数字，出处就该跟着成品走——既是合规要求，
    也因为 `sources/` 是用完即清的工作台，出处不随成品固化，原 PDF 一删就断线。
    本次没用到研报时整块不渲染。
    """
    from core.planner import DOC_FIELD_PREFIX

    docs_used = []
    for name, fv in (ma.field_values or {}).items():
        if not name.startswith(DOC_FIELD_PREFIX) or not getattr(fv, "ok", False):
            continue
        cite = f"{fv.source}"
        if fv.note:
            cite += f" {fv.note}"
        if cite not in docs_used:
            docs_used.append(cite)
    if not docs_used:
        return ""
    lines = ["研报引用："] + [f"　· {c}" for c in docs_used]
    return '<div class="src">' + "<br>".join(lines) + "</div>"


def render_html_file(topic: str, topic_type: str, rep_code: str, out_path: str) -> str:
    from core import pipeline, writer
    ma = pipeline.run(topic, topic_type, rep_code)
    rc = writer.write(ma)
    html = build_html(ma, rc)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


if __name__ == "__main__":  # python -m render.layout
    from core import genres as gr
    p = render_html_file("券商板块投资机会", gr.TYPE_SECTOR, "600030.SH", "_preview_onepager.html")
    print("saved:", p)
