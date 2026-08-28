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
# 不能用 ``\b``：Python 把中文也视为 word character，故“512690.SH的”在
# SH 与“的”之间没有词边界，用户最常见的自然语言写法会漏掉明确 ETF 代码。
_ETF_CODE_RE = re.compile(r"(?<![0-9A-Za-z])\d{6}\.(?:SH|SZ)(?![0-9A-Za-z])", re.I)
_COMMODITY_RE = re.compile(r"黄金|白银|原油|贵金属|商品|铜|豆粕|农产品", re.I)
# 问财会返回数百乃至上千条“相关基金”。动态发现只需为人工确认页补足少数候选，
# 绝不能为每一条结果发一次 iFinD 基础数据请求，否则确认页会在报告开始前假死数分钟。
_DISCOVERY_SCAN_LIMIT = 120
_DISCOVERY_BATCH_SIZE = 50
_DISCOVERY_TERM_LIMIT = 5
_PREFERRED_DAILY_AMOUNT = 1e8
_THEME_ETF_MIN_DAILY_AMOUNT = 3e7
_THEME_BASKET_MIN = 5
_THEME_BASKET_MAX = 20

# 这是检索语言，不是“主题→基金代码”白名单。它只补充行业中常见、且业务暴露
# 直接相关的表达；证券事实仍必须由 iFinD 官方简称、跟踪指数和流动性校验。
_ETF_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "汽车电子": ("汽车电子", "智能驾驶", "智能汽车", "车联网", "汽车智能化"),
    "智能驾驶": ("智能驾驶", "智能汽车", "汽车电子", "车联网"),
    "智能汽车": ("智能汽车", "智能驾驶", "汽车电子", "车联网"),
    "光模块": ("光模块", "光通信", "通信设备", "5G通信"),
    "光通信": ("光通信", "光模块", "通信设备", "5G通信"),
    "人工智能": ("人工智能", "AI算力", "算力", "半导体", "通信设备"),
    "算力": ("算力", "AI算力", "人工智能", "半导体", "通信设备"),
    "黄金": ("黄金", "上海金", "贵金属"),
    "创新药": ("创新药", "港股创新药", "医药"),
    "白酒": ("白酒", "酒"),
    "酒": ("白酒", "酒"),
}


@dataclass
class Confirmation:
    market: str
    research_scope: str
    underlying_code: str = ""
    underlying_name: str = ""
    research_only: bool = False
    reason: str = ""                 # 选填，仅留痕
    research_theme: str = ""         # 细分研究主题；放在末尾以兼容旧位置参数
    theme_basket_codes: list[str] = field(default_factory=list)  # 分析师确认的本次主题公司篮子
    theme_basket_confirmed: bool = False
    # industry=按标准行业/人工主题篮子研究；theme_etf=不强迫细分主题冒充标准行业，
    # 直接以已核验主题 ETF 的真实成分作为研究篮子。
    research_mode: str = "industry"

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
        underlying_code=_normalize_code(raw.get("underlying_code")),
        underlying_name=str(raw.get("underlying_name") or "").strip(),
        research_only=bool(raw.get("research_only", False)),
        reason=str(raw.get("reason") or "").strip(),
        theme_basket_codes=[_normalize_code(x) for x in (raw.get("theme_basket_codes") or [])
                            if _normalize_code(x)],
        theme_basket_confirmed="theme_basket_codes" in raw,
        research_mode=str(raw.get("research_mode") or "industry").strip(),
    )


def validate(value: Confirmation) -> list[str]:
    """不访问数据源的结构校验；完整校验请用 :func:`verify`."""
    errors: list[str] = []
    if value.market not in MARKETS:
        errors.append("市场范围必须是 A股、港股或跨市场")
    if value.research_mode not in {"industry", "theme_etf"}:
        errors.append("研究取数路径必须是标准行业或主题 ETF")
    if not value.research_scope.strip():
        errors.append("必须确认研究口径")
    elif _INVALID_SCOPE_RE.fullmatch(value.research_scope.strip()):
        errors.append("研究口径不能只是数字或符号")
    if not value.research_only and not value.underlying_code.strip():
        errors.append("必须选择挂钩标的，或明确选择仅研究")
    if value.research_mode == "theme_etf" and not value.underlying_code.strip():
        errors.append("主题 ETF 取数路径必须选择一只经校验的 ETF")
    return errors


