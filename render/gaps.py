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


def _chart_audit(rc) -> list[str]:
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
    logics = [lc for lc in rc.logics if (lc.论述 or "").strip()]
    if not logics:
        return ["（无正文逻辑，跳过）", ""]

    specs = [(lc.逻辑id, s) for lc in logics for s in (lc.图表规格列表 or [])]
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


def _scope_section(ma, sector: str) -> list[str]:
    """分析对象与口径。#70 前印在成品页脚，实为内部工作信息，移到这里。

    口径是整份报告最容易被质疑的地方（板块选错过：芯片需求取到医疗服务成分股，#61），
    所以要把"怎么选的"完整摊开，让复核的人能自己判断这个映射合不合理。
    """
    from core import universe

    名 = getattr(ma, "rep_name", "") or ""
    out = [
        f"- **板块**：{sector or '（未指定）'}",
        f"- **代表标的**：{名 + ' ' if 名 else ''}{ma.rep_code}",
    ]
    if getattr(ma, "板块理由", ""):
        out.append(f"- **选取依据**：{ma.板块理由}")
    parts = universe.broad_parts(sector) if sector else []
    if parts:
        out += [
            f"- **宽口径展开**：{('、'.join(parts))}　共 {len(parts)} 个一级行业合并（整体法）",
            f"- **口径依据**：{universe.classification_basis(sector)}",
        ]
    out.append("- **数据来源**：行情与财务数据 iFinD（同花顺）")
    out.append("")
    if parts:
        out += [
            "> 宽口径板块的估值/盈利/分位/波动率均为**合并口径整体法**"
            "（Σ市值/Σ净资产，非加权平均），成分股取各行业市值前列合并后的前 30 大。",
            "",
        ]
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

    out = [
        f"- **挂钩标的**：{pkg.标的名称}（{pkg.标的代码}）" if pkg.标的代码
        else f"- **挂钩标的**：未定 —— {pkg.标的口径}",
    ]
    if pkg.板块:
        out.append(f"- **板块**：{pkg.板块}")
    if pkg.标的代码:
        out.append(f"- **标的口径**：{pkg.标的口径}")
    if pkg.数据代表标的:
        out.append(f"- **数据代表标的**：{pkg.数据代表标的}"
                   "（仅为取数与相对强弱的锚点，**不是挂钩对象**）")
    out += [
        f"- **整体方向**：{pkg.整体方向 or '—'}",
        f"- **波动率看法**：{pkg.波动率看法 or '（未取到）'}",
        "",
        "| 逻辑 | 方向 | 强度 | 窗口 | 确定性 | 市场含义（方向/时间尺度/波动率/失效条件）|",
        "|---|---|---|---|---|---|",
    ]
    for lv in pkg.逻辑要点:
        out.append(f"| `{lv.逻辑id}` | {lv.方向 or '—'} | {lv.强度 or '—'} | "
                   f"{lv.窗口 or '—'} | {lv.确定性 or '—'} | {lv.市场含义 or '—'} |")
    out.append("")
    if pkg.风险提示汇总:
        out += ["**风险提示**：" + "；".join(pkg.风险提示汇总), ""]
    out += [
        "> 观点包只重整已有数据，**不产生任何新数字**，其中每个数都能追回摸底取数或论点库判定。",
        "> **本节只描述市场状态，不含任何产品或结构建议**——挂什么结构、什么期限、"
        "买方还是卖方，由 OptionHelper 依据实时波动率曲面与报价决定；"
        "本系统既无曲面也无报价，越权给方案就是无依据的断言。",
    ]
    if pkg.标的代码:
        out.append("> ⚠ **基差提醒**：挂钩 ETF 跟踪的指数与本报告的板块口径并不完全等同，"
                   "上文的估值/波动率/分位是按报告口径的成分股整体法算的，定价时需考虑这层差异。")
    out.append("")
    return out


def build_markdown(ma, rc, vr, *, title: str, html_path: str) -> str:
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
    lines += ["---", "", "## 二、给 OptionHelper 的观点包", ""]
    lines += _viewpoint_section(ma, rc)
    lines += ["---", "", "## 三、需要人工补充的数据", ""]

    # 1) 自动取数失败
    gaps = ma.gap_fields
    if gaps:
        lines.append(f"### 自动取数失败（{len(gaps)} 项）")
        lines.append("")
        lines.append("| 字段 | 原因 |")
        lines.append("|---|---|")
        for f in gaps:
            lines.append(f"| {f} | {_fmt_gap_reason(ma.field_values.get(f))} |")
        lines.append("")
        lines.append("> 原因里若是 `ConnectionError` 之类，多为接口临时故障，重跑即可；")
        lines.append("> 若是「未匹配到板块」，则是板块名对不上数据源，需补 `_THEME_TO_SECTOR` 映射。")
        lines.append("")
    else:
        lines += ["### 自动取数失败", "", "无——本次所需字段全部取到。", ""]

    # 2) 外部事实待补
    ext = list(getattr(ma, "外部事实待补", []) or [])
    if ext:
        lines.append(f"### 需求里提到、但数据源结构性没有（{len(ext)} 项）")
        lines.append("")
        lines += [f"- [ ] {x}" for x in ext]
        lines += [
            "",
            "> 这类（海外公司业绩、渗透率、产业测算等）行情接口拿不到，只能来自研报。",
            "> 找到对应研报后放进 `sources/`，加 `--pick` 重跑即可进入候选清单。",
            "",
        ]

    # 3) 需人工复核
    lines += ["---", "", "## 四、需要人工复核", ""]

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

    # 3b) 图表体检
    lines += ["### 图表体检", ""]
    lines += _chart_audit(rc)

    # 4) 来源
    lines += ["---", "", "## 五、本次用到的来源", "", "- 行情与财务数据：iFinD（同花顺）"]
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
    lines += ["---", "", "## 六、本次实际写进正文的论点", ""]
    if written:
        for lc in written:
            p = id2plan.get(lc.逻辑id)
            src = f"（{p.来源}）" if p and p.来源 != SRC_SPINE else ""
            lines.append(f"- `{lc.逻辑id}` {p.标题 if p else ''}{src}")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def write_gap_report(ma, rc, vr, *, title: str, html_path: str) -> str:
    """产出内部底稿，返回路径。与一页通同名并排存放。"""
    p = Path(html_path)
    out = p.with_name(f"{p.stem}_内部底稿.md")
    out.write_text(build_markdown(ma, rc, vr, title=title, html_path=html_path),
                   encoding="utf-8")
    return str(out)
