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

# 每条逻辑最多渲染几张图。**版面预算的硬约束**，不是审美偏好——
# 实测每条 3 张时全篇 9 张占 1125px，而整页可用高度仅约 1160px。
# 两张能并排共一行（约165px），第三张必然独占一行（约210px）。
# gaps 的「图表体检」与「版面预算」也按这个数算，三处共用一个常量，
# 避免"体检报的不是读者看到的东西"（同 `_effective_type` 那处的教训）。
MAX_CHARTS_PER_LOGIC = 2


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


def _ordered_time_labels(labels: list[str]) -> bool:
    """折线图只接受显式、单调的时间轴，杜绝把指标清单画成“趋势”。"""
    if len(labels) < 2:
        return False
    keys: list[tuple[int, int]] = []
    for raw in labels:
        text = str(raw).strip()
        # 2026-08 / 2026Q3 / 2026年 / FY2026 / 26H1 等常见报告时间标签。
        match = re.fullmatch(r"(?:FY)?(\d{2,4})(?:[-/.年](\d{1,2})月?|Q([1-4])|H([12]))?", text, re.I)
        if not match:
            return False
        year = int(match.group(1))
        if year < 100:
            year += 2000
        period = int(match.group(2) or match.group(3) or (6 if match.group(4) == "1" else 12 if match.group(4) else 0))
        keys.append((year, period))
    return keys == sorted(keys) and len(set(keys)) == len(keys)


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


def _rich(s) -> str:
    """正文富文本：先转义，再把 `**重点**` 渲染成加粗（#82）。

    顺序不能倒——必须**先转义再解析标记**，否则正文里若出现 `<` 会被当成标签。
    此前 `**…**` 原样印在版面上（写作要求里让模型标重点，渲染层却不认），
    读者看到的是一堆星号；或者更糟，模型学乖了干脆不标，重点就全丢了。
    """
    out = _esc(s)
    # 非贪婪，且不允许跨越换行——避免一处漏配对把后面整段吞成粗体
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)


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
    """产出该逻辑全部图表的 HTML（1~2 张并排），无图则空串。

    **上限 2 张，代码硬卡**（#81）：提示词里的"给 1~2 项"是倾向不是保证，
    实测 LLM 会顶格给到 3/3/3，全篇 9 张图占 1125px，而整页可用高度仅约 1160px——
    光图就把版面吃满。两张在版面上并排共一行（约165px），第三张必然独占一行
    （约210px，相当于 580 字正文），是最不划算的一张。
    多出来的直接丢弃而不是渲染出去：一页通的硬约束是"一页"，
    宁可少一张图，也不要让成品变成两页。
    """
    lid = getattr(lc, "逻辑id", "?")
    specs = (getattr(lc, "图表规格列表", None) or [])[:MAX_CHARTS_PER_LOGIC]
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
                # LLM 常把“PB 分位、利润增速、区间涨跌幅”这种横截面指标并排后
                # 画成折线；没有连续时间轴就没有“走势”含义，强制退回柱状图。
                if not _ordered_time_labels(labels) or len(_units([p for p in pts if _num(p.get("值")) is not None])) != 1:
                    CHART_FALLBACKS.append(f"{getattr(lc, '逻辑id', '?')}：line → number_cards（非同量纲时间序列）")
                    cards = [{"label": p.get("标签", ""), "value": p.get("值", ""),
                              "color": S.DOWN if str(p.get("值", "")).startswith("-") else S.PRIMARY}
                             for p in pts]
                    return _img_html(C.number_cards(cards, title=spec.get("标题")))
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
/* ⚠ 思源黑体在 Windows 上的字重陷阱（#82 实测）：
   Adobe 把 Regular/Bold 以外的字重注册成**各自独立的 GDI 家族名**——
     Source Han Sans SC         只含 Regular(400) + Bold(700)
     Source Han Sans SC Medium  是另一个家族
     Source Han Sans SC Heavy   又是另一个家族
   所以 `font-family:"Source Han Sans SC"; font-weight:500` 在这个家族里
   找不到 Medium，浏览器**静默回落 Regular**；写 900 同理只得到 Bold。
   两次"加粗"因此都没生效，看上去仍然细。
   正确做法是按真实家族名引用，并把基础家族列在后面兜底：
   命中专用家族时其自带字重生效，命中不到时由 font-weight 数值作用于基础家族。 */