def needs_confirmation(brief) -> bool:
    """高风险主题及未点明代码的 ETF 研究必须由分析师确认研究对象。"""
    if getattr(brief, "市场范围", "A股") != "A股":
        return True
    # 用户已在原始需求中点名、且位于受控目录的 ETF 时，研究对象和挂钩工具
    # 已经明确；后续会以该 ETF 的行情及真实跟踪指数成分取数。此时再弹出
    # “选 ETF + 主题公司篮子”的确认页没有新增决策价值，反而可能把报告标题
    # 当成问财概念股检索词，混进无关公司。
    # 白名单外代码仍须走确认页和数据源核验，绝不因“像 ETF 代码”而放行。
    if _explicit_catalogued_etf(brief):
        return False
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


def _explicit_catalogued_etf(brief) -> str:
    """返回用户原文直接点名的、目录已核验 ETF；否则返回空。"""
    raw = str(getattr(brief, "原始需求", "") or "").upper()
    for match in _ETF_CODE_RE.finditer(raw):
        code = match.group(0).upper()
        item = instruments.get(code)
        if item is not None and "ETF" in item.类型.upper():
            return code
    return ""


def _normalize_code(value: object) -> str:
    code = str(value or "").strip().upper()
    # .SS 是 Bloomberg/Yahoo 常见的上海市场后缀；iFinD 使用 .SH。
    # 同时兼容部分终端的 XSHG/XSHE 写法，避免把格式差异误报成“证券不存在”。
    code = re.sub(r"\.SS$", ".SH", code)
    code = re.sub(r"\.XSHG$", ".SH", code)
    code = re.sub(r"\.XSHE$", ".SZ", code)
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
    for word in ("汽车电子", "智能驾驶", "智能汽车", "光模块", "光通信", "黄金",
                 "白银", "原油", "贵金属", "机器人", "券商", "证券", "创新药",
                 "医药", "互联网", "半导体", "人工智能", "算力", "白酒", "酒"):
        if word in text:
            return word
    return (getattr(brief, "涉及板块", []) or [str(getattr(brief, "主题", "") or "ETF")])[0]


def _discovery_terms(brief) -> tuple[str, ...]:
    """形成有限、可审计的 ETF 检索词，不让 LLM 直接猜证券代码。

    顺序为：需求解析器给出的短主题词 → 确定性同义词 → 原始主题兜底。
    最多执行有限次问财检索，避免确认页因全市场扫描长时间无反馈。
    """
    seeds = [str(x or "").strip() for x in (getattr(brief, "ETF检索词", []) or [])]
    seeds.append(_discovery_query(brief))
    text = " ".join([str(getattr(brief, "原始需求", "") or ""),
                     str(getattr(brief, "主题", "") or ""), *seeds])
    expanded: list[str] = []
    for key, aliases in _ETF_SEARCH_ALIASES.items():
        if key.lower() in text.lower():
            expanded.extend(aliases)
    expanded.extend(seeds)
    out: list[str] = []
    for term in expanded:
        term = str(term or "").strip()
        if not term or len(term) > 20 or _ETF_CODE_RE.search(term):
            continue
        # 允许“AI”等英文主题，但不接受整段需求作为检索词。
        if term not in out:
            out.append(term)
        if len(out) >= _DISCOVERY_TERM_LIMIT:
            break
    return tuple(out or ("ETF",))


def _proposed_theme(brief) -> str:
    """给确认页一个可编辑的细分研究主题。

    只能返回可识别的短主题词，绝不能把“酒ETF市场走势与投资机会分析”之类
    的整句标题塞给问财做概念股检索。
    """
    text = " ".join([str(getattr(brief, "原始需求", "") or ""),
                     str(getattr(brief, "主题", "") or "")])
    for word in ("汽车电子", "智能驾驶", "智能汽车", "光模块", "光通信", "黄金",
                 "白银", "原油", "创新药", "券商", "证券", "白酒", "酒", "消费",
                 "半导体", "人工智能", "算力"):
        if word.lower() in text.lower():
            return word
    return ""


