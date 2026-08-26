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
_ETF_CODE_RE = re.compile(r"\b\d{6}\.(?:SH|SZ)\b", re.I)
_COMMODITY_RE = re.compile(r"黄金|白银|原油|贵金属|商品|铜|豆粕|农产品", re.I)


@dataclass
class Confirmation:
    market: str
    research_scope: str
    underlying_code: str = ""
    underlying_name: str = ""
    research_only: bool = False
    reason: str = ""                 # 选填，仅留痕
    research_theme: str = ""         # 细分研究主题；放在末尾以兼容旧位置参数

    @property
    def industries(self) -> list[str]:
        return [x.strip() for x in _SPLIT_RE.split(self.research_scope or "") if x.strip()]

    @property
    def theme(self) -> str:
        """主题为空时兼容旧确认 JSON；新界面会要求分析师明确确认它。"""
        return self.research_theme.strip() or self.research_scope.strip()


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
        research_theme=str(raw.get("research_theme") or "").strip(),
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
    """高风险主题及未点明代码的 ETF 研究必须由分析师确认研究对象。"""
    if getattr(brief, "市场范围", "A股") != "A股":
        return True
    sectors = list(getattr(brief, "涉及板块", []) or [])
    parts = list(getattr(brief, "宽口径成分行业", []) or [])
    if len(sectors) > 1 or len(parts) > 1:
        return True
    raw = str(getattr(brief, "原始需求", "") or "")
    # “黄金 ETF”“机器人 ETF”这类泛称不应被解析器挑出的一只个股或 54 只常用池
    # 静默替代；有明确代码的 ETF 已由代码核验链覆盖，无需重复打断用户。
    generic_etf = "ETF" in raw.upper() and not _ETF_CODE_RE.search(raw)
    # 只要系统需要替客户“选一只 ETF”进入产品路径，就必须让分析师看到候选、理由并确认。
    # 只有用户已在原文中给出明确 ETF 代码时，才允许跳过这一步的选择动作。
    product_route_needs_underlying = bool(getattr(brief, "客户产品诉求", "")) and not _ETF_CODE_RE.search(raw)
    return bool(_HIGH_RISK_RE.search(raw) or _COMMODITY_RE.search(raw) or generic_etf
                or product_route_needs_underlying)


def _normalize_code(value: object) -> str:
    code = str(value or "").strip().upper()
    if re.fullmatch(r"\d{6}", code):
        return code + (".SH" if code.startswith(("5", "6")) else ".SZ")
    return code


def _pick_col(table: dict, *starts: str) -> list | None:
    for start in starts:
        for col, values in table.items():
            if str(col).startswith(start):
                return values if isinstance(values, list) else None
    return None


def _discovery_query(brief) -> str:
    """从需求抽取检索词，而非维护“主题→代码”的第二张白名单。"""
    text = " ".join([str(getattr(brief, "原始需求", "") or ""),
                     str(getattr(brief, "主题", "") or ""),
                     *(getattr(brief, "涉及板块", []) or [])])
    for word in ("光模块", "光通信", "黄金", "白银", "原油", "贵金属", "机器人", "券商", "证券", "医药", "互联网", "半导体", "人工智能"):
        if word in text:
            return word
    return (getattr(brief, "涉及板块", []) or [str(getattr(brief, "主题", "") or "ETF")])[0]


def _proposed_theme(brief) -> str:
    """给确认页一个可编辑的细分研究主题，绝不把宽行业静默当主题。"""
    text = " ".join([str(getattr(brief, "原始需求", "") or ""),
                     str(getattr(brief, "主题", "") or "")])
    for word in ("光模块", "光通信", "黄金", "白银", "原油", "创新药", "券商", "证券",
                 "消费", "汽车电子", "半导体", "人工智能", "算力"):
        if word.lower() in text.lower():
            return word
    return str(getattr(brief, "主题", "") or "").strip()[:40]


def discover_etfs(brief, *, provider: DataProvider | None = None, limit: int = 8) -> list[dict]:
    """动态发现 ETF 候选。

    常用池只用于优先展示；这里通过 iFinD 问财找基金代码和简称，随后仍由 ``verify``
    校验代码、主题暴露和近 20 日流动性。接口不可用时返回空列表，绝不编造代码。
    """
    from .provider import iFinDProvider

    provider = provider or get_provider()
    if not isinstance(provider, iFinDProvider) or not provider.available():
        return []
    try:
        provider._ensure_login()
        import iFinDPy as ths
        query = f"{_discovery_query(brief)} ETF 基金代码 基金简称 基金全称 跟踪指数"
        data = ths.THS_iwencai(query, "fund")
        if data.get("errorcode", -1) != 0:
            return []
        provider.total_data_vol += int(data.get("dataVol", 0) or 0)
        tables = data.get("tables") or []
        table = (tables[0].get("table") or {}) if tables else {}
        codes = _pick_col(table, "基金代码", "证券代码", "代码") or []
        names = _pick_col(table, "基金简称", "证券简称", "基金全称", "简称") or []
    except Exception:
        return []
    # 检索结果不是候选即真相：先以数据源简称、主题词和近 20 日成交额做轻量预检。
    # 正式选择时 ``verify`` 仍会再跑一遍完整核验（因此不会信任界面传回的标签）。
    from . import history
    out: list[dict] = []
    for index, raw_code in enumerate(codes):
        code = _normalize_code(raw_code)
        searched_name = str(names[index] if index < len(names) else "").strip()
        if not _ETF_CODE_RE.fullmatch(code) or not searched_name:
            continue
        if code in {x["code"] for x in out}:
            continue
        basic = provider.get_basic([code], ["ths_stock_short_name_stock"])
        actual_name = str(_first_value(basic, code, "ths_stock_short_name_stock") or "").strip()
        if not getattr(basic, "ok", False) or "ETF" not in actual_name.upper():
            continue
        commodity = bool(_COMMODITY_RE.search(" ".join([
            str(getattr(brief, "原始需求", "") or ""), actual_name,
        ])))
        item = instruments.Instrument(code, actual_name, actual_name,
                                      "商品ETF" if commodity else "行业ETF")
        if not _exposure_matches(str(getattr(brief, "主题", "") or "") + " " +
                                 "、".join(getattr(brief, "涉及板块", []) or []), item, actual_name):
            continue
        try:
            values = history.series(code, "ths_amt_stock", years=1, provider=provider,
                                    drop_nonpositive=False)
            recent = [float(x) for x in values[-20:] if isinstance(x, (int, float))]
        except Exception:
            continue
        if len(recent) < 10:
            continue
        amount = sum(recent) / len(recent)
        if amount < 1e8:
            continue
        out.append({"code": code, "name": actual_name, "type": item.类型,
                    "note": f"iFinD 动态发现并预校验，近20日日均成交额 {amount / 1e8:.2f} 亿元",
                    "origin": "动态发现"})
        if len(out) >= limit:
            break
    return out