:root {
  /* 与 OptionHelper Designer 的 design_tokens.py 对齐；以下是唯一的页面色板。 */
  --oh-brand-red: #C8102E;
  --oh-brand-red-deep: #890D26;
  --oh-brand-red-soft: #FBF1F3;
  --oh-paper: #FFFDFB;
  --oh-ground: #F5F1F0;
  --oh-surface: #FFFFFF;
  --oh-ink: #241D20;
  --oh-ink-soft: #44383C;
  --oh-muted: #6E5F63;
  --oh-muted-soft: #75666A;
  --oh-blue-gray: #49647D;
  --oh-blue-gray-soft: #F3F6F8;
  --oh-risk-gold-soft: #FBF7EE;
  --oh-rule: #E9DADC;
  --oh-paper-border: #E7D8DB;
  --oh-red-border-soft: #E5C5CC;
  --oh-red-surface: #FFFAFA;
  --oh-table-border: #DDBCC3;
  --oh-table-head-ink: #59353D;
  --f-fallback: "Noto Sans CJK SC","HarmonyOS Sans SC","Alibaba PuHuiTi","DengXian","等线",sans-serif;
  --f-reg: "Source Han Sans SC", var(--f-fallback);
  --f-med: "Source Han Sans SC Medium","Source Han Sans SC", var(--f-fallback);
  --f-heavy: "Source Han Sans SC Heavy","Source Han Sans SC", var(--f-fallback);
}

* { box-sizing: border-box; }
/* 字体优先可商用（思源黑体/Noto/鸿蒙/普惠体，均 SIL OFL 或官方免费商用），
   本机未装则回落等线；**不列微软雅黑**——它是方正授权给微软的，商用需另行授权，
   而这份东西是要发给客户的。与 render/style.py 的 FONT_STACK 保持同一优先级，
   否则图里的字和正文的字会是两种字体。 */
/* 极淡米黄底（#82）：纯白版面在长文档里发刺眼，暖底更接近纸感；
   外围比页面略深一档，页面才"浮"得起来。图表画布同色（style.SURFACE），
   避免图在米黄页面上呈现为一块块白方块。 */
body { margin:0; background:var(--oh-ground); color:var(--oh-ink);
       font-family:var(--f-reg); }
/* ⚠ 版面宽度必须对齐纸张（#83 实测）：原先 width:820px + padding 32px×2
   在 content-box 下实占 884px，而 A4 在 96dpi 下只有 794px——**宽出 90px**。
   只做 HTML 时看不出来（浏览器可横向滚），一导 PDF 就右边被裁。
   改为 border-box + 794px：padding 含在宽内，正文净宽 794-60=734px。 */
.page { box-sizing:border-box; width:794px; margin:10px auto; background:var(--oh-paper);
        padding:18px 28px; box-shadow:0 2px 12px rgb(44 53 62 / .10); }

/* 打印/导 PDF 时：纸张 A4、零边距（版面自带 padding），去掉屏幕用的投影与外底色 */
@page { size:A4; margin:0; }
@media print {
  body { background:var(--oh-surface); margin:0; }
  .page { margin:0; box-shadow:none; }
}
/* 标题直接顶在最上面：原先上方有一行机构名+日期的页眉，已按要求去掉 */
/* 字重（#82）：思源黑体可用 500/700/900。主标题用 900 拉开层级，
   正文 500 保证长文可读，行内重点 <b> 直接跳到 900 —— 500→900 的
   落差比 500→700 明显得多，重点才真正"跳"出来。 */
h1 { font-size:16px; color:var(--oh-ink); margin:0 0 3px; line-height:1.35;
     font-family:var(--f-heavy); font-weight:700; }
h1 .accent { color:var(--oh-brand-red); }
.sub { font-size:10px; color:var(--oh-muted); margin-bottom:6px; }
/* 正文与核心结论的字号：#80 一并下调，换取更长的论述而版面高度不涨。
   一页通的约束是"一页"，不是"字少"——同样的高度里，小一号字能多容
   约三成内容，而 12px/12.5px 在 820px 宽的版面上仍清晰可读。 */