def _discovery_keywords(brief) -> tuple[str, ...]:
    """用于给问财返回的基金行排序/预筛，不把它当成最终主题暴露证明。"""
    terms = list(_discovery_terms(brief))
    # 排序词可比实际请求略宽，但不能宽到“科技/电子”这种几乎什么都能命中的词。
    extras = {
        "创新药": ("医疗",),
        "黄金": ("上海金", "贵金属"),
        "光模块": ("光通信", "通信设备", "通信"),
        "光通信": ("光模块", "通信设备", "通信"),
    }
    for key, values in extras.items():
        if any(key in term for term in terms):
            terms.extend(values)
    return tuple(dict.fromkeys(term for term in terms if term))


def _discovery_rows(codes: list, names: list, brief) -> list[tuple[str, str]]:
    """保留问财的相关性顺序，先选名称与主题相关的有限行再做官方代码核验。"""
    seen: set[str] = set()
    rows: list[tuple[str, str, int]] = []
    keywords = tuple(word.lower() for word in _discovery_keywords(brief) if word)
    for index, raw_code in enumerate(codes):
        code = _normalize_code(raw_code)
        name = str(names[index] if index < len(names) else "").strip()
        if not _ETF_CODE_RE.fullmatch(code) or not name or code in seen:
            continue
        seen.add(code)
        lowered = name.lower()
        # 0=直接主题命中，1=同一产业链表达，2=问财相关结果中的兜底项。
        score = next((rank for rank, word in enumerate(keywords) if word in lowered), len(keywords))
        rows.append((code, name, score))
    matched = [row for row in rows if row[2] < len(keywords)]
    # 先按主题命中强度，再保持问财原始排序；没有名字命中时仍保留前若干条，
    # 使诸如“通信设备 ETF”这类名称较宽的工具仍能进入后续正式主题校验。
    ranked = (matched or rows)
    ranked.sort(key=lambda row: row[2])
    return [(code, name) for code, name, _score in ranked[:_DISCOVERY_SCAN_LIMIT]]


