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
    inst, note = ins.underlying_for(sector)
    vp = ViewPackage(
        标的代码=inst.代码 if inst else "",
        标的名称=inst.简称 if inst else "",
        标的口径=note,
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