.concl { background:var(--oh-brand-red-soft); border-left:3px solid var(--oh-brand-red); padding:7px 10px; margin:5px 0 8px; font-size:11px; line-height:1.52; font-family:var(--f-med); font-weight:500; }
.concl .lbl { color:var(--oh-brand-red); font-family:var(--f-heavy); font-weight:700; letter-spacing:2px; margin-right:8px; }
.concl b { font-family:var(--f-heavy); font-weight:700; color:var(--oh-brand-red); }
.logic { margin:12px 0; }
.tag { display:inline-block; background:var(--oh-brand-red); color:var(--oh-surface); font-size:9.5px; padding:1px 7px; border-radius:2px; margin-right:6px; font-weight:700; }
.ltitle { font-size:12px; font-family:var(--f-heavy); font-weight:700; color:var(--oh-ink); }
/* 字重 500 = 思源黑体 Medium。Regular(400) 在小字号下偏细，观感发灰；
   Medium 更接近传统黑体的密度，正文读起来更实。重点由 <b>(700) 承担。 */
.body { font-size:10.5px; line-height:1.52; color:var(--oh-ink-soft); margin:4px 0;
        font-family:var(--f-med); font-weight:500; }
.body b { font-family:var(--f-heavy); font-weight:700; color:var(--oh-brand-red); }
.chart { text-align:center; margin:7px 0; }
/* 宽度由 <img width> 按 CSS_DPI 定死，这里只兜底防溢出 */
.chart img { max-width:100%; height:auto; }
/* 一条逻辑配 2~3 张图时并排显示（而非竖向堆叠），省纵向版面。
   每张图已收窄到正文宽的 53~74%，故用 wrap 而非硬挤一行——
   两张通常并得下，第三张若挤不下会自动换到下一行，不会溢出页面。 */
/* 并排是**省纵向版面**的关键，但此前没真生效：每张图按 CSS_DPI 定死了
   width（396~576px），两张就超出 752px 的内容宽，flex-wrap 把它们全换了行——
   实测每个 chart-row 的图宽合计 828~1440px，无一并排成功，等于一张一行。
   改成 flex 基准宽 + img 跟随容器，两张才真能共一行（各约372px，约原宽77%，
   图内文字仍清晰）；三张时第三张换行，靠 max-width 防止它被拉满整行。 */
.chart-row { display:flex; flex-wrap:wrap; justify-content:center; gap:6px; margin:7px 0;
             align-items:flex-start; }
.chart-row .chart, .chart-row .htmlchart { margin:0; flex:1 1 340px; min-width:0; max-width:400px; }
.chart-row .chart img { width:100%; height:auto; }

/* ---- HTML 原生图表（排版型，不走 matplotlib）---- */
/* 限宽并居中，与 matplotlib 图的显示宽度（约 55~70% 正文宽）保持一致，
   否则排版型图会通栏、数值型图偏窄，同一页里两种图一大一小很割裂 */
.htmlchart { text-align:left; border:1px solid var(--oh-rule); background:var(--oh-surface); padding:10px 12px;
             max-width:560px; margin:0 auto; font-size:12px; }
.c-title { font-size:11px; font-family:var(--f-heavy); font-weight:700; color:var(--oh-brand-red); text-align:center; margin-bottom:7px; }

/* 分位标尺 */
.g-row { margin:10px 0; }
.g-head { display:flex; justify-content:space-between; font-size:12px; color:var(--oh-ink); margin-bottom:4px; }
.g-head .g-val { color:var(--oh-brand-red); font-weight:bold; }
.g-track { position:relative; height:10px; background:var(--oh-rule); border-radius:5px; }
.g-fill { position:absolute; left:0; top:0; height:100%; background:var(--oh-red-border-soft); border-radius:5px 0 0 5px; }
.g-dot { position:absolute; top:-3px; width:4px; height:16px; background:var(--oh-brand-red); border-radius:2px; transform:translateX(-2px); }
.g-thr { position:absolute; top:-2px; width:1px; height:14px; background:var(--oh-muted); }
.g-thrlab { position:absolute; top:14px; font-size:9px; color:var(--oh-muted); transform:translateX(-50%); white-space:nowrap; }
.g-foot { display:flex; justify-content:space-between; font-size:10px; color:var(--oh-muted); margin-top:12px; }