def discover_etfs(brief, *, provider: DataProvider | None = None, limit: int = 8) -> list[dict]:
    """动态发现 ETF 候选。

    常用池只用于优先展示；这里通过 iFinD 问财找基金代码和简称，随后仍由 ``verify``
    校验代码、主题暴露和近 20 日流动性。接口不可用时返回空列表，绝不编造代码。
    """
    from .provider import iFinDProvider

    provider = provider or get_provider()
    if not isinstance(provider, iFinDProvider) or not provider.available():
        return []
    merged_rows: list[tuple[str, str]] = []
    seen_rows: set[str] = set()
    search_terms = _discovery_terms(brief)
    try:
        provider._ensure_login()
        import iFinDPy as ths
    except Exception:
        return []
    for term in search_terms:
        try:
            query = f"{term} ETF 基金代码 基金简称 基金全称 跟踪指数"
            data = ths.THS_iwencai(query, "fund")
            if data.get("errorcode", -1) != 0:
                continue
            provider.total_data_vol += int(data.get("dataVol", 0) or 0)
            tables = data.get("tables") or []
            table = (tables[0].get("table") or {}) if tables else {}
            codes = _pick_col(table, "基金代码", "证券代码", "代码") or []
            names = _pick_col(table, "基金简称", "证券简称", "基金全称", "简称") or []
            for code, name in _discovery_rows(codes, names, brief):
                if code not in seen_rows:
                    merged_rows.append((code, name))
                    seen_rows.add(code)
        except Exception:
            # 一个同义词查询失败不能抹掉此前已取得的候选；继续尝试其余受控词。
            continue
    # 检索结果不是候选即真相：先以数据源简称、主题词和近 20 日成交额做轻量预检。
    # 正式选择时 ``verify`` 仍会再跑一遍完整核验（因此不会信任界面传回的标签）。
    # 注意必须批量取官方简称。此前逐只调用会把一次“光模块 ETF”检索变成近千次
    # THS_BasicData 请求；这既不提升准确性，也会让确认页长时间没有任何反馈。
    from . import history
    rows = merged_rows[:_DISCOVERY_SCAN_LIMIT]
    actual_names: dict[str, str] = {}
    tracking_codes: dict[str, str] = {}
    for offset in range(0, len(rows), _DISCOVERY_BATCH_SIZE):
        batch = [code for code, _name in rows[offset:offset + _DISCOVERY_BATCH_SIZE]]
        basic = provider.get_basic(batch, ["ths_stock_short_name_stock", "ths_tracking_index_code_fund"])
        if not getattr(basic, "ok", False):
            # 部分账号可能没有跟踪指数批量字段权限；证券真实性校验仍可先完成，
            # 正式提交时会再次单只查询跟踪指数并校验主题暴露。
            basic = provider.get_basic(batch, ["ths_stock_short_name_stock"])
            if not getattr(basic, "ok", False):
                continue
        for code in batch:
            actual = str(_first_value(basic, code, "ths_stock_short_name_stock") or "").strip()
            tracking = str(_first_value(basic, code, "ths_tracking_index_code_fund") or "").strip()
            if actual:
                actual_names[code] = actual
            if tracking:
                tracking_codes[code] = tracking

    tracking_names: dict[str, str] = {}
    unique_tracking = list(dict.fromkeys(tracking_codes.values()))
    for offset in range(0, len(unique_tracking), _DISCOVERY_BATCH_SIZE):
        batch = unique_tracking[offset:offset + _DISCOVERY_BATCH_SIZE]
        basic = provider.get_basic(batch, ["ths_stock_short_name_stock"])
        if not getattr(basic, "ok", False):
            continue
        for code in batch:
            name = str(_first_value(basic, code, "ths_stock_short_name_stock") or "").strip()
            if name:
                tracking_names[code] = name

    out: list[dict] = []
    exposure_scope = " ".join([
        str(getattr(brief, "主题", "") or ""),
        "、".join(getattr(brief, "涉及板块", []) or []),
        str(getattr(brief, "原始需求", "") or ""),
    ])
    for code, _searched_name in rows:
        actual_name = actual_names.get(code, "")
        if "ETF" not in actual_name.upper():
            continue
        tracking_code = tracking_codes.get(code, "")
        tracking_name = tracking_names.get(tracking_code, "")
        commodity = bool(_COMMODITY_RE.search(" ".join([
            str(getattr(brief, "原始需求", "") or ""), actual_name,
        ])))
        item = instruments.Instrument(code, actual_name, actual_name,
                                      "商品ETF" if commodity else "行业ETF",
                                      [], "本次动态发现", tracking_code)
        evidence_text = " ".join([actual_name, tracking_name, tracking_code])
        if not _exposure_matches(exposure_scope, item, evidence_text):
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
        if amount < _THEME_ETF_MIN_DAILY_AMOUNT:
            continue
        matched = [term for term in search_terms if term.lower() in evidence_text.lower()]
        exposure_note = (f"官方跟踪指数：{tracking_name or tracking_code}" if tracking_code
                         else "官方简称与检索主题匹配")
        liquidity_label = ("优选" if amount >= _PREFERRED_DAILY_AMOUNT else
                           "可选但低于1亿元优选门槛")
        direct_rank = next((rank for rank, term in enumerate(search_terms)
                            if term.lower() in evidence_text.lower()), len(search_terms))
        out.append({"code": code, "name": actual_name, "type": item.类型,
                    "note": (f"iFinD 动态发现；检索命中“{'、'.join(matched) or search_terms[0]}”；"
                             f"{exposure_note}；近20日日均成交额 {amount / 1e8:.2f} 亿元（{liquidity_label}）"),
                    "origin": "动态发现", "average_daily_amount": amount,
                    "_direct_rank": direct_rank})
    # 主题直接命中优先；同等主题相关度下选择流动性更好的工具。内部排序字段不传给 UI。
    out.sort(key=lambda value: (value.get("_direct_rank", 999),
                                -float(value.get("average_daily_amount") or 0)))
    for value in out:
        value.pop("_direct_rank", None)
    return out[:limit]


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
    if "光模块" in text or "光通信" in text:
        # 仅作确认页的起始候选；仍保留 iFinD 动态发现来补充白名单外工具。
        codes += ["515050.SH"]
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
    # 常用池只作起点，候选不足时再从 iFinD 动态扩展；不再每次无差别扫描全量基金。
    remaining = max(0, 8 - len(out))
    if remaining:
        for item in discover_etfs(brief, provider=provider, limit=remaining):
            if item["code"] not in known:
                out.append(item)
                known.add(item["code"])
    return out