def _suggestions(brief, *, provider: DataProvider | None = None) -> list[dict]:
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
                        "note": item.说明, "origin": "常用池"})
    known = {item["code"] for item in out}
    for item in discover_etfs(brief, provider=provider):
        if item["code"] not in known:
            out.append(item)
            known.add(item["code"])
    return out


def proposal(brief, *, provider: DataProvider | None = None) -> dict:
    sectors = list(getattr(brief, "涉及板块", []) or [])
    parts = list(getattr(brief, "宽口径成分行业", []) or [])
    raw = str(getattr(brief, "原始需求", "") or "")
    if not parts and "汽车电子" in raw:
        parts = ["汽车", "电子"]
    elif not parts and re.search(r"AI\s*算力|算力产业链", raw, re.I):
        parts = ["半导体", "通信设备", "计算机设备", "元件"]
    suggested = _suggestions(brief, provider=provider)
    dynamic_needed = bool("ETF" in raw.upper() and not _ETF_CODE_RE.search(raw))
    discovery_note = ""
    if dynamic_needed and not any(x.get("origin") == "动态发现" for x in suggested):
        discovery_note = ("iFinD 动态基金检索暂未返回候选；可直接输入 ETF 代码，系统会继续核验"
                          "证券真实性、主题暴露和流动性。请同时检查 iFinD 凭证。")
    return {
        "original_market": getattr(brief, "市场范围", "A股"),
        "topic": getattr(brief, "主题", ""),
        "proposed_theme": _proposed_theme(brief),
        "proposed_scope": "、".join(parts or sectors),
        "proposed_sectors": sectors,
        "proposed_industries": parts,
        "reason": getattr(brief, "板块理由", ""),
        "suggested_instruments": suggested,
        "discovery_notice": discovery_note,
        "notice": "候选包含常用池与 iFinD 动态发现结果；选择或输入代码后，系统才会校验真实性、主题暴露和近20日流动性。未确认不进入研究。",
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
        "黄金": ("黄金", "上海金", "贵金属", "商品"),
        "贵金属": ("黄金", "白银", "贵金属", "上海金", "商品"),
        "原油": ("原油", "能源", "商品"),
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

    commodity_scope = bool(_COMMODITY_RE.search(" ".join([
        value.research_scope, str(getattr(brief, "原始需求", "") or ""),
    ])))
    if value.market == "A股":
        if not commodity_scope:
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
    is_commodity = bool(_COMMODITY_RE.search(" ".join([
        value.research_scope, str(getattr(brief, "原始需求", "") or ""), actual_name,
    ])))
    item = static or instruments.Instrument(
        code, actual_name, actual_name,
        "跨境ETF" if _is_cross_border(None, actual_name) else (
            "商品ETF" if is_commodity else ("行业ETF" if "ETF" in actual_name.upper() else "指数")),
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
    brief.研究主题 = value.theme
    brief.研究篮子口径 = value.research_scope
    brief.确认挂钩标的 = value.underlying_code if result.instrument else ""
    brief.确认挂钩标的类型 = result.instrument.类型 if result.instrument else ""
    brief.研究资产类型 = result.instrument.类型 if result.instrument else ""
    if result.instrument:
        rationale = (f"分析师确认挂钩 {result.instrument.简称}（{result.instrument.代码}）："
                     f"已核验与研究主题“{value.theme}”的暴露及近20日流动性")
        if result.average_daily_amount is not None:
            rationale += f"（日均成交额 {result.average_daily_amount / 1e8:.2f} 亿元）"
        # 分析师自行填写的理由优先保留，并补上系统实际核验过的事实。
        brief.板块理由 = (value.reason + "；" if value.reason else "") + rationale
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
        # 商品 ETF 是研究对象本身，不存在“拿一只矿业股当数据锚点”的合理口径。
        lead = None if result.instrument and result.instrument.类型 == "商品ETF" else \
            universe.pick_representative(brief.涉及板块[:1], provider=provider or get_provider())
        if lead:
            from .brief import TargetRef
            brief.候选标的 = [TargetRef(lead.简称, lead.代码,
                                       f"ok:{lead.简称}（分析师确认口径后重选）")]