/* 两列对照 */
.tc { display:flex; gap:12px; }
.tc-col { flex:1; border:1px solid var(--oh-rule); padding:8px 10px; }
.tc-col.tc-0 { background:var(--oh-brand-red-soft); }
.tc-col.tc-1 { background:var(--oh-blue-gray-soft); }
.tc-head { font-size:12px; font-weight:bold; color:var(--oh-muted); text-align:center; margin-bottom:6px; }
.tc-col ul { list-style:none; margin:0; padding:0; }
.tc-col li { display:flex; justify-content:space-between; font-size:11.5px; line-height:1.7; }
.tc-col li b { color:var(--oh-brand-red); }
.tc-col.tc-1 li b { color:var(--oh-blue-gray); }
.tc-note { font-size:11.5px; font-weight:bold; color:var(--oh-brand-red); text-align:center; margin-top:8px; }

/* 对比卡片组 */
.cc { display:flex; gap:10px; }
.cc-card { flex:1; border:1px solid var(--oh-rule); background:var(--oh-risk-gold-soft); padding:10px; text-align:center; }
.cc-card.cc-hi { border:1.5px solid var(--oh-brand-red); background:var(--oh-brand-red-soft); }
.cc-h { font-size:13px; font-weight:bold; color:var(--oh-ink); margin-bottom:8px; }
.cc-card ul { list-style:none; margin:0 0 8px; padding:0; }
.cc-card li { font-size:11px; color:var(--oh-muted); line-height:1.7; }
.cc-card.cc-hi li { color:var(--oh-ink); font-weight:bold; }
.cc-cl { font-size:12px; font-weight:bold; color:var(--oh-brand-red); border-top:1px solid var(--oh-rule); padding-top:6px; }
.pool { font-size:11.5px; color:var(--oh-muted); border-top:1px dashed var(--oh-rule); padding-top:7px; margin-top:12px; }
.under { border:1px solid var(--oh-paper-border); border-left:3px solid var(--oh-brand-red-deep); background:var(--oh-red-surface);
         border-radius:3px; padding:7px 10px; margin-top:8px; }
/* 标题与标的身份强制同行，避免“挂钩标的”孤悬一行造成额外高度。 */
.u-line { display:flex; align-items:baseline; gap:10px; min-width:0; }
/* 挂钩标的卡片是页末收束信息：统一 10px，仍保留可读的 1.45 行高。 */
.under .lbl, .u-main, .u-why, .u-struct, .u-note { font-size:10px; line-height:1.45; }
.under .lbl { flex:0 0 auto; font-weight:700; color:var(--oh-brand-red-deep); letter-spacing:1px; white-space:nowrap; }
.u-main { min-width:0; white-space:nowrap; }
.u-main, .u-why { color:var(--oh-ink); }
.u-why { margin-top:1px; }
.u-struct { color:var(--oh-ink); margin-top:2px; }
.u-note { color:var(--oh-muted-soft); margin-top:2px; }
/* 正式报价由 OptionHelper 返回后，独立展示已确认的结构和适配理由；研究层不生成这块。 */
.recommendation { border:1px solid var(--oh-paper-border); background:var(--oh-brand-red-soft);
                  border-left:3px solid var(--oh-brand-red); border-radius:3px; padding:6px 10px;
                  margin-top:7px; font-size:10px; line-height:1.45; color:var(--oh-ink); }
.recommendation .r-label { font-family:var(--f-heavy); font-weight:700; color:var(--oh-brand-red-deep);
                           letter-spacing:1px; margin-right:8px; }
.recommendation .r-name { font-family:var(--f-heavy); font-weight:700; }
.recommendation .r-reason { color:var(--oh-ink-soft); }
/* 参考 0720 成品：报价是正文最末一块，暖灰表头、细边线、末列暗红强调。 */
.quote { margin-top:8px; page-break-inside:avoid; }
.q-heading { text-align:center; color:var(--oh-brand-red-deep); font-family:var(--f-heavy); font-weight:700;
             font-size:11px; letter-spacing:3px; margin:0 0 6px; }
