"""内部底稿：与一页通并排产出的**唯一一份内部文档**。

## 为什么要落盘

这些信息原先只在终端打印一次，窗口一关就没了。后果是整条"人在环里"的路径断在第一步——
你想补数据，但不知道缺什么；想复核可疑数字，但不记得是哪几个。
等隔几天真有空回头看时，终端早就关了。

## 为什么不写进一页通

一页通是要发给客户的成品。两类东西不该出现在客户版面上：
  - 内部备忘（"缺渗透率数据""F1逻辑漏写"）——显然不合适；
  - **工作口径**（分析对象怎么选的、板块由哪几个行业合并、数据来自谁家）——
    #70 之前这些印在成品页脚，其实同样是内部信息。客户要看的是结论与论据，
    不是我们的取数过程。故一并移到这里。

例外是**第三方研报出处**：那不是内部信息而是对别家内容的署名，
仍留在成品上（见 `layout._sources_block`），这里也留一份便于回溯。

## 装什么

一份文档装齐，不再拆成多个文件让人对着看：
  - **本次分析对象与口径**（板块、代表标的、宽口径展开、选取依据、数据源）
  - **给 OptionHelper 的观点包**（下游做产品选择的输入，需要人过一眼再用）
  - 自动取数失败的字段**及原因**（区分接口故障与板块名匹配不上——处置完全不同）
  - 需求里提到、但数据源结构性没有的外部事实（这类只能人工找研报）
  - writer 漏写的逻辑（成品里已被剔除，需重跑或补写）
  - validator 标记的可疑数字
  - 本次用到的研报出处
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path


def _fmt_gap_reason(fv) -> str:
    note = (getattr(fv, "note", "") or "").strip()
    if not note:
        return "原因未记录"
    return note


# 缺口原因 → 补救路径。判据取自 fetcher 写进 note 的固定措辞。
# 分三类是因为它们要找的人完全不同：重跑（分析师自己）、改代码（开发）、
# 填数（分析师，且现在有覆盖文件可填了）。混成一张表时，
# 整节标题写着"需要人工补充的数据"，实际上大半条目根本不该人来管。
_GAP_RERUN = ("取数失败", "ConnectionError", "Timeout", "返回空值", "样本不足")
_GAP_DEV = ("映射未就绪", "未匹配到板块", "暂未实现", "未在 schema 定义", "未提供板块名")


def _gap_kind(note: str) -> str:
    n = note or ""
    if any(k in n for k in _GAP_DEV):
        return "dev"
    if any(k in n for k in _GAP_RERUN):
        return "rerun"
    return "manual"


def _gap_sections(ma) -> list[str]:
    """自动取数失败，按补救路径分成三节。"""
    gaps = ma.gap_fields
    if not gaps:
        return ["### 自动取数失败", "", "无——本次所需字段全部取到。", ""]

    buckets: dict[str, list[tuple[str, str]]] = {"rerun": [], "dev": [], "manual": []}
    for f in gaps:
        reason = _fmt_gap_reason(ma.field_values.get(f))
        buckets[_gap_kind(reason)].append((f, reason))

    titles = {
        "rerun": ("重跑即可（接口临时故障 / 序列样本不足）",
                  "> 这类不需要任何人工数据，重跑一次通常就好；连续多次失败再排查取数链路。"),
        "dev": ("需开发处理（映射未就绪 / 板块名对不上数据源）",
                "> 这类**不是分析师的活**：需要补 `ifind_indicators` 映射或 "
                "`_THEME_TO_SECTOR` 板块名映射，请提给开发。"),
        "manual": ("确需人工补充（数据源结构性没有）",
                   "> 这类可用下方覆盖文件模板填写，加 `--overrides` 重跑。"),
    }
    out = [f"### 自动取数失败（{len(gaps)} 项）", ""]
    for kind in ("manual", "rerun", "dev"):
        items = buckets[kind]
        if not items:
            continue
        head, tip = titles[kind]
        out += [f"**{head}** —— {len(items)} 项", "", "| 字段 | 原因 |", "|---|---|"]
        out += [f"| {f} | {r} |" for f, r in items]
        out += ["", tip, ""]
    return out


def _append_override_template(lines: list[str], ma, ext: list[str]) -> None:
    """把本次缺口渲染成可直接复制的覆盖文件。"""
    from core import overrides as ov

    tpl = ov.template(list(ma.gap_fields), ext, ma.field_values)
    if not tpl:
        return
    lines += [
        "### 覆盖文件模板（可直接复制填写）",
        "",
        "把下面这段存成 `.json`，填好 `值` 与 `来源` 后：",
        "",
        "```bash",
        'python main.py -b "原需求…" --overrides 你的文件.json',
        "```",
        "",
        "```json",
        tpl,
        "```",
        "",
        "> ⚠ **只列了允许人工覆盖的字段。** 判定引擎依赖的那 17 个字段"
        "（PB / ROE / 各类历史分位 / 波动率…）**不接受手工填写**，"
        "写进覆盖文件会直接报错中止——论点必须由机器取到的真数据触发，"
        "否则触发依据那行看不出它其实是人敲的（DESIGN §9.1）。",
        "> 人工填写的值在成品与本底稿中来源标为 `人工填写·…`，与自动取得的可区分。",
        "",
    ]


def _effective_type(spec) -> str:
    """规格声明的图型经渲染层加工后**实际**会画成什么。

    与 `layout` 里那段升级逻辑必须保持一致——两处判据一旦分叉，
    体检报的就不是读者看到的东西。故这里直接复用 layout 的判定函数。
    """
    from render.layout import _num, _units

    s = spec or {}
    t = s.get("类型") or "?"
    if t != "number_cards":
        return t
    pts = s.get("数据点") or []
    nums = [p for p in pts if _num(p.get("值")) is not None]
    if len(nums) >= 3 and len(nums) == len(pts) and len(_units(pts)) == 1:
        return "bar"          # 渲染层会升级成柱状图
    return t


def _chart_audit(ma, rc) -> list[str]:
    """图表体检：机械地卡"图少、图单调、图型信息量低"。

    为什么要有这道闸：图的质量退化是**静默**的。数字错了有 validator 拦，
    正文漏了有空缺检查拦，而"三条逻辑各配一张数字卡"完全合法——
    程序跑通、校验全绿、成品能出，只有人翻开看才发现信息量极低。
    实测这个毛病复发过两轮（#65 放开张数、#67 强制配柱状图都没根治，
    因为根因是 writer 手上只有标量、画不出别的，#71 才补上序列）。

    既然会复发，就不能靠每次人工翻看，得像 validator 卡数字那样卡下来：
    换个板块、换批论点后若又退回一堆数字卡，这里会直接写进底稿。
    只报告不拦截——图少不该阻断出报告，但必须让人看见。
    """
    logics = _rendered_logics(ma, rc)
    if not logics:
        return ["（无正文逻辑，跳过）", ""]

    # 只统计**会被渲染出来的**那几张：渲染层每条逻辑截断到 MAX_CHARTS_PER_LOGIC，
    # 按原始规格数统计会把体检做在读者看不到的图上（同下方"实际画出来的图型"同理）。
    from render.layout import MAX_CHARTS_PER_LOGIC as CAP

    specs = [(lc.逻辑id, s) for lc in logics
             for s in (lc.图表规格列表 or [])[:CAP]]
    # 统计**实际画出来的**图型，不是规格里声明的。渲染层会把满足条件的
    # number_cards 升级成柱状图（见 layout），若这里仍按声明类型统计，
    # 底稿会报"数字卡×2"而读者在成品上看到的是两张柱状图——体检自己先失真了。
    types = [_effective_type(s) for _, s in specs]
    无图 = [lc.逻辑id for lc in logics if not (lc.图表规格列表 or [])]
    # 信息量低的图型：只把数字换个字体印一遍，不表达比较、趋势或分布
    低信息 = {"number_cards", "table"}
    低 = [t for t in types if t in 低信息]

    from collections import Counter
    cnt = Counter(types)
    out = [
        f"- 正文逻辑 {len(logics)} 条，配图 {len(specs)} 张"
        f"（每条均值 {len(specs) / len(logics):.1f} 张）",
        f"- 图型分布：{'、'.join(f'{k}×{v}' for k, v in cnt.most_common()) or '无'}",
        "",
    ]
    warn = []
    if 无图:
        warn.append(f"**{len(无图)} 条逻辑完全没有配图**（`{'`、`'.join(无图)}`）"
                    "——多半是该条的数据画不成图，需人工确认是否值得补一张。")
    if len(specs) < len(logics):
        warn.append("平均每条逻辑不足 1 张图，版面偏空。")
    if 低 and len(低) / max(1, len(types)) > 0.5:
        warn.append(f"**{len(低)}/{len(types)} 张是数字卡或表格**——"
                    "这两种只是把数字排一遍，不表达比较/趋势/分布。"
                    "若同一批数同量纲可比，应改柱状图；若有历史序列，应改走势图。")
    if len(cnt) == 1 and len(specs) > 1:
        warn.append(f"全篇只用了一种图型（{list(cnt)[0]}），呈现过于单调。")
    if warn:
        out.append("**需注意：**")
        out += [f"- {w}" for w in warn]
    else:
        out.append("图表数量与图型分布正常。")
    out.append("")
    return out


# 行情类字段（三级回落：分析ETF自身 → 板块整体法 → 代表标的个股）。
# 与 fetcher._SECTOR_DERIVED 同一批，在这里独立列出是为了做**事后核对**：
# 声明的口径与真实 source 对不上时要看得出来。
_QUOTE_FIELDS = ("年化波动率", "波动率历史分位", "区间涨跌幅分位",
                 "换手率历史分位", "换手率近期高分位", "成交额历史分位")


def _quote_actual(ma) -> str:
    """按 `FieldValue.source` 统计行情类字段**实际**走了哪种口径。

    判据只看 source 字符串，不看当初打算走哪条——这正是本函数存在的理由：
    预期与实际会分叉（见 `_scope_section` 里的说明）。
    """
    buckets: dict[str, list[str]] = {}
    for f in _QUOTE_FIELDS:
        fv = ma.field_values.get(f)
        if not fv or not getattr(fv, "ok", False):
            continue
        src = str(getattr(fv, "source", ""))
        if "整体法" in src:
            kind = "板块整体法合成"
        elif ma.rep_code and ma.rep_code in src:
            kind = f"⚠ 代表标的单只个股（{ma.rep_code}）"
        else:
            kind = f"标的自身数据（{src.split('·')[-1].split('近')[0] or src}）"
        buckets.setdefault(kind, []).append(f)
    if not buckets:
        return ""
    return "；".join(f"{k} → {'、'.join(v)}" for k, v in buckets.items())


# 版面高度估算（px @ A4 版面）。
# **系数由真实渲染标定**（#83）：拿 6 份历史成品用 QtWebEngine 实际渲染后量各块高度，
# 而不是像先前那样按字号行距推算——推算版偏差不小：
#   核心结论 低估 17%（0.337 → 实测 0.40 px/字）
#   固定开销 高估 30%（300 → 实测约 230px）
# 校准后仍是**粗估**：真要准就直接导 PDF 数页数（`render.pdf_out.page_count`），
# 那才是唯一权威答案；本估算的价值在于**不启动 Qt 就能给出预警**。
_PX_PER_CHAR_BODY = 0.290       # 正文（实测 0.283 @752px 宽，A4 收窄到 734px 后微增）
_PX_PER_CHAR_CONCL = 0.405      # 核心结论（字号更大、有内边距，单字占高明显高于正文）
# 图按**行**算不按张算：同一条逻辑的两张图并排共一行。
# 实测单图高中位 198px，加上下间距约 210px；两张共行时各缩到约 77% 宽，行高约 165px。
_PX_CHART_ROW_1 = 230      # 独占一行：图更宽故更高
_PX_CHART_ROW_2 = 197      # 并排一行（实测 189/203/197）
_PX_FIXED = 264                 # 标题+副标题+页脚+页面内边距+外边距+各块间距（实测）
_PX_PER_LOGIC = 20              # 每条逻辑的标题行与上下间距
_PAGE_LIMIT = 1123              # A4 在 96dpi 下的高度（原写 1160 是按 820px 宽估的）                       # 820px 宽按 A4 比例对应的可用高度
# 「挂钩标的」卡片（render.layout._underlying_block）：估算值，未像上面几个
# 常量那样做过真实渲染校准（#83 那轮只测了逻辑正文和图表行）。这节 #84 才
# 从"预留不渲染"改成常驻渲染，先按 CSS 行高粗算，等有真实成品再回来校。
_PX_UNDERLYING_BASE = 95        # 标的行 + 报价说明，无挂钩理由/推荐结构时
_PX_UNDERLYING_WHY = 25         # 加一行挂钩理由（择优映射类需求才有）
_PX_UNDERLYING_STRUCT = 30      # 加一行 OptionHelper 推荐结构
_PX_QUOTE_BASE = 42             # 报价节标题、组间距与表后提示
_PX_QUOTE_GROUP = 22            # 每个挂钩标的分组标题
_PX_QUOTE_ROW = 24              # 表头或一行报价


def _chart_px(n: int) -> float:
    """n 张图在一条逻辑里占的纵向高度（每行最多 2 张）。"""
    full, rest = divmod(n, 2)
    return full * _PX_CHART_ROW_2 + (rest * _PX_CHART_ROW_1)


def _rendered_logics(ma, rc) -> list:
    """**实际会印在版面上的**逻辑（只有主轴）。

    #82 删掉「补充观察」后，可选池逻辑不再渲染，但体检与预算仍按 `rc.logics`
    全量统计——多算了一条正文与它的配图，预算因此虚高（实测把 3 条报成 4 条、
    1.13 页报成超页）。体检必须做在读者真正看得到的东西上，
    这与 `_effective_type`（按实际画出来的图型统计）是同一条原则。
    """
    from core.planner import SRC_SPINE

    src = {lg.逻辑id: lg.来源 for lg in (getattr(ma, "plan", None).logics
                                        if getattr(ma, "plan", None) else [])}
    body = [lc for lc in rc.logics if (lc.论述 or "").strip()]
    spine = [lc for lc in body if src.get(lc.逻辑id, SRC_SPINE) == SRC_SPINE]
    return spine or body        # 拿不到来源信息时不至于把整份报告统计成空


def _underlying_px(ma, rc, oh=None) -> float:
    """挂钩标的卡片本次会不会渲染、渲染多高——与 render.layout._underlying_block 同一套判断条件。"""
    try:
        from core import viewpoint as vp

        pkg = vp.build(ma, rc)
    except Exception:
        return 0.0
    if not getattr(pkg, "ok", False) or not pkg.标的代码:
        return 0.0
    px = _PX_UNDERLYING_BASE
    if pkg.挂钩理由 or pkg.板块理由:
        px += _PX_UNDERLYING_WHY
    if oh is not None and getattr(oh, "ok", False) and oh.product_name:
        px += _PX_UNDERLYING_STRUCT
    return px


def _quote_px(oh=None) -> float:
    """正式参考报价表估高；列数影响宽度，不影响纵向预算。"""
    if oh is None or not getattr(oh, "ok", False):
        return 0.0
    groups = list(getattr(oh, "quote_groups", []) or [])
    if not groups:
        return 0.0
    return (_PX_QUOTE_BASE
            + len(groups) * (_PX_QUOTE_GROUP + _PX_QUOTE_ROW)
            + sum(len(getattr(group, "rows", []) or []) * _PX_QUOTE_ROW for group in groups))


def _page_budget(ma, rc, oh_result=None) -> list[str]:
    """版面预算体检：估算这份成品大概占多高，超了就报警。

    「一页通」的硬约束是**一页**，而在此之前系统里没有任何机制保证这件事——
    `.page` 只设了 `width:820px`，高度由内容撑开，既无 `@page` 也无溢出检测。
    历史成品实测图数 0~8 张、正文 21~311 字、核心结论 65~367 字，
    最坏组合几乎不可能压进一页，只是因为一直只看 HTML（可以无限往下滚）
    而没暴露出来。等 A2 的 PDF 导出接上，这个问题会立刻变成"客户拿到两页半"。

    这里只报警不阻断：版面超限是**质量问题不是正确性问题**，
    该由人决定是删一张图、压一条逻辑，还是就这么发。
    """
    from render.layout import MAX_CHARTS_PER_LOGIC as CAP

    bodies = [len(lc.论述 or "") for lc in _rendered_logics(ma, rc)]
    # 按**实际渲染出来的**张数算，不是 writer 写了几项——渲染层会截断到 CAP，
    # 用原始长度会把预算算高，报出根本不存在的超页警告。
    超额 = [len(lc.图表规格列表 or []) for lc in _rendered_logics(ma, rc)]
    per_logic = [min(n, CAP) for n in 超额]
    charts = sum(per_logic)
    concl = len(rc.核心结论 or "")
    chart_px = sum(_chart_px(n) for n in per_logic)
    under_px = _underlying_px(ma, rc, oh_result)
    quote_px = _quote_px(oh_result)
    est = (_PX_FIXED + concl * _PX_PER_CHAR_CONCL
           + sum(bodies) * _PX_PER_CHAR_BODY
           + len(bodies) * _PX_PER_LOGIC + chart_px + under_px + quote_px)
    pages = est / _PAGE_LIMIT

    out = ["### 版面预算", "",
           f"- 核心结论 {concl} 字｜正文 {bodies}（合计 {sum(bodies)} 字）"
           f"｜配图 {per_logic}（合计 {charts} 张，占 {chart_px:.0f}px）"
           + (f"｜挂钩标的卡片 {under_px:.0f}px" if under_px else "")
           + (f"｜参考报价表 {quote_px:.0f}px" if quote_px else ""),
           f"- 估算高度 ≈ {est:.0f}px，约 **{pages:.2f} 页**（阈值 1 页 = {_PAGE_LIMIT}px）",
           ""]
    if any(n > CAP for n in 超额):
        out += [f"> ℹ writer 给了 {超额} 张图表规格，渲染层按每条逻辑上限 {CAP} 张"
                f"截断为 {per_logic}——多出的已丢弃，不占版面。"
                "若被丢的那张更重要，需在正文里调整论述侧重，让它成为前两张之一。", ""]
    if pages > 1.0:
        单 = [n for n in per_logic if n % 2 == 1]
        out += [f"> ⚠ **预计放不下一页**（约 {pages:.2f} 页）。"
                f"**超过 1.00 就是 2 页，没有中间态**——PDF 会直接多出一张。"
                "最有效的压缩顺序："
                f"①删图——独占一行的图约 {_PX_CHART_ROW_1}px，"
                f"等于 {_PX_CHART_ROW_1 / _PX_PER_CHAR_BODY:.0f} 字正文；"
                "②压核心结论；③砍最弱的一条逻辑。"]
        if 单:
            out.append("> 💡 有逻辑的配图是**奇数**张，最后一张独占一行最不划算——"
                       "给它补一张凑成并排、或直接删掉，都比删两张分散的图省版面。")
        out.append("")
    elif pages < 0.6:
        out += ["> ⚠ **版面偏空**（不足六成）。这通常不是好事——多半是正文太薄或配图缺失，"
                "对照上方「图表体检」与「正文空缺」一起看。", ""]
    else:
        out += ["> 版面高度正常。", ""]
    out += ["> ⚠ 这是**粗估**（按字号行距折算，未跑真实渲染），只用于分出"
            "「肯定超」「肯定空」两档；接近 1 页时以实际渲染为准。", ""]
    return out


def _trace_table(ma, vr) -> list[str]:
    """正文里每个数字 → 它溯源到的字段 → 该字段的真实口径。

    **这是唯一能查出"张冠李戴"的地方。** validator 只比**数值**是否对得上，
    不比**口径**是否说对了：正文写"板块近20日 -0.33%"，而这个数其实来自字段
    `代表标的603986.SH近20日`，数值完全匹配，validator 判"命中"，一路放行——
    数字是真的，论断却把一只股票说成了整个板块（DESIGN §9.1 那一类错误）。
    writer 也拦不住，它收到的每个字段只有名字和值、没有 source。

    所以把 `Finding.匹配字段`（validator 本就为每个数字算好了，只是此前只对
    "可疑"的显示、"命中"的丢掉）连同该字段的 source 一起摊开，
    让复核的人能逐行对："正文管这个数叫什么" vs "这个数实际是什么口径"。
    """
    findings = list(getattr(vr, "findings", []) or [])
    if not findings:
        return []
    fvs = ma.field_values or {}
    out = [
        f"### 数字溯源明细（{len(findings)} 个）",
        "",
        "| 出现位置 | 正文里的数字 | 溯源到字段 | 该字段的实际口径 |",
        "|---|---|---|---|",
    ]
    单股 = 0
    for f in findings:
        field = f.匹配字段 or ""
        fv = fvs.get(field)
        src = str(getattr(fv, "source", "") or "—") if fv is not None else "—"
        判 = "" if f.判定 == "命中" else f" ⚠{f.判定}"
        # 个股口径的行单独标出来：板块类报告里这些是最可能被写成"板块如何如何"的，
        # 也是唯一需要人逐句核对的少数几行，不标出来会淹没在几十行里。
        if ma.rep_code and ma.rep_code in src:
            src = f"**⚠个股口径** {src}"
            单股 += 1
        out.append(f"| {f.位置}{判} | {f.数字} | {field or '（无匹配）'} | {src} |")
    out += [
        "",
        "> **这张表是查「张冠李戴」的地方。** 校验器只保证数字能对上某个真实字段，"
        "**不保证正文把它说对了**——用代表标的一只股票的涨跌去讲整个板块，"
        "数值完全能对上、校验照样通过、成品上也看不出任何痕迹。"
        "请逐行核对「正文在这个位置管它叫什么」与「该字段的实际口径」是否一致。",
    ]
    if 单股:
        out.append(f"> ‼ 本次有 **{单股} 处**数字来自代表标的（{ma.rep_code}）的**单只个股**口径，"
                   "已在上表标出。**请优先核对这几处**：若正文把它们表述成"
                   "「板块」「行业」如何，即为口径张冠李戴，须改写或换用板块口径数据。")
    out += [
        "> ⚠ 同一个数值若有多个字段都能对上，本表取**第一个**匹配到的字段，"
        "归属可能不准；此时以「各字段的实际来源」表为准。",
        "",
    ]
    return out


def _selection_section(ma) -> list[str]:
    """挂钩标的择优的过程与实测指标（B4/§9.2②）。

    只在板块→ETF 映射给不出答案时才有内容。把**候选池规模、择优维度、
    每个候选的实测指标**都摊开：这一步是"分析对象 → 可交易标的"的映射，
    是全篇最需要人复核的判断之一，只给一个结论不足以让人否决它。
    """
    p = getattr(ma, "挂钩择优", None)
    if p is None:
        return []
    if not getattr(p, "ok", False):
        return ["", f"> ⚠ 挂钩标的择优失败：{getattr(p, 'error', '')}"
                    "——本节挂钩标的仍为空，需人工指定。", ""]
    if not p.picks:
        return ["", f"> ⚠ 候选池 {p.候选数} 个中未选出合适标的：{p.说明 or '（未说明）'}", ""]

    out = ["", f"**挂钩标的择优**（候选池 {p.候选数} 个"
                + (f"；维度：{'、'.join(p.择优维度)}" if p.择优维度 else "") + "）", ""]
    out += ["| 选中 | 标的 | 实测指标 | 适合 | 理由 |", "|---|---|---|---|---|"]
    for i, k in enumerate(p.picks):
        seg = "、".join(f"{n}{v}" for n, v in k.展示.items() if v) or "—"
        out.append(f"| {'★' if i == 0 else ''} | {k.简称}（{k.代码}）| {seg} | "
                   f"{k.适合 or '—'} | {k.理由 or '—'} |")
    out.append("")
    if p.说明:
        out += [f"> 择优结论：{p.说明}", ""]
    out += ["> 挂钩标的与分析对象**不是同一个**时（产业趋势/事件驱动类，其分析对象"
            "本身不可交易），这层映射的理由必须成立才用得上——请重点复核上表的"
            "「理由」是否真由实测指标支撑，以及候选池里有没有更合适而被漏掉的。", ""]
    return out


def _source_table(ma) -> list[str]:
    """逐字段列出真实来源。

    此前底稿只列**失败**字段的原因，成功字段的 `source` 一个都不显示，
    于是"这个数到底是 ETF 的、板块聚合的、还是某一只个股的"在成品与底稿里
    都查不到——而 writer 收到的只有字段名与值（没有 source），它同样分辨不了。
    口径混用因此可以完全无声地发生。这张表是唯一能事后核对的地方。
    """
    from core import overrides as ov

    rows = []
    for name, fv in (ma.field_values or {}).items():
        if not isinstance(name, str) or name.startswith("__"):
            continue
        if not getattr(fv, "ok", False):
            continue
        src = str(getattr(fv, "source", "") or "—")
        mark = " 🖉" if ov.is_manual(fv) else ""
        disp = str(getattr(fv, "display", "") or getattr(fv, "value", ""))
        rows.append(f"| {name}{mark} | {disp[:46]} | {src} |")
    if not rows:
        return []
    return [
        "### 本次各字段的实际来源",
        "",
        "| 字段 | 值 | 来源（口径）|",
        "|---|---|---|",
        *rows,
        "",
        "> 🖉 = 分析师人工填写，非数据源自动取得。",
        "> **口径以本表为准**：同名字段会因走 ETF 自身 / 板块整体法 / 代表标的"
        "而得到不同口径的值，成品正文里看不出区别，只有这里能核对。",
        "",
    ]


def _scope_section(ma, sector: str) -> list[str]:
    """分析对象与口径。#70 前印在成品页脚，实为内部工作信息，移到这里。

    口径是整份报告最容易被质疑的地方（板块选错过：芯片需求取到医疗服务成分股，#61），
    所以要把"怎么选的"完整摊开，让复核的人能自己判断这个映射合不合理。
    """
    from core import universe

    名 = getattr(ma, "rep_name", "") or ""
    out = [
        f"- **板块**：{sector or '（未指定）'}",
        f"- **数据代表标的**：{名 + ' ' if 名 else ''}{ma.rep_code}"
        "（仅供板块口径解析用的锚点，见下方「数据口径」一行）",
    ]
    if getattr(ma, "板块理由", ""):
        out.append(f"- **选取依据**：{ma.板块理由}")

    # #73：波动率/涨跌幅/换手率/成交额分位这几项，只要有一只流动性够格的 ETF，
    # 就直接取它自己的价格数据——此时"分析对象"与"最终推荐挂钩的标的"是同一个，
    # 不再有基差；PB/ROE 等基本面仍走成分股聚合，见下方说明。
    # ⚠ 这一行必须按**实际发生**写，不能按预期写。行情类字段有三级回落
    #  （分析ETF自身 → 板块整体法合成 → 代表标的单只个股），此前这里只按
    #  "有没有解析出 ETF"二选一地印一句话，而在"无合格 ETF 且板块聚合也失败"时
    #  字段其实已经跌到单只个股，声明却仍写着"退回成分股聚合口径"——是错的。
    #  口径写错比不写更糟：复核的人会据此认为数字是板块级的。
    etf_code = ma.field_values.get("__etf__")
    etf_note = ma.field_values.get("__etf_note__") or ""
    实际 = _quote_actual(ma)
    if etf_code:
        out.append(f"- **行情数据口径**：{etf_code}（{etf_note}），"
                   "波动率/涨跌幅分位/换手率/成交额分位取自该 ETF 自身价格数据")
    elif etf_code == "":
        out.append(f"- **行情数据口径**：无合格 ETF（{etf_note}），"
                   "行情类字段退回板块整体法合成口径")
    if 实际:
        out.append(f"- **实际生效口径**（按字段真实来源统计）：{实际}")

    parts = universe.broad_parts(sector) if sector else []
    if parts:
        out += [
            f"- **宽口径展开**：{('、'.join(parts))}　共 {len(parts)} 个一级行业合并（整体法）",
            f"- **口径依据**：{universe.classification_basis(sector)}",
        ]
    out.append("- **数据来源**：行情与财务数据 iFinD（同花顺）")
    out.append("")
    notes = []
    if etf_code:
        notes.append("行情类字段（波动率/涨跌幅/换手率/成交额分位）是上述 ETF 自己的真实数据；"
                     "**估值与盈利类字段（PB/ROE/净利同比等）仍是成分股整体法聚合**——"
                     "ETF 与其跟踪指数均不直接提供这类历史序列，聚合数字在正文里的作用是"
                     "\"支撑该 ETF 值得配置的基本面证据\"，不是 ETF 自身的财务指标。")
    if parts:
        notes.append("宽口径板块的估值/盈利/分位/波动率聚合部分均为**合并口径整体法**"
                     "（Σ市值/Σ净资产，非加权平均），成分股取各行业市值前列合并后的前 30 大。")
    if notes:
        out += [f"> {n}" for n in notes] + [""]

    # #74：触发实体（事件本体，可能是境外标的）解析结果单独记一节——
    # 校验没过时也要如实写出来（LLM猜的代码格式不对、或这家公司数据源确实查不到），
    # 而不是让它无声消失，读者至少知道"我们试过，没成"而不是"从没想起过这件事"。
    trig_code = ma.field_values.get("__trigger_code__")
    trig_name = ma.field_values.get("__trigger_name__")
    if trig_code:
        got = {k[len("触发标的_"):]: fv.display for k, fv in ma.field_values.items()
              if isinstance(k, str) and k.startswith("触发标的_") and getattr(fv, "ok", False)}
        out.append(f"- **触发实体**：{trig_name}（{trig_code}）")
        if got:
            out.append("  已取到：" + "、".join(f"{k}={v}" for k, v in got.items()))
        else:
            out.append("  ⚠ 代码校验通过，但未取到任何数据（该市场覆盖有限，或字段本身缺失）")
        out.append("")
    return out


def _viewpoint_section(ma, rc) -> list[str]:
    """给 OptionHelper 的观点包。

    放进内部底稿而不是直接喂给下游：这份东西是**产品选择的输入**，
    方向、期限、波动率看法都会影响报价，必须有人过一眼再用。
    落在这里既便于核对，也留痕——事后能查当时是按什么观点定的结构。
    """
    try:
        from core import viewpoint as vp

        pkg = vp.build(ma, rc)
    except Exception as e:                       # 观点包失败不该拖垮整份底稿
        return [f"（观点包生成失败：{type(e).__name__}: {e}）", ""]
    if not pkg.ok:
        return [f"（观点包不可用：{pkg.error}）", ""]

    # ① 实际发送给 OptionHelper 的原文。它仅由已验证市场事实组成，不含 writer
    #    结论、产品建议、结构、期限或执行价；照原样贴出来便于逐字核对。
    out = ["**① 实际发送给 OptionHelper 的内容**（自然语言原文，逐字如下）：", ""]
    try:
        from core import optionhelper_bridge as _ohb

        sent = _ohb.build_prompt(pkg)
    except Exception as e:
        sent = f"（无法生成发送原文：{type(e).__name__}: {e}）"
    out += ["```", sent, "```", ""]

    # ② 以下全部是分析师核对用、不发送给 OptionHelper 的内部信息：挂钩标的口径、
    #    数据锚点、择优过程、每条逻辑对应的论点库代号与特征。
    out += ["**② 分析师核对用（不发送给 OptionHelper）**", ""]
    out.append(f"- **挂钩标的**：{pkg.标的名称}（{pkg.标的代码}）" if pkg.标的代码
               else f"- **挂钩标的**：未定 —— {pkg.标的口径}")
    if pkg.板块:
        out.append(f"- **板块**：{pkg.板块}")
    if pkg.标的代码:
        out.append(f"- **标的口径**：{pkg.标的口径}")
    if pkg.数据代表标的:
        out.append(f"- **数据代表标的**：{pkg.数据代表标的}"
                   "（仅为取数与相对强弱的锚点，**不是挂钩对象**）")
    if getattr(pkg, "挂钩理由", ""):
        out.append(f"- **挂钩理由**：{pkg.挂钩理由}")
    if getattr(pkg, "标的选择说明", ""):
        out.append(f"- **标的选择说明**：{pkg.标的选择说明}")
    out += ["", "| 市场字段 | 数值 | 来源 | 截止日 |", "|---|---|---|---|"]
    for fact in pkg.市场事实:
        out.append(f"| {fact.标签} | {fact.数值} | {fact.来源 or '—'} | {fact.截止日 or '—'} |")
    outlook = pkg.市场展望
    out += ["", "**市场展望（仅市场判断，不含结构建议）**", ""]
    out.append(f"- **预计方向**：{outlook.方向 or '—'}")
    if outlook.窗口:
        out.append("- **观察窗口**：" + "、".join(outlook.窗口))
    if outlook.支持因素:
        out.append("- **主要支持因素**：" + "、".join(outlook.支持因素))
    if outlook.制约因素:
        out.append("- **主要制约因素**：" + "、".join(outlook.制约因素))
    if outlook.需验证风险:
        out.append("- **需持续验证的风险**：" + "、".join(outlook.需验证风险))
    out += _selection_section(ma)
    out += [
        "",
        "| 逻辑 | 方向 | 强度 | 窗口 | 确定性 | 风险点 |",
        "|---|---|---|---|---|---|",
    ]
    for lv in pkg.逻辑要点:
        out.append(f"| `{lv.逻辑id}` | {lv.方向 or '—'} | {lv.强度 or '—'} | "
                   f"{lv.窗口 or '—'} | {lv.确定性 or '—'} | {lv.风险点 or '—'} |")
    out.append("")
    out += [
        "> 观点包只重整已有数据，**不产生任何新数字**，其中每个数都能追回摸底取数或论点库判定。",
        "> **只描述市场状态，不含任何产品或结构建议**——挂什么结构、什么期限、"
        "买方还是卖方，由 OptionHelper 依据实时波动率曲面与报价决定；"
        "本系统既无曲面也无报价，越权给方案就是无依据的断言。",
    ]
    if pkg.标的代码:
        out.append("> ⚠ **基差提醒**：挂钩 ETF 跟踪的指数与本报告的板块口径并不完全等同，"
                   "上文的估值/波动率/分位是按报告口径的成分股整体法算的，定价时需考虑这层差异。")
    out.append("")
    return out


def _optionhelper_section(oh) -> list[str]:
    """OptionHelper 最新 Skill 正式 Quote 的本次调用结果——不管成败都要落一笔。

    `--optionhelper` 没开时 `oh is None`，如实写"未调用"；开了但失败时把 stage/
    error/missing 原样摊开，让分析师能直接照着补（缺 token 还是缺环境一看便知），
    不能只在客户版面留一句"报价由交易台确定"就把真实原因吞掉。
    """
    if oh is None:
        return ["（本次未调用——生成时未加 `--optionhelper` 开关）", ""]
    if not getattr(oh, "ok", False):
        out = [f"- **状态**：失败（{oh.stage or '未知阶段'}）", f"- **原因**：{oh.error or '（无详细信息）'}"]
        if oh.missing:
            out.append("- **缺失前置条件**：")
            out += [f"  - {m}" for m in oh.missing]
        out.append("")
        return out
    out = [
        f"- **推荐结构**：{oh.product_name}" + (f"（{oh.product_id}）" if oh.product_id else ""),
        f"- **理由**：{oh.reason or '（无）'}",
    ]
    if oh.main_risks:
        out.append("- **主要风险**：" + "；".join(oh.main_risks))
    out.append(f"- **交付状态**：{oh.status}（{'完整' if oh.coverage_status == 'complete' else oh.coverage_status or '—'}）")
    if oh.module_failures:
        out.append("- **未完成模块**：" + "；".join(f"{k}：{v}" for k, v in oh.module_failures.items()))
    if oh.report_path:
        out.append(f"- **正式 Quote 文件**：`{oh.report_path}`")
    if getattr(oh, "designer_input_path", ""):
        out.append(f"- **冻结报价事实**：`{oh.designer_input_path}`")
    groups = list(getattr(oh, "quote_groups", []) or [])
    if groups:
        out.append(f"- **一页通表格**：{len(groups)} 组、"
                   f"{sum(len(group.rows) for group in groups)} 行正式参考报价")
    if oh.assumptions:
        out.append("- **计算假设**：" + "；".join(oh.assumptions))
    out.append("")
    out.append("> 结构选择由当前对话 Agent 按新版 Recommender 指南完成；合同、取数、"
               "收益结构、定价与冻结交付由 OptionHelper 受控链路完成。本系统不推导表格数值。")
    out.append("")
    return out


def build_markdown(ma, rc, vr, *, title: str, html_path: str, oh_result=None) -> str:
    """把本次生成的缺口与复核项排成一份可留存的 Markdown。"""
    from core.planner import DOC_FIELD_PREFIX, SRC_SPINE

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    sector = ma.field_values.get("__sector__") or ""
    lines: list[str] = [
        f"# 内部底稿 · {title}",
        "",
        f"> 生成时间：{now}　｜　成品：`{Path(html_path).name}`",
        "",
        "> **本文件供内部使用，不随一页通发出。**",
        "",
        "---",
        "",
        "## 一、本次分析对象与口径",
        "",
    ]
    lines += _scope_section(ma, sector)
    lines += _source_table(ma)
    lines += ["---", "", "## 二、给 OptionHelper 的观点包", ""]
    lines += _viewpoint_section(ma, rc)
    lines += ["---", "", "## 三、OptionHelper 正式参考报价调用结果", ""]
    lines += _optionhelper_section(oh_result)
    lines += ["---", "", "## 四、需要人工补充的数据", ""]

    # 1) 自动取数失败 —— **按补救路径分类**，而不是笼统列一张表。
    #    此前三种毫不相干的东西混在同一节里（重跑就好的、要改代码的、真要人填的），
    #    分析师看不出哪几条与自己有关，整节标题却写着"需要人工补充的数据"。
    lines += _gap_sections(ma)

    # 2) 外部事实：已填的与仍缺的分开列——已填的属于"本次用了什么"，
    #    仍缺的才是待办；混在一起会让人重复去找已经补过的东西。
    filled = dict(getattr(ma, "外部事实已填", {}) or {})
    if filled:
        lines.append(f"### 外部事实·分析师已人工填写（{len(filled)} 条）")
        lines.append("")
        for k, v in filled.items():
            lines.append(f"- **{k}**：{v}")
        lines += ["", "> 这些内容已作为可引用事实进入正文撰写，"
                      "**不是**数据源自动取得，出处由填写人负责。", ""]

    ext = list(getattr(ma, "外部事实待补", []) or [])
    if ext:
        lines.append(f"### 需求里提到、但数据源结构性没有（{len(ext)} 项）")
        lines.append("")
        lines += [f"- [ ] {x}" for x in ext]
        lines += [
            "",
            "> 这类（海外公司业绩、渗透率、产业测算等）行情接口拿不到。两条补法：",
            "> ① 有研报写了它 → 放进 `sources/`，加 `--pick` 重跑，进候选清单由你勾选；",
            "> ② 你手上直接有这个数（公告/终端/内部测算）→ 填下方覆盖文件模板，"
            "加 `--overrides` 重跑。",
            "",
        ]

    # 3) 覆盖文件模板：把"缺什么"直接变成"照着填就能用的东西"。
    #    只列**允许覆盖**的字段——判定字段即使缺了也不放进模板，
    #    否则等于引导人去填一个填了就会被拒的东西（见 core/overrides.py 的铁律）。
    _append_override_template(lines, ma, ext)

    # 3) 需人工复核
    lines += ["---", "", "## 五、需要人工复核", ""]

    missing_logics = list(getattr(rc, "空缺逻辑", []) or [])
    if missing_logics:
        lines.append(f"### 正文空缺（{len(missing_logics)} 条）")
        lines.append("")
        lines.append(f"`{'`、`'.join(missing_logics)}` —— 撰写环节漏写了正文。")
        lines.append("**这些逻辑已从成品中剔除**（不留只有标题的空壳），故成品比规划少几条。")
        lines.append("建议重跑；若反复漏同一条，多半是该逻辑缺可用数据，需人工补写。")
        lines.append("")
    else:
        lines += ["### 正文空缺", "", "无。", ""]

    suspects = list(getattr(vr, "suspects", []) or [])
    if suspects:
        from core.validator import DIRECTION

        lines.append(f"### 可疑数字（{len(suspects)} 处）")
        lines.append("")
        lines.append("| 位置 | 数字 | 判定 | 说明 |")
        lines.append("|---|---|---|---|")
        for f in suspects:
            if f.判定 == DIRECTION:
                lines.append(f"| {f.位置} | {f.数字} | **方向可能抄反** | "
                             f"数值能对上 `{f.匹配字段}`，但**符号相反**——"
                             f"正文的涨跌措辞与真值方向不一致，**优先核这条** |")
            else:
                lines.append(f"| {f.位置} | {f.数字} | 可疑 | 无匹配真值，需人工确认 |")
        lines.append("")
        if any(f.判定 == DIRECTION for f in suspects):
            lines.append("> ‼ **方向抄反比数字对不上更危险**：数字本身是真的，只是涨说成了跌"
                         "（或反之），读者无从察觉。投研里这类错误的代价最高。")
            lines.append("")
    else:
        n = len(getattr(vr, "findings", []) or [])
        lines += ["### 可疑数字", "",
                  f"无——{n} 个带单位数字全部可溯源。", ""]
        if n <= 4:
            lines.append("> ⚠ 数字总数偏少。这个指标会骗人：**内容越少越容易「全部可溯源」**。")
            lines.append("> 若同时存在正文空缺，说明达标是因为内容缺失，不是因为质量好。")
            lines.append("")

    lines += _trace_table(ma, vr)

    # 3b) 图表体检
    lines += ["### 图表体检", ""]
    lines += _chart_audit(ma, rc)

    # 3c) 版面预算：一页通的硬约束是"一页"，而此前没有任何机制保证
    lines += _page_budget(ma, rc, oh_result)

    # 4) 来源
    lines += ["---", "", "## 六、本次用到的来源", "", "- 行情与财务数据：iFinD（同花顺）"]
    docs_used = []
    for name, fv in (ma.field_values or {}).items():
        if not isinstance(name, str) or not name.startswith(DOC_FIELD_PREFIX):
            continue
        if not getattr(fv, "ok", False):
            continue
        cite = f"{fv.source} {getattr(fv, 'note', '')}".strip()
        if cite not in docs_used:
            docs_used.append(cite)
    if docs_used:
        lines.append("- 研报引用：")
        lines += [f"  - {c}" for c in docs_used]
        lines.append("")
        lines.append("> `sources/` 是用完即清的工作台。上面这些出处已固化在此，")
        lines.append("> 原 PDF 删掉后仍可凭机构/标题/日期/页码回平台找回原文。")
    lines.append("")

    # 5) 实际写进正文的论点
    id2plan = {lg.逻辑id: lg for lg in ma.plan.logics}
    written = [lc for lc in rc.logics if (lc.论述 or "").strip()]
    lines += ["---", "", "## 七、本次实际写进正文的论点", ""]
    if written:
        for lc in written:
            p = id2plan.get(lc.逻辑id)
            src = f"（{p.来源}）" if p and p.来源 != SRC_SPINE else ""
            lines.append(f"- `{lc.逻辑id}` {p.标题 if p else ''}{src}")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def write_gap_report(ma, rc, vr, *, title: str, html_path: str, oh_result=None) -> str:
    """产出内部底稿，返回路径。与一页通同名并排存放。"""
    p = Path(html_path)
    out = p.with_name(f"{p.stem}_内部底稿.md")
    out.write_text(build_markdown(ma, rc, vr, title=title, html_path=html_path, oh_result=oh_result),
                   encoding="utf-8")
    return str(out)
