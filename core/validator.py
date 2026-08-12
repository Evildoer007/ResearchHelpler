"""validator：数字溯源校验（防幻觉兜底闸门）。

把 writer 成文（核心结论/各逻辑论述/结论/图表数据点）里出现的每个"带单位数字"
抠出来，和 fetcher 取到的真实数据逐个比对：
  - 命中：能在真实数据里找到对应值（允许四舍五入误差）
  - 可疑：找不到对应真值 —— 可能是换算错、转写错或编造，交人工复核

设计取舍（v1）：
  - 只校验"带单位"的数字（%/倍/亿元/万亿元/元…）。真实数据字段都带单位，
    而正文里的年份、序号等无单位数字不是数据，跳过以降噪。
  - 允许值同时收录"格式化串里的数"(如41.50)与"原始值"(如4.15e9)，两种口径都算命中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dfield

from .fetcher import FieldValue

_UNIT = r"万亿元|亿元|万元|万亿|亿|元|倍|%"
_NUM_UNIT = re.compile(rf"(-?\d+(?:\.\d+)?)\s*({_UNIT})")
_ANY_NUM = re.compile(r"-?\d+(?:\.\d+)?")

HIT = "命中"
SUSPECT = "可疑"
DIRECTION = "方向存疑"    # 数值对得上但符号相反，且措辞的方向与真值不符

# 方向词。中文常把符号写进词里——真值 -5.81% 写成"下跌5.81%"是正确表述，
# 但纯数值比对会判不匹配（5.81 ≠ -5.81），造成假阳性。
# 反过来真值 -5.81% 却写成"上涨5.81%"是**方向抄反**，投研里属致命错误，必须单独标出。
_DOWN_WORDS = ("下跌", "跌", "下滑", "回调", "下降", "减少", "净流出", "缩水", "下行", "收窄")
_UP_WORDS = ("上涨", "涨", "增长", "上升", "增加", "净流入", "扩大", "上行", "走高")


@dataclass
class Finding:
    位置: str            # 核心结论 / 逻辑id / 图表:逻辑id
    数字: str            # 原文里的数字串（含单位）
    数值: float
    判定: str            # 命中 / 可疑
    匹配字段: str = ""


@dataclass
class ValidationReport:
    findings: list[Finding] = dfield(default_factory=list)

    @property
    def suspects(self) -> list[Finding]:
        """需人工复核的：无匹配真值 + **方向抄反**。后者更严重，不可漏。"""
        return [f for f in self.findings if f.判定 in (SUSPECT, DIRECTION)]

    @property
    def direction_errors(self) -> list[Finding]:
        return [f for f in self.findings if f.判定 == DIRECTION]

    @property
    def ok(self) -> bool:
        return not self.suspects


def _allowed_values(field_values: dict[str, FieldValue]) -> list[tuple[float, str]]:
    """收集允许出现的真实数值：格式化串里的数 + 原始值，各自带来源字段。"""
    allowed: list[tuple[float, str]] = []
    for name, fv in field_values.items():
        if name.startswith("__") or not getattr(fv, "ok", False):
            continue
        for m in _ANY_NUM.findall(fv.display or ""):
            try:
                allowed.append((float(m), fv.field))
            except ValueError:
                pass
        try:
            allowed.append((float(fv.value), fv.field))
        except (TypeError, ValueError):
            pass
    return allowed


def _match(val: float, allowed: list[tuple[float, str]], *, tol: float = 0.02) -> str | None:
    """在允许值里找与 val 接近的（相对误差≤tol 或绝对差≤0.01），返回来源字段。"""
    for aval, field in allowed:
        if abs(aval) < 1e-9:
            if abs(val) < 1e-9:
                return field
            continue
        if abs(val - aval) / abs(aval) <= tol or abs(val - aval) <= 0.01:
            return field
    return None


def _scan(location: str, text: str, allowed, report: ValidationReport) -> None:
    text = text or ""
    for m in _NUM_UNIT.finditer(text):
        num_str, unit = m.group(1), m.group(2)
        val = float(num_str)
        field = _match(val, allowed)
        verdict = HIT if field else SUSPECT

        # 直接匹配不上时，看是不是"符号写进了词里"（如真值 -5.81%、正文写"下跌5.81%"）
        if not field:
            flipped = _match(-val, allowed)
            if flipped:
                ctx = text[max(0, m.start() - 12):m.start()]      # 数字前的措辞
                said_down = any(w in ctx for w in _DOWN_WORDS)
                said_up = any(w in ctx for w in _UP_WORDS)
                # 真值为负而措辞说"跌"（或真值为正而措辞说"涨"）→ 表述正确，判命中
                if (said_down and -val < 0) or (said_up and -val > 0):
                    field, verdict = flipped, HIT
                elif said_up or said_down:
                    # 措辞方向与真值相反 —— 方向抄反，投研里最危险的一类错
                    field, verdict = flipped, DIRECTION

        report.findings.append(Finding(
            位置=location, 数字=f"{num_str}{unit}", 数值=val,
            判定=verdict, 匹配字段=field or "",
        ))


def validate(report_content, field_values: dict[str, FieldValue]) -> ValidationReport:
    """校验 writer 产出（ReportContent）里的数字是否都能溯源到真实数据。"""
    allowed = _allowed_values(field_values)
    vr = ValidationReport()

    _scan("核心结论", report_content.核心结论, allowed, vr)
    for lc in report_content.logics:
        _scan(lc.逻辑id, f"{lc.论述} {lc.结论}", allowed, vr)
        # 图表数据点也校验（一条逻辑下可能有 1~3 张图，#65）
        for spec in lc.图表规格列表 or []:
            for dp in (spec or {}).get("数据点", []) or []:
                if isinstance(dp, dict):
                    _scan(f"图表:{lc.逻辑id}", str(dp.get("值", "")), allowed, vr)
    return vr


if __name__ == "__main__":  # python -m core.validator
    from . import pipeline, writer
    from . import genres as gr

    ma = pipeline.run("券商板块投资机会", gr.TYPE_SECTOR, "600030.SH")
    rc = writer.write(ma)
    vr = validate(rc, ma.field_values)

    print(f"共校验数字 {len(vr.findings)} 个；可疑 {len(vr.suspects)} 个\n")
    for f in vr.findings:
        mark = "✔" if f.判定 == HIT else "✗可疑"
        print(f"  {mark} [{f.位置}] {f.数字}  ← {f.匹配字段 or '无匹配真值'}")

    # 负例演示：故意塞一个错数（模拟之前 414.98亿 的换算错），看能否抓出
    print("\n--- 负例演示（把 41.50亿 篡改成 414.98亿）---")
    bad = "现金分红总额414.98亿元，A股股息率2.48%"
    vr2 = ValidationReport()
    _scan("测试", bad, _allowed_values(ma.field_values), vr2)
    for f in vr2.findings:
        print(f"  {'✔' if f.判定==HIT else '✗可疑'} {f.数字} ← {f.匹配字段 or '无匹配真值'}")