.q-meta { display:flex; justify-content:space-between; align-items:flex-end; gap:8px;
          font-size:9px; color:var(--oh-muted); margin:5px 0 3px; }
.q-group-title { color:var(--oh-table-head-ink); font-family:var(--f-med); font-weight:500; }
.q-table { width:100%; border-collapse:collapse; table-layout:fixed; font-size:9px; line-height:1.35; }
.q-table th, .q-table td { border:1px solid var(--oh-table-border); padding:3px 4px; text-align:center;
                           vertical-align:middle; overflow-wrap:anywhere; }
.q-table th { background:var(--oh-brand-red-soft); color:var(--oh-table-head-ink); font-family:var(--f-heavy); font-weight:700; }
.q-table td { background:var(--oh-red-surface); color:var(--oh-ink-soft); }
.q-table td:first-child { font-family:var(--f-heavy); font-weight:700; }
.q-table td:last-child { color:var(--oh-brand-red-deep); font-family:var(--f-heavy); font-weight:700; }
.q-note { font-size:8.5px; line-height:1.45; color:var(--oh-muted-soft); margin:4px 0 0; text-align:right; }
.src { font-size:10px; color:var(--oh-muted); border-top:1px solid var(--oh-rule); margin-top:8px; padding-top:4px; line-height:1.45; }
.foot { font-size:8.5px; color:var(--oh-muted-soft); border-top:1px solid var(--oh-rule); margin-top:9px; padding-top:4px; line-height:1.45; }
.ft-line { margin-top:3px; }
"""

_CN_NUM = ["一", "二", "三", "四", "五", "六"]

# 核心结论缺失时**明写出来**，而不是渲染成一个空框。空框在成品上看不出是"模型漏写"
# 还是"本来就没有"，隔几天回头看更判断不了；写明缺失才符合"如实标记、绝不代笔"。
_MISSING_CONCL = "（核心结论缺失：撰写与补写两轮均未产出，需人工补写后再交付）"


def _today_cn() -> str:
    """报告日期 = **生成当天**。此前写死"2026年7月"，每份成品都印同一个月份，
    与实际生成时间无关——读者据此判断时效会被误导（行情数据是当天的）。"""
    import datetime as _dt

    d = _dt.date.today()
    return f"{d.year}年{d.month}月{d.day}日"


def build_html(ma, rc, *, org: str = DEFAULT_ORG, date: str = "", oh_result=None) -> str:
    """正文展开"主轴"标记的 2~3 条论点，其余（可选池/自由槽）压成一行补充观察。

    条数与"哪几条算主轴"由 planner 依据触发结果决定（DESIGN §7.2），此处只负责呈现。
    """
    date = date or _today_cn()
    id2plan = {lg.逻辑id: lg for lg in ma.plan.logics}

    def title_of(lc):
        p = id2plan.get(lc.逻辑id)
        # 优先用 writer 自拟的一句话标题（#82）：planner 那个基本是论点库的
        # 类型名（"估值历史低分位"），当标题用等于给读者三个术语。
        # writer 没给时回退到 planner 的，再不行才用 id——保证版面不会空着。
        t = (getattr(lc, "标题", "") or "").strip() or (p.标题 if p else "") or lc.逻辑id
        return t, (p.来源 if p else SRC_SPINE)

    # 论述为空的逻辑不进版面。LLM 偶发只写标题、正文留空（writer 已记入 rc.空缺逻辑），
    # 若照排会在成品里留下"策略逻辑二 ▶"这样的空壳——比少一条逻辑更糟：
    # 版面上像是漏印了，而且核心结论里往往还在引用这条，读者会以为报告残缺。
    # 宁可少一条也不留空壳；终端已有 ⚠ 提示，人可据此决定重跑或补写。
    body = [lc for lc in rc.logics if (lc.论述 or "").strip()]
    spine = [lc for lc in body if title_of(lc)[1] == SRC_SPINE]

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
          <span class="tag">策略逻辑{_CN_NUM[i]}</span><span class="ltitle">{_esc(t)}</span>
          <div class="body">{_rich(lc.论述)}</div>
          {chart_html}
        </div>""")

    # 「补充观察」已按要求删除（#82）：它把可选池逻辑的 `结论` 压成一行印在版面上，
    # 而 `结论` 自 #59 起写的是**给 OptionHelper 的市场含义**（"方向中性、
    # 建议等待波动收敛、若波动率飙升则…"），是产品选择环节的输入、内部口吻，
    # 印在客户版面上既突兀又与正文重复。可选池论点仍在内部底稿的观点包里可查。

    # 「挂钩标的与推荐结构」：标的卡片（为什么是它、代表什么暴露）常驻；
    # 结构与报价只在传入 oh_result 时出现——由 main.py 的 --optionhelper quote
    # 开关控制是否实际调用新版 Skill，失败时 _underlying_block
    # 自动退回旧版"报价由交易台确定"的措辞，不阻断客户版面生成。
    under_html = _underlying_block(ma, rc, oh_result)
    if under_html:
        sections.append(under_html)
    recommendation_html = _recommendation_block(oh_result)
    if recommendation_html:
        sections.append(recommendation_html)
    quote_html = _quote_block(oh_result)
    if quote_html:
        sections.append(quote_html)

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
    <div class="page">
      <h1>场外衍生品投资策略 <span class="accent">—— {ma.plan.主题}</span></h1>
      <div class="sub">策略研究 · {date}</div>
      <div class="concl"><span class="lbl">核心结论</span>{rc.核心结论 or _MISSING_CONCL}</div>
      {''.join(sections)}
      {_footer_block(ma)}
    </div></body></html>"""


def _underlying_block(ma, rc, oh=None) -> str:
    """「挂钩标的与推荐结构」一节；正式 Quote 表由其后的 `_quote_block` 渲染。

    与 #70 不冲突：#70 撤掉的是**内部工作信息**（板块口径、数据代表标的、
    自有数据源），而"建议挂钩哪个标的、为什么"恰恰是模板里印给客户看的内容，
    也是这份成品之前唯一缺的结论落点（"有分析、没有落点"）。

    两类需求在这里汇合，都经 `viewpoint.build`：
      · 板块类   → 分析对象与挂钩标的本就是同一只 ETF，理由取板块选取依据
      · 产业趋势/事件驱动 → 分析对象不可交易，挂钩标的经择优映射而来，
        理由取择优理由（§9.2②）——**这层映射必须写出来**，不允许分析 A 推荐 B
        却对两者关系只字不提（见设计理念）。

    `oh`（`core.optionhelper_bridge.OptionHelperResult`，可为 None）决定结构·报价
    那半怎么呈现：未调用（`oh is None`，默认——`--optionhelper` 未开）或调用失败时，
    退回旧版"报价由交易台确定"的措辞，**不阻断客户版面生成**——OptionHelper 是
    development_only 的外部依赖，它掉线不该拖累主流程。只有明确拿到结构才越过
    §10 那道线：本系统自己既无波动率曲面也无报价，不能替 OptionHelper 断言结构。
    """
    try:
        from core import viewpoint as vp

        pkg = vp.build(ma, rc)
    except Exception:
        return ""
    if not getattr(pkg, "ok", False) or not pkg.标的代码:
        return ""

    # 客户版不再重复正文中的市场情况；这里只保留标的身份、核心状态和选取原因。
    理由 = pkg.标的选择说明 or pkg.挂钩理由 or pkg.板块理由 or ""
    rows = [f'<b>{pkg.标的名称}</b>（{pkg.标的代码}）']
    if pkg.整体方向:
        rows.append(f"整体方向：{pkg.整体方向}")
    if pkg.波动率看法:
        rows.append(pkg.波动率看法)
    head = "　｜　".join(rows)
    body = (f'<div class="u-why"><b>选取原因</b>：{_esc(理由)}</div>' if 理由 else "")

    return (f'<div class="under"><div class="u-line"><span class="lbl">挂钩标的</span>'
            f'<span class="u-main">{head}</span></div>{body}</div>')


def _recommendation_block(oh) -> str:
    """只呈现 OptionHelper 已确认的产品结构与理由，不由研究层补写。"""
    if oh is None or not getattr(oh, "ok", False) or not getattr(oh, "product_name", ""):
        return ""
    product = _esc(str(oh.product_name))
    product_id = _esc(str(oh.product_id)) if getattr(oh, "product_id", "") else ""
    reason = _esc(str(getattr(oh, "reason", "") or ""))
    name = product + (f"（{product_id}）" if product_id else "")
    detail = f'<span class="r-reason">　{reason}</span>' if reason else ""
    partial = (getattr(oh, "coverage_status", "") == "partial")
    warning = "　⚠ 部分计算模块未完成，本页仅供参考。" if partial else ""
    return ('<section class="recommendation"><span class="r-label">推荐结构</span>'
            f'<span class="r-name">{name}</span>{detail}{warning}</section>')


def _quote_block(oh) -> str:
    """渲染最新版 Designer 冻结的变列 Quote 表，不推导、不改写任何报价。"""
    if oh is None or not getattr(oh, "ok", False):
        return ""
    groups = list(getattr(oh, "quote_groups", []) or [])
    if not groups:
        return ""
    rendered: list[str] = []
    for group in groups:
        columns = list(getattr(group, "columns", []) or [])
        rows = list(getattr(group, "rows", []) or [])
        if not columns or not rows:
            continue
        head = "".join(f'<th scope="col">{_esc(column.label)}</th>' for column in columns)
        body_rows = []
        for row in rows:
            cells = "".join(f'<td>{_esc(row.get(column.key, "—"))}</td>' for column in columns)
            body_rows.append(f"<tr>{cells}</tr>")
        date = f'<span>报价日期：{_esc(oh.quote_date)}</span>' if oh.quote_date else ""
        rendered.append(
            '<div class="q-group">'
            f'<div class="q-meta"><span class="q-group-title">{_esc(group.title)}</span>{date}</div>'
            f'<table class="q-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        )
    if not rendered:
        return ""
    note = oh.quote_note or "以上为参考报价，实际以交易台正式报价为准。"
    return ('<section class="quote"><div class="q-heading">推荐结构 · 参考报价</div>'
            + "".join(rendered) + f'<p class="q-note">{_esc(note)}</p></section>')


_DISCLAIMER = (
    "本材料仅为策略研究与信息分享，不构成投资建议或销售要约。"
    "所述场外衍生品结构存在本金损失风险，具体结构条款与报价以交易台正式报价为准。"
    "文中数据与测算基于历史行情与公开信息，历史表现不代表未来。"
    "投资者应在充分理解产品结构与风险收益特征的基础上，"
    "结合自身风险识别能力与风险承受意愿独立决策。投资有风险，入市需谨慎。"
)


def _footer_block(ma) -> str:
    """页脚：数据来源 + 风险提示与免责声明，灰字，对标模板版式。

    ⚠ 与 #70「内部信息撤出版面」不冲突。#70 撤掉的是**内部工作信息**
    （板块口径怎么定的、数据代表标的是谁、宽口径展开了哪几个行业）；
    而"数据来源"是**对第三方内容的署名**，参考模板每一份都印在页脚
    （"数据来源：Wind、国泰海通证券研究"），属于合规要求而非内部信息。
    研报引用同理，仍随成品固化——`sources/` 是用完即清的工作台，
    出处不落进产出，原 PDF 一删引用就断线。
    """
    from core.planner import DOC_FIELD_PREFIX

    srcs = ["iFinD（同花顺）"]
    for name, fv in (ma.field_values or {}).items():
        if not isinstance(name, str) or not name.startswith(DOC_FIELD_PREFIX):
            continue
        if not getattr(fv, "ok", False):
            continue
        cite = f"{fv.source}".strip()
        if cite and cite not in srcs:
            srcs.append(cite)
    return (f'<div class="foot">'
            f'<div class="ft-line">数据来源：{_esc("、".join(srcs))}</div>'
            f'<div class="ft-line">风险提示与免责声明：{_DISCLAIMER}</div>'
            f'</div>')


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