def discover_theme_companies(brief, *, provider: DataProvider | None = None,
                             limit: int = _THEME_BASKET_MAX) -> list[dict]:
    """从 iFinD 动态发现细分主题的 A 股公司，供分析师确认研究篮子。

    问财的“概念股”结果只提供候选范围，不直接当成事实：代码与简称仍批量走
    THS_BasicData 核验。它与 ETF 动态发现一样是一次运行内的候选池，不写回人工库。
    """
    from .provider import iFinDProvider

    provider = provider or get_provider()
    if not isinstance(provider, iFinDProvider) or not provider.available():
        return []
    theme = _proposed_theme(brief)
    if not theme:
        return []
    try:
        # 市值排序用于控制概念股噪声；分析师仍可在确认页排除不纯的公司。
        leaders = universe._query_iwencai(
            f"{theme} 概念股 总市值排名前{_THEME_BASKET_MAX} 上市日期", provider)
    except Exception:
        return []
    leaders = [item for item in leaders
               if re.fullmatch(r"(?:00|30|60|68)\d{4}\.(?:SH|SZ)", str(item.代码 or "").upper())]
    if not leaders:
        return []
    leaders = leaders[:limit]
    codes = [item.代码.upper() for item in leaders]
    basic = provider.get_basic(codes, ["ths_stock_short_name_stock"])
    if not getattr(basic, "ok", False):
        return []
    out: list[dict] = []
    for item in leaders:
        code = item.代码.upper()
        actual_name = str(_first_value(basic, code, "ths_stock_short_name_stock") or "").strip()
        if not actual_name or universe._is_fund(code):
            continue
        out.append({
            "code": code,
            "name": actual_name,
            "origin": "iFinD 动态主题发现",
            "note": f"iFinD 问财“{theme} 概念股”候选，代码与简称已核验",
            "market_value": item.总市值,
        })
    return out


def _theme_basket_candidates(brief, *, provider: DataProvider | None = None) -> list[dict]:
    """仅为“细分主题→标准行业”模式生成公司研究篮子。"""
    if _explicit_catalogued_etf(brief):
        return []
    proposed_theme = _proposed_theme(brief)
    proposed_scope = "、".join(getattr(brief, "宽口径成分行业", []) or
                                 getattr(brief, "涉及板块", []) or [])
    scope_parts = {part.strip() for part in _SPLIT_RE.split(proposed_scope) if part.strip()}
    # 普通行业、以及未能明确识别细分主题的需求，直接走行业/ETF 成分口径；
    # 不能把 LLM 给出的候选个股重新贴成“主题核心样本”。
    if not proposed_theme or not proposed_scope or proposed_theme in scope_parts:
        return []
    out: list[dict] = []
    known: set[str] = set()
    for item in (getattr(brief, "候选标的", []) or []):
        code = str(getattr(item, "代码", "") or "").upper()
        if not code or not getattr(item, "可用", False) or universe._is_fund(code) or code in known:
            continue
        out.append({"code": code, "name": item.名称, "origin": "需求解析核心候选",
                    "note": "需求解析识别并已核验的主题相关公司", "core": True})
        known.add(code)

    # 只有细分主题与映射行业不同才扩展。例如“光模块→通信设备”；普通“证券→证券”
    # 继续用行业整体法，不额外伪造一套主题公司池。
    if proposed_theme and proposed_scope and proposed_theme != proposed_scope:
        for item in discover_theme_companies(brief, provider=provider):
            code = str(item.get("code") or "").upper()
            if code and code not in known:
                out.append(item)
                known.add(code)
    return out[:_THEME_BASKET_MAX]


