"""观点包：把已生成的报告内容整理成 OptionHelper 做产品选择要用的输入。

DESIGN §10 定义的观点包结构：{ 标的, 方向, 期限倾向, 波动率看法, 风险偏好 }。

产出写进**内部底稿**（`render/gaps.py` 第二节），不直接喂给 OptionHelper：
方向、期限、波动率看法都会影响报价，必须有人过一眼再用；落盘也留痕，
事后能查当时是按什么观点定的结构。

只读取 `MarketAnalysis`/`ReportContent` 已有的数据重新整理，**不产生任何新数字、
不调用 LLM**——观点包里出现的每个数字，源头都能追回摸底取数或论点库特征列，
与全项目"数字必须可溯源"这条主线一致。

两条边界（#71）：
1. **挂钩标的是板块 ETF，不是数据阶段的代表个股。** 报告分析的是整个板块，
   把观点绑到贵州茅台身上，做出来的结构承担的是茅台的个股风险而非板块风险。
   代表个股仍在 `数据代表标的` 里留一份，供追溯，但明确标注不是挂钩对象。
2. **只描述市场状态，不给产品建议。** 挂什么结构、什么期限、买方还是卖方，
   是 OptionHelper 依据实时波动率曲面与报价决定的事。本系统既没有曲面也没有报价，
   在这里写"卖权收益厚"是无依据的断言，且会让不掌握定价信息的环节替定价环节做决定。
"""

from __future__ import annotations

import dataclasses
import json
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
    市场含义: str = ""   # lc.结论——writer 为下游归结的市场状态判断（方向/时间尺度/波动率/失效条件）


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
    波动率看法: str = ""            # 年化波动率 + 历史分位，拼成一句人可读的话
    逻辑要点: list[LogicView] = dfield(default_factory=list)
    风险提示汇总: list[str] = dfield(default_factory=list)   # 各条风险点去重保序
    ok: bool = False
    error: str = ""


def build(ma: MarketAnalysis, rc: ReportContent) -> ViewPackage:
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
    etf_code = ma.field_values.get("__etf__")
    if etf_code:
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
    if inst is None and 择优 is not None and getattr(择优, "picks", None):
        top = 择优.picks[0]
        inst = ins.get(top.代码)
        择优理由 = top.理由
        维度 = "、".join(getattr(择优, "择优维度", []) or [])
        note = (f"择优自 {择优.候选数} 个候选" + (f"（维度：{维度}）" if 维度 else "")
                + ("；另一选项：" + "、".join(
                    f"{k.简称}{('·' + k.适合) if k.适合 else ''}" for k in 择优.picks[1:])
                   if len(择优.picks) > 1 else ""))

    vp = ViewPackage(
        标的代码=inst.代码 if inst else "",
        标的名称=inst.简称 if inst else "",
        标的口径=note,
        挂钩理由=择优理由,
        数据代表标的=f"{getattr(ma, 'rep_name', '') or ''} {ma.rep_code}".strip(),
        板块=sector,
        板块理由=getattr(ma, "板块理由", "") or "",
        整体方向=rc.推荐方向,
    )

    # 波动率是期权定价的核心输入（DESIGN §9.4 的"⑧衍生品维度"）。
    # 这两个字段本就在摸底阶段的必查清单里（required_fields 含"年化波动率"/
    # "波动率历史分位"，不依赖哪条论点触发），故这里几乎总能取到。
    vol = ma.field_values.get("年化波动率")
    pctl = ma.field_values.get("波动率历史分位")
    if vol is not None and getattr(vol, "ok", False):
        seg = f"年化波动率 {vol.display}"
        if pctl is not None and getattr(pctl, "ok", False):
            seg += f"，处近3年 {pctl.display}"
        vp.波动率看法 = seg

    risks: list[str] = []
    for lc in rc.logics:
        # 逻辑id 可能是论点库 id（V1/S3/R1…），也可能是研报观点（doc_N）
        # 或自由槽（free_N）——后两者查不到论点库条目，特征留空，只保留市场含义。
        t = lib.get(lc.逻辑id)
        feat = t.特征 if t else {}
        lv = LogicView(
            逻辑id=lc.逻辑id,
            方向=feat.get("方向", ""),
            强度=feat.get("强度", ""),
            窗口=feat.get("窗口", ""),
            确定性=feat.get("确定性", ""),
            风险点=feat.get("风险点", ""),
            市场含义=lc.结论,
        )
        vp.逻辑要点.append(lv)
        if lv.风险点:
            risks.append(lv.风险点)
    vp.风险提示汇总 = list(dict.fromkeys(risks))   # 去重，保留出现顺序

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
        "",
        "逻辑要点：",
    ]
    for lv in vp.逻辑要点:
        feat = "、".join(x for x in [lv.方向, lv.强度, lv.窗口, lv.确定性] if x)
        lines.append(f"  · [{lv.逻辑id}] {feat or '（研报观点/自由槽，无论点库特征）'}")
        if lv.市场含义:
            lines.append(f"      市场含义：{lv.市场含义}")
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
