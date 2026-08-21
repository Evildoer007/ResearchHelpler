"""高风险研究口径的分析师确认与临时标的校验。

确认结果只在本次进程生效。自由文本不能绕过行业、证券真实性和流动性校验，
也不会自动写回人工维护的行业/ETF 库。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import instruments, universe
from .provider import DataProvider, get_provider

MARKETS = ("A股", "港股", "跨市场")
_SPLIT_RE = re.compile(r"[、,，;/；]+")
_HIGH_RISK_RE = re.compile(r"产业链|智能化|AI\s*算力|人工智能|港股|跨市场", re.I)
_INVALID_SCOPE_RE = re.compile(r"^[\d\W_]+$")


@dataclass
class Confirmation:
    market: str
    research_scope: str
    underlying_code: str = ""
    underlying_name: str = ""
    research_only: bool = False
    reason: str = ""                 # 选填，仅留痕

    @property
    def industries(self) -> list[str]:
        return [x.strip() for x in _SPLIT_RE.split(self.research_scope or "") if x.strip()]


@dataclass
class ValidationResult:
    confirmation: Confirmation
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    instrument: instruments.Instrument | None = None
    average_daily_amount: float | None = None


def from_dict(raw: dict) -> Confirmation:
    return Confirmation(
        market=str(raw.get("market") or "").strip(),
        research_scope=str(raw.get("research_scope") or "").strip(),
        underlying_code=str(raw.get("underlying_code") or "").strip().upper(),
        underlying_name=str(raw.get("underlying_name") or "").strip(),
        research_only=bool(raw.get("research_only", False)),
        reason=str(raw.get("reason") or "").strip(),
    )


def validate(value: Confirmation) -> list[str]:
    """不访问数据源的结构校验；完整校验请用 :func:`verify`."""
    errors: list[str] = []
    if value.market not in MARKETS:
        errors.append("市场范围必须是 A股、港股或跨市场")
    if not value.research_scope.strip():
        errors.append("必须确认研究口径")
    elif _INVALID_SCOPE_RE.fullmatch(value.research_scope.strip()):
        errors.append("研究口径不能只是数字或符号")
    if not value.research_only and not value.underlying_code.strip():
        errors.append("必须选择挂钩标的，或明确选择仅研究")
    return errors


def needs_confirmation(brief) -> bool:
    """港股、跨市场及容易被错误收窄的多行业主题必须人工确认。"""
    if getattr(brief, "市场范围", "A股") != "A股":
        return True
    sectors = list(getattr(brief, "涉及板块", []) or [])
    parts = list(getattr(brief, "宽口径成分行业", []) or [])
    if len(sectors) > 1 or len(parts) > 1:
        return True
    return bool(_HIGH_RISK_RE.search(str(getattr(brief, "原始需求", "") or "")))


def _suggestions(brief) -> list[dict]:
    text = " ".join([
        str(getattr(brief, "原始需求", "") or ""),
        str(getattr(brief, "主题", "") or ""),
        *(getattr(brief, "涉及板块", []) or []),
    ]).lower()
    codes: list[str] = []
    if "互联网" in text:
        codes += ["513050.SH", "513130.SH"]
    if ("创新药" in text or "医药" in text) and getattr(brief, "市场范围", "A股") == "A股":
        codes += ["159992.SZ", "512010.SH"]
    if re.search(r"ai|算力|人工智能", text, re.I):
        codes += ["515980.SH", "515050.SH", "512480.SH"]
    for target in getattr(brief, "候选标的", []) or []:
        code = str(getattr(target, "代码", "") or "").upper()
        if code and getattr(target, "可用", False) and instruments.get(code):
            codes.append(code)
    out = []
    for code in dict.fromkeys(codes):
        item = instruments.get(code)
        if item:
            out.append({"code": item.代码, "name": item.简称, "type": item.类型,
                        "note": item.说明})
    return out


def proposal(brief) -> dict:
    sectors = list(getattr(brief, "涉及板块", []) or [])
    parts = list(getattr(brief, "宽口径成分行业", []) or [])
    raw = str(getattr(brief, "原始需求", "") or "")
    if not parts and "汽车电子" in raw:
        parts = ["汽车", "电子"]
    elif not parts and re.search(r"AI\s*算力|算力产业链", raw, re.I):
        parts = ["半导体", "通信设备", "计算机设备", "元件"]
    return {
        "original_market": getattr(brief, "市场范围", "A股"),
        "topic": getattr(brief, "主题", ""),
        "proposed_scope": "、".join(parts or sectors),
        "proposed_sectors": sectors,
        "proposed_industries": parts,
        "reason": getattr(brief, "板块理由", ""),
        "suggested_instruments": _suggestions(brief),
        "notice": "建议口径仅供人工确认，不会自动采用；自定义标的会先校验真实性、市场、主题暴露与流动性。",
    }


def _first_value(result, code: str, indicator: str):
    data = (getattr(result, "data", {}) or {}).get(code) or {}
    value = data.get(indicator)
    if isinstance(value, list):
        return value[0] if value else None
    if value is not None:
        return value
    return getattr(result, "value", None)


def _is_cross_border(item: instruments.Instrument | None, name: str) -> bool:
    if item and instruments.T_OVERSEA in item.标签:
        return True
    return any(x in name.upper() for x in ("QDII", "恒生", "港股", "中概", "H股"))


def _exposure_matches(scope: str, item: instruments.Instrument, actual_name: str) -> bool:
    """保守的主题暴露校验；不确定就拒绝并让分析师换标的。"""
    text = " ".join([item.简称, item.官方名, *item.标签, item.说明, actual_name]).lower()
    aliases = {
        "互联网": ("互联网", "恒生科技", "中概", "科技"),
        "创新药": ("创新药", "医药", "医疗"),
        "医药": ("医药", "医疗", "创新药"),
        "ai": ("人工智能", "ai", "算力", "半导体", "通信", "科技"),
        "算力": ("人工智能", "ai", "算力", "半导体", "通信", "科技"),
        "汽车电子": ("汽车", "新能源车", "半导体", "通信", "科技"),
        "半导体": ("半导体", "芯片"),
        "通信": ("通信", "5g"),
        "消费": ("消费", "食品", "酒", "家电"),
    }
    scope_lower = scope.lower()
    for key, words in aliases.items():
        if key in scope_lower and any(word.lower() in text for word in words):
            return True
    tokens = [x for x in re.split(r"[\s、,，产业链板块主题]+", scope_lower) if len(x) >= 2]
    return any(token in text for token in tokens)


def verify(value: Confirmation, brief=None, *, provider: DataProvider | None = None,
           min_daily_amount: float = 1e8) -> ValidationResult:
    """执行行业与标的的完整校验，成功时临时注册非白名单工具。"""
    result = ValidationResult(confirmation=value, errors=validate(value))
    if result.errors:
        return result
    provider = provider or get_provider()
    industries = value.industries

    if value.market == "A股":
        to_check = [name for name in industries if not universe.is_broad(name)]
        if to_check:
            _ok, bad = universe.validate_industries(to_check, provider=provider)
            if bad:
                result.errors.append("研究口径不是已验证的 A 股行业：" + "、".join(bad))
    elif brief is not None:
        raw = str(getattr(brief, "原始需求", "") or "")
        topic = str(getattr(brief, "主题", "") or "")
        if not any(name in raw or name in topic for name in industries):
            result.errors.append("港股/跨市场研究口径必须能在原始需求中找到依据")

    if value.reason == "":
        result.warnings.append("未填写映射理由（选填）")
    if value.research_only:
        result.ok = not result.errors
        return result

    code = value.underlying_code
    static = instruments.get(code)
    basic = provider.get_basic([code], ["ths_stock_short_name_stock"])
    actual_name = str(_first_value(basic, code, "ths_stock_short_name_stock") or "").strip()
    if not getattr(basic, "ok", False) or not actual_name:
        result.errors.append(f"证券代码 {code} 无法从数据源验证：{getattr(basic, 'error', '') or '查不到简称'}")
        return result
    if value.underlying_name and static is None:
        expected = re.sub(r"\s+", "", value.underlying_name).lower()
        actual = re.sub(r"\s+", "", actual_name).lower()
        if expected not in actual and actual not in expected:
            result.errors.append(f"代码 {code} 实为「{actual_name}」，与输入名称「{value.underlying_name}」不符")
            return result

    if static is None and not ("ETF" in actual_name.upper() or "指数" in actual_name):
        result.errors.append(f"{actual_name}（{code}）不是可验证的 ETF 或指数")
        return result
    tracking = ""
    if static is None and "ETF" in actual_name.upper():
        tracking_result = provider.get_basic([code], ["ths_tracking_index_code_fund"])
        if getattr(tracking_result, "ok", False):
            tracking = str(_first_value(tracking_result, code, "ths_tracking_index_code_fund") or "")
    item = static or instruments.Instrument(
        code, actual_name, actual_name,
        "跨境ETF" if _is_cross_border(None, actual_name) else ("行业ETF" if "ETF" in actual_name.upper() else "指数"),
        [instruments.T_OVERSEA] if _is_cross_border(None, actual_name) else [],
        "本次运行经数据源验证的临时标的",
        tracking,
    )
    if value.market == "港股" and not (code.endswith(".HK") or _is_cross_border(item, actual_name)):
        result.errors.append("港股研究只能选择港股工具或明确的跨境 ETF，不能静默换成普通 A 股 ETF")
    scope_match = _exposure_matches(value.research_scope, item, actual_name)
    original_match = bool(brief is not None and _exposure_matches(
        str(getattr(brief, "原始需求", "") or ""), item, actual_name))
    # 单一 A 股行业映射必须与工具本身对口；只有多行业产业链或原市场研究，
    # 才可用原始主题补足“AI→半导体/通信”等跨行业词汇差异。
    exposure_ok = scope_match or (len(industries) > 1 and original_match) \
        or (value.market != "A股" and original_match)
    if not exposure_ok:
        result.errors.append(f"无法验证 {item.简称} 与研究口径「{value.research_scope}」具有直接主题暴露")

    if "ETF" in item.类型.upper():
        from . import history
        values = history.series(code, "ths_amt_stock", years=1, provider=provider,
                                drop_nonpositive=False)
        recent = [float(x) for x in values[-20:] if isinstance(x, (int, float))]
        if len(recent) < 10:
            result.errors.append(f"{code} 成交额样本不足，无法确认流动性")
        else:
            result.average_daily_amount = sum(recent) / len(recent)
            if result.average_daily_amount < min_daily_amount:
                result.errors.append(
                    f"{code} 近20日日均成交额 {result.average_daily_amount / 1e8:.2f} 亿元，"
                    f"低于 {min_daily_amount / 1e8:.0f} 亿元门槛")

    if not result.errors:
        if static is None:
            instruments.register_temporary(item)
            result.warnings.append("该标的不在人工白名单中，仅作为本次运行的临时标的")
        result.instrument = item
        result.ok = True
    return result


def apply_to_brief(result: ValidationResult, brief, *, provider: DataProvider | None = None) -> None:
    """把已校验确认应用到本次 Brief；不会修改任何持久化目录。"""
    if not result.ok:
        raise ValueError("不能应用未通过校验的市场确认")
    value = result.confirmation
    brief.市场范围 = value.market
    brief.市场确认 = asdict(value)
    brief.确认挂钩标的 = value.underlying_code if result.instrument else ""
    brief.确认挂钩标的类型 = result.instrument.类型 if result.instrument else ""
    parts = value.industries
    if value.market == "A股":
        if len(parts) > 1:
            basket_name = (str(getattr(brief, "主题", "") or "").strip() or "分析师确认行业篮子")[:30]
            brief.涉及板块 = [basket_name]
            brief.宽口径成分行业 = universe.register_broad(
                basket_name, parts, basis="分析师本次确认（不写回全局行业库）")
        else:
            brief.涉及板块 = parts
            brief.宽口径成分行业 = []
        brief.候选标的 = []
        lead = universe.pick_representative(brief.涉及板块[:1], provider=provider or get_provider())
        if lead:
            from .brief import TargetRef
            brief.候选标的 = [TargetRef(lead.简称, lead.代码,
                                       f"ok:{lead.简称}（分析师确认口径后重选）")]
