"""观点包：把已生成的报告内容整理成 OptionHelper 做产品选择要用的输入。

DESIGN §10 定义的观点包结构：{ 标的, 方向, 期限倾向, 波动率看法, 风险偏好 }。

研究阶段产出的是不预选标的的只读共同观点快照。分析师在研究完成后确认待报价
标的时，GUI 才把当前标的产品画像追加到该轮 OptionHelper 输入；落盘用于追溯
当时按什么研究观点与标的事实选择结构。

只读取 `MarketAnalysis`/`ReportContent` 已有的数据重新整理，**不产生任何新数字、
不调用 LLM**——观点包里出现的每个数字，源头都能追回摸底取数或论点库特征列，
与全项目"数字必须可溯源"这条主线一致。

两条边界（#71）：
1. **研究取数对象不是正式挂钩标的。** 标准行业、人工篮子或主题 ETF 先形成
   共同研究结论；客户代码和系统候选必须等研究完成后再由分析师确认。
   代表个股仍在 `数据代表标的` 里留一份，供追溯，但明确标注不是挂钩对象。
2. **只描述市场状态，不给产品建议。** 挂什么结构、什么期限、买方还是卖方，
   是 OptionHelper 依据实时波动率曲面与报价决定的事。本系统既没有曲面也没有报价，
   在这里写"卖权收益厚"是无依据的断言，且会让不掌握定价信息的环节替定价环节做决定。
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field as dfield

from .pipeline import MarketAnalysis
from .writer import ReportContent


@dataclass
class LogicView:
    """单条逻辑贡献给观点包的部分。"""

    逻辑id: str
    方向: str = ""       # 来自论点库特征列；研报观点/自由槽没有对应条目，留空
    强度: str = ""
    窗口: str = ""
    确定性: str = ""
    风险点: str = ""


@dataclass
class MarketFact:
    """可追溯的市场事实；只允许来自已验证 FieldValue。"""

    标签: str
    数值: str
    来源: str = ""
    截止日: str = ""


@dataclass
class MarketOutlook:
    """对未来市场状态的受控表述，不包含任何产品或条款建议。"""

    方向: str = ""
    方向来源: str = ""
    窗口: list[str] = dfield(default_factory=list)
    支持因素: list[str] = dfield(default_factory=list)
    制约因素: list[str] = dfield(default_factory=list)
    需验证风险: list[str] = dfield(default_factory=list)


@dataclass
class ViewPackage:
    标的代码: str                   # **可交易的挂钩标的**（板块 ETF），非数据阶段的代表个股
    标的名称: str = ""
    标的口径: str = ""              # 该 ETF 跟踪什么，与本报告板块口径是否一致
    # 挂钩标的**不等于**分析对象时，这里是那一层映射的理由（B4/§9.2②）。
    # 板块类需求两者本就同一，此项为空；产业趋势/事件驱动类必须有——
    # 不允许"分析 A、推荐 B、却对 A 与 B 的关系只字不提"（见设计理念）。
    挂钩理由: str = ""
    数据代表标的: str = ""          # 数据锚点，仅供追溯，不是挂钩对象
    板块: str = ""
    板块理由: str = ""
    整体方向: str = ""              # rc.推荐方向
    方向来源: str = ""              # writer 字段 / 核心结论受控提取 / 研究计划
    波动率看法: str = ""            # 年化波动率 + 历史分位，拼成一句人可读的话
    情景收益带: object | None = None  # 历史相似状态的未来收益分位，非预测/非产品建议
    市场事实: list[MarketFact] = dfield(default_factory=list)
    市场展望: MarketOutlook = dfield(default_factory=MarketOutlook)
    页面市场摘要: str = ""          # 仅一页通使用；完整事实仍留在市场事实
    标的选择说明: str = ""          # 为什么选择这个 ETF，不涉及产品结构
    逻辑要点: list[LogicView] = dfield(default_factory=list)
    风险提示汇总: list[str] = dfield(default_factory=list)   # 各条风险点去重保序
    ok: bool = False
    error: str = ""


_DIRECTIONS = ("看涨", "看跌", "震荡", "中性")


def _canonical_direction(value: object) -> str:
    """将已存在的研究结论归一为 OptionHelper 能识别的单一市场观点。"""
    text = str(value or "").strip()
    if text in _DIRECTIONS:
        return text
    hit = [item for item in _DIRECTIONS if item in text]
    if "震荡" in hit or ("看涨" in hit and "看跌" in hit):
        return "震荡"
    return hit[0] if len(hit) == 1 else ""


def _resolve_direction(ma: MarketAnalysis, rc: ReportContent) -> tuple[str, str]:
    """补齐 Writer 偶发漏掉的 JSON 字段，但绝不生成新的市场判断。

    优先级：Writer 的结构化字段 > 核心结论中明确的“方向/倾向”句 > 已有研究计划。
    如果三处都没有明确方向，保留空值，由报价桥接显式拦截，不能让 OptionHelper 猜。
    """
    direction = _canonical_direction(rc.推荐方向)
    if direction:
        return direction, "Writer 结构化推荐方向"
    conclusion = str(rc.核心结论 or "")
    patterns = (
        r"(?:方向|走势)\s*(?:倾向|为|是)?\s*(看涨|看跌|震荡|中性)",
        r"(?:呈现|维持)\s*(?:[^。；，]{0,16})?(看涨|看跌|震荡|中性)(?:格局|态势|走势)?",
    )
    for pattern in patterns:
        matches = re.findall(pattern, conclusion)
        if matches:
            direction = _canonical_direction(matches[-1])
            if direction:
                return direction, "核心结论中的明确方向"
    direction = _canonical_direction(getattr(getattr(ma, "plan", None), "整体方向", ""))
    if direction:
        return direction, "研究计划整体方向"
    return "", ""


def build(ma: MarketAnalysis, rc: ReportContent, *, include_underlying: bool = True) -> ViewPackage:
    """把已生成的报告组装成观点包。纯读取已有数据，不产生新数字、不调用 LLM。"""
    if not ma.ok or not rc.ok:
        return ViewPackage(标的代码=getattr(ma, "rep_code", ""), ok=False,
                           error="报告未成功生成，观点包无意义")

    from . import instruments as ins
    from . import thesis as th

    lib = th.load_library()
    sector = ma.field_values.get("__sector__") or ""

    # 挂钩标的取板块 ETF，不是数据阶段的代表个股。报告分析的是整个板块，
    # 把观点绑到贵州茅台身上，做出来的结构承担的是茅台的个股风险而非板块风险。
    #
    # #73 起，优先读 `field_values["__etf__"]`——那是取数阶段（`fetch_profile`）
    # 已经过流动性校验的同一个结果：如果这份报告的波动率/涨跌幅等字段本就取自
    # 这只 ETF 自己的价格数据（`_SECTOR_DERIVED` 路由），观点包这里必须推荐同一只，
    # 否则"分析的东西"和"推荐挂钩的东西"又会变成两个不一致的对象——
    # 正是这次改造要消灭的基差问题。只有旧数据（没有这个键，如反序列化的
    # 历史 pickle）才退回当场重新解析，且那次解析不含流动性校验，仅作兜底。
    confirmed_code = str(getattr(ma, "确认挂钩标的", "") or "")
    etf_code = ma.field_values.get("__etf__")
    if not include_underlying:
        inst, note = None, "挂钩标的待研究完成后由分析师确认"
    elif getattr(ma, "仅研究", False):
        inst, note = None, "分析师选择仅研究；ETF 如有，仅作为行情/成分取数代理，不形成挂钩建议"
    elif confirmed_code:
        inst, note = ins.get(confirmed_code), "分析师本次确认并经数据源校验的挂钩工具"
    elif etf_code:
        inst, note = ins.get(etf_code), ma.field_values.get("__etf_note__") or ""
    elif etf_code is None:              # 键都不存在，说明是改造前的旧数据
        inst, note = ins.underlying_for(sector)
    else:                                # 键存在但为空串：取数阶段已判定无合格ETF
        inst, note = None, ma.field_values.get("__etf_note__") or "无合适挂钩ETF"

    # 板块→ETF 给不出答案时，用择优结果（B4/§9.2②）。这两类需求的分析对象
    # 本身不可交易，挂钩标的必须经映射得出，且映射理由要一并带上——
    # 否则观点包给下游的就是一个"未定"，A1 那节永远空白。
    择优 = getattr(ma, "挂钩择优", None)
    择优理由 = ""
    if include_underlying and inst is None and 择优 is not None and getattr(择优, "picks", None):
        top = 择优.picks[0]
        inst = ins.get(top.代码)
        择优理由 = top.理由
        维度 = "、".join(getattr(择优, "择优维度", []) or [])
        note = (f"择优自 {择优.候选数} 个候选" + (f"（维度：{维度}）" if 维度 else "")
                + ("；另一选项：" + "、".join(
                    f"{k.简称}{('·' + k.适合) if k.适合 else ''}" for k in 择优.picks[1:])
                   if len(择优.picks) > 1 else ""))

    direction, direction_source = _resolve_direction(ma, rc)
    vp = ViewPackage(
        标的代码=inst.代码 if inst else "",
        标的名称=inst.简称 if inst else "",
        标的口径=note,
        挂钩理由=择优理由,
        数据代表标的=f"{getattr(ma, 'rep_name', '') or ''} {ma.rep_code}".strip(),
        板块=sector,
        板块理由=getattr(ma, "板块理由", "") or "",
        整体方向=direction,
        方向来源=direction_source,
    )
    if not include_underlying:
        vp.标的选择说明 = note

    # 观点包只选已取到的、可追溯的市场数据；不读取 writer 的结论，避免把
    # LLM 对产品或条款的表述反向传给 OptionHelper。字段顺序同时决定下游摘要顺序。
    fact_specs = (
        ("PB", "PB"),
        ("PB历史分位", "PB历史分位"),
        ("归母净利同比", "归母净利同比"),
        ("ROE", "ROE"),
        ("板块区间涨跌幅", "板块区间涨跌幅"),
        ("板块资金净流入", "板块资金净流入"),
        ("成交额历史分位", "成交额历史分位"),
    )
    facts_by_field: dict[str, MarketFact] = {}
    for field, label in fact_specs:
        value = ma.field_values.get(field)
        if value is None or not getattr(value, "ok", False) or not getattr(value, "display", ""):
            continue
        fact = MarketFact(label, value.display, value.source, value.as_of)
        vp.市场事实.append(fact)
        facts_by_field[field] = fact

    # 卡片只占一行：优先呈现估值、盈利、资金三类互补状态；缺失时按已有事实补齐。
    card_order = ("PB历史分位", "归母净利同比", "板块资金净流入", "PB", "ROE")
    card_facts = [facts_by_field[field] for field in card_order if field in facts_by_field][:3]
    if card_facts:
        vp.页面市场摘要 = "　｜　".join(f"{item.标签} {item.数值}" for item in card_facts)

    if inst is not None:
        if 择优理由:
            vp.标的选择说明 = 择优理由
        else:
            # 细分主题（如“光模块”）与 ETF 的规范行业（如“通信设备”）通常不完全同名。
            # 报告必须明确：前文研究的是经核验的主题公司篮子；ETF 是把该主题所在
            # 产业链转成可交易价格标的的工具。不能只写一句“主题匹配”，否则读者会误以为
            # 前面的基本面结论来自这只 ETF 的全部成分股。
            theme = str(getattr(ma, "研究主题", "") or sector or "本次")
            scope = str(getattr(ma, "研究篮子口径", "") or "").strip()
            basket = list(getattr(ma, "研究篮子", []) or [])
            if basket:
                preview = "、".join(basket[:3])
                if len(basket) > 3:
                    preview += "等"
                basket_state = str(getattr(ma, "研究篮子状态", "") or "")
                if "核心样本" in basket_state:
                    relationship = (
                        f"前文围绕“{theme}”主题展开，以经核验的核心样本（{preview}）观察；"
                        "样本不足以代表行业整体，未据此输出行业整体基本面结论。"
                    )
                else:
                    relationship = (
                        f"前文围绕“{theme}”主题展开，基本面判断取自经核验的主题公司篮子"
                        f"（{preview}）。"
                    )
                if scope and scope != theme:
                    relationship += (
                        f"{inst.简称}提供“{scope}”的可交易行业表达，覆盖“{theme}”所在产业链；"
                    )
                else:
                    relationship += f"{inst.简称}是该主题的可交易表达工具；"
                vp.标的选择说明 = (
                    relationship + "经分析师确认并通过近20日流动性检查，故作为挂钩标的；"
                    "报价、波动率和情景收益均以该 ETF 自身行情为准。"
                )
            else:
                vp.标的选择说明 = (
                    f"该 ETF 与{theme}研究主题匹配，且已通过取数阶段的流动性检查。"
                )

    # 波动率是期权定价的核心输入（DESIGN §9.4 的"⑧衍生品维度"）。
    # 这两个字段本就在摸底阶段的必查清单里（required_fields 含"年化波动率"/
    # "波动率历史分位"，不依赖哪条论点触发），故这里几乎总能取到。
    vol = ma.field_values.get("年化波动率")
    pctl = ma.field_values.get("波动率历史分位")
    # 若确认的是指数而研究行情取自行业篮子，不能把篮子波动率冒充指数波动率。
    same_price_object = not confirmed_code or confirmed_code == etf_code
    if same_price_object and vol is not None and getattr(vol, "ok", False):
        seg = f"年化波动率 {vol.display}"
        if pctl is not None and getattr(pctl, "ok", False):
            seg += f"，处近3年 {pctl.display}"
        vp.波动率看法 = seg

    # 情景收益带只取挂钩标的自身价格，使用固定状态规则筛历史样本；它描述历史条件分布，
    # 不参与推荐方向，也不将某个历史分位翻译成产品结构。
    if vp.标的代码:
        try:
            from . import scenario_band

            candidate_band = getattr(ma, "情景收益带", None)
            if candidate_band is None or getattr(candidate_band, "code", "") != vp.标的代码:
                candidate_band = scenario_band.calculate(vp.标的代码)
                # 同一份报告会被报价桥接、客户版面和内部底稿各读一次观点包；缓存本次的
                # 纯历史计算，避免重复取同一条 ETF 价格序列。
                ma.情景收益带 = candidate_band
            if candidate_band.ok:
                vp.情景收益带 = candidate_band
        except Exception:
            pass

    risks: list[str] = []
    drivers: list[str] = []
    headwinds: list[str] = []
    windows: list[str] = []
    for lc in rc.logics:
        # 逻辑id 可能是论点库 id（V1/S3/R1…），也可能是研报观点（doc_N）
        # 或自由槽（free_N）——后两者查不到论点库条目，特征留空。这里故意
        # 不读取 lc.结论：它由 writer/LLM 生成，且可能夹带结构或条款建议。
        t = lib.get(lc.逻辑id)
        feat = t.特征 if t else {}
        lv = LogicView(
            逻辑id=lc.逻辑id,
            方向=feat.get("方向", ""),
            强度=feat.get("强度", ""),
            窗口=feat.get("窗口", ""),
            确定性=feat.get("确定性", ""),
            风险点=feat.get("风险点", ""),
        )
        vp.逻辑要点.append(lv)
        if lv.风险点:
            risks.append(lv.风险点)
        if lv.窗口:
            windows.append(lv.窗口)
        # 论点库名称与方向特征来自已触发、已校验的研究规则；它们只说明
        # 未来市场状态的驱动和制约，不推导产品结构。
        direction = lv.方向
        if "看涨" in direction or direction == "涨":
            drivers.append(t.名称 if t else lc.逻辑id)
        elif "看跌" in direction or direction == "跌":
            headwinds.append(t.名称 if t else lc.逻辑id)
    vp.风险提示汇总 = list(dict.fromkeys(risks))   # 去重，保留出现顺序
    vp.市场展望 = MarketOutlook(
        方向=vp.整体方向,
        方向来源=vp.方向来源,
        窗口=list(dict.fromkeys(windows))[:2],
        支持因素=list(dict.fromkeys(drivers))[:3],
        制约因素=list(dict.fromkeys(headwinds))[:3],
        需验证风险=vp.风险提示汇总[:3],
    )

    vp.ok = True
    return vp


def render(vp: ViewPackage) -> str:
    """人可读展示，供测试时核对内容对不对。"""
    if not vp.ok:
        return f"观点包生成失败：{vp.error}"
    lines = [
        f"挂钩标的：{vp.标的名称}（{vp.标的代码}）" if vp.标的代码
        else f"挂钩标的：（未定）{vp.标的口径}",
    ]
    if vp.板块:
        lines.append(f"  板块：{vp.板块}")
    if vp.标的代码 and vp.标的口径:
        lines.append(f"  标的口径：{vp.标的口径}")
    if vp.数据代表标的:
        lines.append(f"  数据代表标的：{vp.数据代表标的}（仅数据锚点，非挂钩对象）")
    if vp.挂钩理由:
        lines.append(f"  挂钩理由：{vp.挂钩理由}")
    if vp.板块理由:
        lines.append(f"  选取依据：{vp.板块理由}")
    lines += [
        f"整体方向：{vp.整体方向 or '—'}",
        f"波动率看法：{vp.波动率看法 or '（未取到）'}",
        f"页面市场摘要：{vp.页面市场摘要 or '（未取到）'}",
        f"标的选择说明：{vp.标的选择说明 or '（未取到）'}",
        "",
        "市场事实：",
    ]
    for fact in vp.市场事实:
        trace = " · ".join(item for item in (fact.来源, fact.截止日) if item)
        lines.append(f"  · {fact.标签}：{fact.数值}" + (f"（{trace}）" if trace else ""))
    if vp.情景收益带 is not None:
        from .scenario_band import render_compact

        lines += ["", "历史相似状态收益带（非预测）：", "  · " + render_compact(vp.情景收益带)]
    outlook = vp.市场展望
    lines += ["", "市场展望：", f"  · 方向：{outlook.方向 or '—'}"]
    if outlook.方向来源:
        lines.append(f"  · 方向依据：{outlook.方向来源}")
    if outlook.窗口:
        lines.append("  · 窗口：" + "、".join(outlook.窗口))
    if outlook.支持因素:
        lines.append("  · 支持因素：" + "、".join(outlook.支持因素))
    if outlook.制约因素:
        lines.append("  · 制约因素：" + "、".join(outlook.制约因素))
    if outlook.需验证风险:
        lines.append("  · 需验证风险：" + "、".join(outlook.需验证风险))
    lines += ["", "逻辑特征："]
    for lv in vp.逻辑要点:
        feat = "、".join(x for x in [lv.方向, lv.强度, lv.窗口, lv.确定性] if x)
        lines.append(f"  · [{lv.逻辑id}] {feat or '（研报观点/自由槽，无论点库特征）'}")
        if lv.风险点:
            lines.append(f"      风险点：{lv.风险点}")
    if vp.风险提示汇总:
        lines.append("")
        lines.append("风险提示汇总：" + "；".join(vp.风险提示汇总))
    return "\n".join(lines)


def to_json(vp: ViewPackage) -> str:
    """机读格式。当前不喂给任何系统——留作日后真正对接 OptionHelper 时
    参考这份 schema 该怎么定，或直接复用。"""
    return json.dumps(dataclasses.asdict(vp), ensure_ascii=False, indent=2)


if __name__ == "__main__":  # python -m core.viewpoint <代码> [板块]
    import sys

    from . import genres as gr, pipeline, writer

    code = sys.argv[1] if len(sys.argv) > 1 else "600030.SH"
    sector = sys.argv[2] if len(sys.argv) > 2 else "证券"
    ma = pipeline.run("板块投资机会测试", gr.TYPE_SECTOR, code, sector=sector)
    if not ma.ok:
        print(f"生成失败：{ma.error}")
        sys.exit(1)
    rc = writer.write(ma)
    vp = build(ma, rc)
    print(render(vp))
    print()
    print("=== JSON（供参考，未对接任何系统）===")
    print(to_json(vp))