def _scope_options(brief, theme: str) -> list[str]:
    """为确认页生成*系统建议的标准行业*，不把细分主题塞回校验字段。

    ``涉及板块`` 是需求解析结果，常会同时含有“通信设备、光模块”这类
    行业 + 细分主题。旧界面把它们用顿号直接拼成一个可自由编辑的输入框，
    等于要求分析师知道哪一个词是数据源认可的行业名；随后又把同一串文本送
    给严格校验，必然造成“系统自己填、系统自己拒绝”的循环。

    这里的选项只承担“主题暴露/ETF 校验”角色；真正的研究对象仍由
    ``research_theme`` 和主题公司篮子决定。
    """
    from .signals import _THEME_TO_SECTOR

    options: list[str] = []
    normalized_theme = (theme or "").strip()
    # 先采用人工维护的保守主题→行业映射。例如光模块→通信设备。
    mapped_theme = _THEME_TO_SECTOR.get(normalized_theme)
    if mapped_theme:
        options.append(mapped_theme)

    raw_scopes = list(getattr(brief, "宽口径成分行业", []) or
                      getattr(brief, "涉及板块", []) or [])
    for raw in raw_scopes:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        # 细分主题本身不能当“标准行业”再次展示；有已知映射时改成映射结果。
        resolved = _THEME_TO_SECTOR.get(candidate, candidate)
        if candidate == normalized_theme and resolved == candidate:
            continue
        if resolved and resolved not in options:
            options.append(resolved)
    return options


def proposal(brief, *, provider: DataProvider | None = None) -> dict:
    sectors = list(getattr(brief, "涉及板块", []) or [])
    parts = list(getattr(brief, "宽口径成分行业", []) or [])
    raw = str(getattr(brief, "原始需求", "") or "")
    if not parts and "汽车电子" in raw:
        parts = ["汽车", "电子"]
    elif not parts and re.search(r"AI\s*算力|算力产业链", raw, re.I):
        parts = ["半导体", "通信设备", "计算机设备", "元件"]
    suggested = _suggestions(brief, provider=provider)
    basket_candidates = _theme_basket_candidates(brief, provider=provider)
    # 缓存于本次 Brief，后端只接受这份候选池中的代码，不能信任 UI 回传的任意代码。
    from .brief import TargetRef
    brief.主题篮子候选池 = [
        TargetRef(str(item.get("name") or ""), str(item.get("code") or ""), "ok:本次主题候选")
        for item in basket_candidates
    ]
    dynamic_needed = bool(("ETF" in raw.upper() or getattr(brief, "客户产品诉求", ""))
                          and not _ETF_CODE_RE.search(raw))
    discovery_note = ""
    if dynamic_needed and not any(x.get("origin") == "动态发现" for x in suggested):
        discovery_note = ("iFinD 动态基金检索暂未返回候选；可直接输入 ETF 代码，系统会继续核验"
                          "证券真实性、主题暴露和流动性。请同时检查 iFinD 凭证。")
    proposed_theme = _proposed_theme(brief)
    scope_options = _scope_options(brief, proposed_theme)
    # 确认页的“系统建议”不能先展示一个数据源不承认的口径，再让分析师替系统
    # 承担校验失败。除商品主题外，在弹窗出现前先用同一数据源过滤；保留的才是
    # 可提交的 A 股行业选项。
    if scope_options and not _COMMODITY_RE.search(raw):
        try:
            valid, _invalid = universe.validate_industries(scope_options, provider=provider)
            scope_options = valid
        except Exception:
            # 数据源临时不可用时保留映射结果；提交阶段仍会再次校验并给出真实原因。
            pass
    return {
        "original_market": getattr(brief, "市场范围", "A股"),
        "topic": getattr(brief, "主题", ""),
        "proposed_theme": proposed_theme,
        # 兼容 CLI/旧调用方；新 GUI 使用 ``verified_scope_options`` 的 data 值，
        # 而不是让分析师辨认和编辑一串行业/主题混合文本。
        "proposed_scope": "、".join(scope_options),
        "verified_scope_options": scope_options,
        # 细分主题找到了可验证 ETF 时，不再要求分析师先猜一个标准行业。提交后
        # 会以 ETF 官方跟踪指数及真实成分完成研究口径校验与取数。
        "theme_etf_route": bool(proposed_theme),
        "theme_etf_scope": proposed_theme,
        "proposed_sectors": sectors,
        "proposed_industries": parts,
        "theme_basket_candidates": basket_candidates,
        "theme_basket_min": _THEME_BASKET_MIN,
        "theme_basket_max": _THEME_BASKET_MAX,
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
        "汽车电子": ("汽车电子", "智能驾驶", "智能汽车", "车联网", "汽车智能化", "智能车"),
        "智能驾驶": ("智能驾驶", "智能汽车", "汽车电子", "车联网", "智能车"),
        "智能汽车": ("智能汽车", "智能驾驶", "汽车电子", "车联网", "智能车"),
        "光模块": ("光模块", "光通信", "通信设备", "通信", "5g"),
        "光通信": ("光通信", "光模块", "通信设备", "通信", "5g"),
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
        if value.research_mode == "industry" and not commodity_scope:
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

    if brief is not None and value.theme_basket_codes:
        # 主题篮子只能从本次确认页展示、且已核验过代码的候选中选择。这样既允许动态
        # 发现白名单外公司，也不会让 UI 回传任意股票混入整体法聚合。
        pool = list(getattr(brief, "主题篮子候选池", []) or
                    getattr(brief, "候选标的", []) or [])
        allowed = {str(item.代码 or "").upper() for item in pool if getattr(item, "可用", False)}
        invalid = [code for code in value.theme_basket_codes if code not in allowed]
        if invalid:
            result.errors.append("主题研究篮子包含未核验候选：" + "、".join(invalid))

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
    tracking = static.跟踪指数 if static is not None else ""
    if not tracking and "ETF" in actual_name.upper():
        tracking_result = provider.get_basic([code], ["ths_tracking_index_code_fund"])
        if getattr(tracking_result, "ok", False):
            tracking = str(_first_value(tracking_result, code, "ths_tracking_index_code_fund") or "")
    tracking_name = ""
    if tracking:
        tracking_basic = provider.get_basic([tracking], ["ths_stock_short_name_stock"])
        if getattr(tracking_basic, "ok", False):
            tracking_name = str(_first_value(
                tracking_basic, tracking, "ths_stock_short_name_stock") or "").strip()
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
    exposure_text = " ".join([actual_name, tracking, tracking_name])
    scope_match = _exposure_matches(value.research_scope, item, exposure_text)
    theme_match = _exposure_matches(value.theme, item, exposure_text)
    original_match = bool(brief is not None and _exposure_matches(
        str(getattr(brief, "原始需求", "") or ""), item, exposure_text))
    # 单一 A 股行业映射必须与工具本身对口；只有多行业产业链或原市场研究，
    # 才可用原始主题补足“AI→半导体/通信”等跨行业词汇差异。
    exposure_ok = ((theme_match or original_match) if value.research_mode == "theme_etf"
                   else (scope_match or (len(industries) > 1 and original_match)
                         or (value.market != "A股" and original_match)))
    if not exposure_ok:
        target_scope = value.theme if value.research_mode == "theme_etf" else value.research_scope
        result.errors.append(
            f"无法通过 ETF 官方名称或跟踪指数验证 {item.简称} 与研究主题「{target_scope}」具有直接暴露")

    if "ETF" in item.类型.upper():
        from . import history
        values = history.series(code, "ths_amt_stock", years=1, provider=provider,
                                drop_nonpositive=False)
        recent = [float(x) for x in values[-20:] if isinstance(x, (int, float))]
        if len(recent) < 10:
            result.errors.append(f"{code} 成交额样本不足，无法确认流动性")
        else:
            result.average_daily_amount = sum(recent) / len(recent)
            hard_floor = (_THEME_ETF_MIN_DAILY_AMOUNT
                          if value.research_mode == "theme_etf" else min_daily_amount)
            if result.average_daily_amount < hard_floor:
                result.errors.append(
                    f"{code} 近20日日均成交额 {result.average_daily_amount / 1e8:.2f} 亿元，"
                    f"低于 {hard_floor / 1e8:.1f} 亿元最低门槛")
            elif (value.research_mode == "theme_etf"
                  and result.average_daily_amount < min_daily_amount):
                result.warnings.append(
                    f"{code} 近20日日均成交额 {result.average_daily_amount / 1e8:.2f} 亿元，"
                    f"低于 {min_daily_amount / 1e8:.0f} 亿元优选门槛；已按主题 ETF 谨慎档通过，"
                    "正式询价前需结合名义本金复核冲击成本")

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
    # 主题 ETF 路径直接以该 ETF 真实成分作为研究篮子，不再制造一个并不存在的
    # “标准行业”；行业路径仍保留分析师确认行业作审计口径。
    brief.研究篮子口径 = value.theme if value.research_mode == "theme_etf" else value.research_scope
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
    # 主题篮子来自确认页勾选的本次候选池；候选池由解析阶段核心样本 + iFinD 动态
    # 主题发现组成。它们用于“光模块”等细分主题的整体法，不是报价 ETF 成分。
    from .universe import _is_fund
    pool = list(getattr(brief, "主题篮子候选池", []) or
                getattr(brief, "候选标的", []) or [])
    by_code = {str(item.代码 or "").upper(): item for item in pool
               if item.可用 and not _is_fund(item.代码)}
    # 兼容旧 CLI 确认 JSON：未出现该字段时仍沿用原有核心候选。新版 GUI 明确传空
    # 列表则代表分析师不采用主题篮子，不能擅自把全部动态候选塞回去。
    selected_codes = ([] if value.research_mode == "theme_etf" else
                      (value.theme_basket_codes if value.theme_basket_confirmed else list(by_code)))
    brief.主题篮子候选 = [by_code[code] for code in dict.fromkeys(selected_codes)
                       if code in by_code][:_THEME_BASKET_MAX]
    parts = value.industries
    if value.market == "A股":
        if value.research_mode == "theme_etf":
            brief.涉及板块 = [value.theme]
            brief.宽口径成分行业 = []
            brief.候选标的 = []
            return
        # 主题与确认行业不相同（光模块 vs 通信设备）时，研究对象必须仍是主题篮子；
        # 确认行业只用于验证 A 股映射和 ETF 暴露，不能反过来替代研究主题。
        theme_mode = bool(value.theme and value.theme != value.research_scope)
        if theme_mode:
            brief.涉及板块 = [value.theme]
            brief.宽口径成分行业 = []
        elif len(parts) > 1:
            basket_name = (str(getattr(brief, "主题", "") or "").strip() or "分析师确认行业篮子")[:30]
            brief.涉及板块 = [basket_name]
            brief.宽口径成分行业 = universe.register_broad(
                basket_name, parts, basis="分析师本次确认（不写回全局行业库）")
        else:
            brief.涉及板块 = parts
            brief.宽口径成分行业 = []
        brief.候选标的 = []
        # 商品 ETF 是研究对象本身，不存在“拿一只矿业股当数据锚点”的合理口径。
        lead = None if (theme_mode or (result.instrument and result.instrument.类型 == "商品ETF")) else \
            universe.pick_representative(brief.涉及板块[:1], provider=provider or get_provider())
        if theme_mode and brief.主题篮子候选:
            first = brief.主题篮子候选[0]
            lead = type("ThemeLead", (), {"简称": first.名称, "代码": first.代码})()
        if lead:
            from .brief import TargetRef
            brief.候选标的 = [TargetRef(lead.简称, lead.代码,
                                       f"ok:{lead.简称}（分析师确认口径后重选）")]
