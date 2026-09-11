"""事件证据自动发现：检索负责找原文，LLM 只负责规划与分类。

安全边界：

* 模型记忆不能成为证据；所有候选必须回指本地材料或 HTTPS 原文。
* LLM 返回的引文必须逐字存在于送入模型的材料中，否则直接丢弃。
* 自动发现的候选只有经 GUI 分析师勾选后，才进入 ``事件证据`` 硬门。
* 网络失败只形成诊断，不阻断已有的材料导入和手工录入路径。
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, replace
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote_plus, urlparse

import requests

from core import config
from llm.client import DeepSeekClient


SEARCH_URL = "https://www.bing.com/search?format=rss&q={query}"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_CHARS = 6_000
MAX_LOCAL_DOCUMENTS = 4
MAX_FACT_WEB_DOCUMENTS = 4
MAX_MECHANISM_WEB_DOCUMENTS = 4
MAX_EXPOSURE_WEB_DOCUMENTS = 4
MAX_CHANNEL_RETRY_DOCUMENTS = 4
SUPPORTED_LOCAL_SUFFIXES = {".pdf", ".txt", ".md", ".docx"}
RELATIONS = {"直接竞争", "供应链", "客户需求", "技术替代", "估值情绪映射", "其他"}
EVIDENCE_TYPES = {"事件事实", "产业机制", "A股暴露", "传导证据"}


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    summary: str = ""
    raw_content: str = ""
    provider: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class EvidenceDocument:
    document_id: str
    title: str
    source: str
    reference: str
    text: str
    source_kind: str


@dataclass(frozen=True)
class DiscoveredEvidence:
    evidence_type: str                 # 事件事实 / 产业机制 / A股暴露 / 兼容旧版传导证据
    content: str
    source: str
    link: str
    evidence_id: str = ""
    relation: str = ""
    reason: str = ""
    source_kind: str = ""


@dataclass(frozen=True)
class DiscoveredChain:
    """LLM 只组合已取得的证据 ID；结论不是新的事实来源。"""

    fact_ids: tuple[str, ...]
    mechanism_ids: tuple[str, ...]
    exposure_ids: tuple[str, ...]
    conclusion: str
    direction: str = "不确定"
    confidence: str = "低"
    reason: str = ""


@dataclass
class DiscoveryResult:
    candidates: list[DiscoveredEvidence] = field(default_factory=list)
    chains: list[DiscoveredChain] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    query_groups: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    searched_documents: int = 0
    searched_by_channel: dict[str, int] = field(default_factory=dict)
    search_hits_by_channel: dict[str, int] = field(default_factory=dict)
    fetch_failures_by_channel: dict[str, int] = field(default_factory=dict)
    filtered_low_value_hits: int = 0
    filtered_entity_mismatch_hits: int = 0
    configured_search_provider: str = ""
    search_providers: list[str] = field(default_factory=list)
    search_calls_by_provider: dict[str, int] = field(default_factory=dict)
    search_credits_by_provider: dict[str, float] = field(default_factory=dict)
    search_audit: list[dict] = field(default_factory=list)
    classification_rejections: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidates": [asdict(item) for item in self.candidates],
            "chains": [asdict(item) for item in self.chains],
            "queries": list(self.queries),
            "query_groups": {key: list(values) for key, values in self.query_groups.items()},
            "warnings": list(self.warnings),
            "searched_documents": self.searched_documents,
            "searched_by_channel": dict(self.searched_by_channel),
            "search_hits_by_channel": dict(self.search_hits_by_channel),
            "fetch_failures_by_channel": dict(self.fetch_failures_by_channel),
            "filtered_low_value_hits": self.filtered_low_value_hits,
            "filtered_entity_mismatch_hits": self.filtered_entity_mismatch_hits,
            "configured_search_provider": self.configured_search_provider,
            "search_providers": list(self.search_providers),
            "search_calls_by_provider": dict(self.search_calls_by_provider),
            "search_credits_by_provider": dict(self.search_credits_by_provider),
            "search_audit": list(self.search_audit),
            "classification_rejections": list(self.classification_rejections),
        }


class _VisibleTextParser:
    """小型 HTML 正文提取器；避免为了事件检索增加 BeautifulSoup 依赖。"""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(inner_self) -> None:
                super().__init__()
                inner_self.hidden = 0
                inner_self.parts: list[str] = []

            def handle_starttag(inner_self, tag: str, attrs) -> None:
                if tag.lower() in {"script", "style", "noscript", "svg"}:
                    inner_self.hidden += 1

            def handle_endtag(inner_self, tag: str) -> None:
                if tag.lower() in {"script", "style", "noscript", "svg"} and inner_self.hidden:
                    inner_self.hidden -= 1

            def handle_data(inner_self, data: str) -> None:
                if not inner_self.hidden:
                    value = re.sub(r"\s+", " ", html.unescape(data)).strip()
                    if value:
                        inner_self.parts.append(value)

        self.parser = Parser()

    def feed(self, value: str) -> str:
        self.parser.feed(value)
        return "\n".join(self.parser.parts)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _literal_in(quote: str, text: str) -> bool:
    return bool(quote and _compact(quote) in _compact(text))


def _public_https(url: str) -> bool:
    """拒绝自动抓取本机、内网和非 HTTPS 地址，避免检索结果触发 SSRF。"""
    parsed = urlparse(str(url or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        # DNS/网络不可用由真正的 GET 返回明确诊断；不能因此把合法公网地址误判为恶意。
        return True
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def _is_low_value_hit(hit: SearchHit) -> bool:
    """排除纯行情、走势图和报价页；它们不能证明事件或产业传导。"""
    parsed = urlparse(hit.url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    title = _compact(f"{hit.title} {hit.summary}").lower()
    if "yahoo" in host and any(token in path for token in ("/quote", "/chart", "/finance")):
        return True
    quote_markers = (
        "走势图", "实时行情", "即时行情", "股价行情", "stock quote", "stock price",
        "price chart", "historical data", "报价", "行情中心",
    )
    return any(marker in title for marker in quote_markers)


def _normalized_entity_text(value: object) -> str:
    """实体匹配忽略空格、连字符和标点，但不做语义扩展。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _event_entity_profile(topic: str) -> tuple[list[str], list[str]]:
    """提取需要在事件事实网页中真实出现的主体别名与已知歧义词。

    这里只做高精度实体锚定，不猜产业主题。已知歧义实体维护显式别名，其他
    公司名则从“发布/披露/公布业绩”等事件句式中提取原文名称。
    """
    value = _compact(topic)
    known = (
        (r"(?:SK[\s\-]*海力士|海力士|SK[\s\-]*hynix)",
         ["SK海力士", "SK hynix"], ["SK-II", "SK II", "SKII", "护肤", "化妆品", "美妆"]),
    )
    for pattern, aliases, exclusions in known:
        if re.search(pattern, value, flags=re.IGNORECASE):
            return aliases, exclusions

    patterns = (
        r"(?:针对|关于|结合)?\s*([A-Za-z][A-Za-z0-9 .&·\-]{1,28}|[\u4e00-\u9fffA-Za-z0-9&·\-]{2,18})"
        r"(?:发布|公布|披露|宣布|发了|交出|上调|下调)(?:了|其|最新|本次|季度|年度|半年度)?(?:业绩|财报|指引|公告|预告|进展)",
        r"(?:针对|关于)?\s*([A-Za-z][A-Za-z0-9 .&·\-]{1,28}|[\u4e00-\u9fffA-Za-z0-9&·\-]{2,18})"
        r"(?:业绩|财报|指引|公告|预告)(?:发布|公布|披露|落地)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        entity = _compact(match.group(1))
        entity = re.sub(r"^(?:请问|请分析|分析|市场关注)", "", entity).strip()
        if 2 <= len(entity) <= 30:
            return [entity], []
    return [], []


def _anchor_event_queries(queries: Iterable[str], topic: str) -> list[str]:
    """把事实查询逐条绑定到事件主体；多别名轮流使用以覆盖中英文官方来源。"""
    aliases, _exclusions = _event_entity_profile(topic)
    values = _unique_queries(queries, limit=3)
    if not aliases:
        return values
    anchored: list[str] = []
    for index, query in enumerate(values):
        alias = aliases[index % len(aliases)]
        # 已有同一精确短语时不重复添加；引号使搜索入口减少同形品牌误召回。
        prefix = "" if f'"{alias}"'.lower() in query.lower() else f'"{alias}" '
        # 不把已知歧义词拼进查询；部分语义搜索会忽略减号，反而提高歧义词召回。
        # 排除由读取前的实体硬门完成。
        anchored.append(_compact(f"{prefix}{query}"))
    return anchored


def _hit_matches_event_entity(hit: SearchHit, aliases: Iterable[str]) -> bool:
    haystack = _normalized_entity_text(
        f"{hit.title}\n{hit.summary}\n{hit.raw_content[:2000]}"
    )
    return any(
        normalized and normalized in haystack
        for normalized in (_normalized_entity_text(alias) for alias in aliases)
    )


def _fallback_query_groups(topic: str, research_context: dict | None = None) -> dict[str, list[str]]:
    compact = _compact(topic)[:70]
    context = research_context or {}
    theme = _compact(context.get("research_theme") or "")[:40]
    basket_queries: list[str] = []
    for item in (context.get("theme_basket") or [])[:3]:
        if not isinstance(item, dict):
            continue
        label = _compact(f"{item.get('name') or ''} {item.get('code') or ''}")
        if label:
            basket_queries.append(f"{label} {theme} 主营业务 年报 产品收入")
    underlying = _compact(
        f"{context.get('underlying_name') or ''} {context.get('underlying_code') or ''}"
    )
    tracking = _compact(context.get("tracking_index") or "")
    scope = _compact(context.get("research_scope") or "")
    if basket_queries:
        exposure_queries = basket_queries
    elif underlying or tracking:
        exposure_queries = [
            f"{underlying} {tracking} 跟踪指数 编制方案 成分股 官方",
            f"{tracking or underlying} 主要成分 基金公告",
        ]
    elif scope:
        exposure_queries = [
            f"{scope} {theme} 行业成分 主营业务 数据源",
            f"{scope} {theme} A股 公司 年报 产品收入",
        ]
    else:
        exposure_queries = [
            f"{compact} A股 公司 主营业务 官方",
            f"{compact} ETF 跟踪指数 成分股 官方",
        ]
    return {
        "事件事实": _anchor_event_queries([
            f"{compact} 官方公告 业绩 指引",
            f"{compact} 产品 产能 研发进展 官方披露",
        ], topic),
        "产业机制": [
            f"{compact} 产业链 供应链 需求 竞争机制 行业研究",
            f"{compact} 产品价格 产能 技术替代 行业影响 权威媒体",
        ],
        "A股暴露": exposure_queries,
    }


def _unique_queries(values: Iterable[object], *, limit: int) -> list[str]:
    out: list[str] = []
    for value in values:
        query = _compact(value)
        if 4 <= len(query) <= 100 and query not in out:
            out.append(query)
        if len(out) >= limit:
            break
    return out


def _fallback_queries(topic: str) -> list[str]:
    """保留旧入口，供调用方或外部脚本取得扁平化的检索词。"""
    groups = _fallback_query_groups(topic)
    return [*groups["事件事实"], *groups["产业机制"], *groups["A股暴露"]]


def plan_query_groups(topic: str, client: DeepSeekClient | None = None, *,
                      research_context: dict | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """分别规划事实、产业机制和 A 股暴露检索词。"""
    client = client or DeepSeekClient()
    fallback = _fallback_query_groups(topic, research_context)
    if not client.available():
        return fallback, ["未配置 DeepSeek，已使用规则生成三路检索词；候选仍需分析师核对。"]
    prompt = {
        "任务": "为事件影响研究分别生成三类公开资料检索词；此阶段禁止回答问题或生成事实",
        "客户需求": topic,
        "已确认A股研究对象": research_context or {},
        "要求": [
            "事件事实检索词：2至3条，寻找事件主体的官方披露、公告、业绩实际值、指引或进展",
            "产业机制检索词：2至3条，寻找事件变量如何通过价格、供需、产能、供应链、竞争或技术替代影响某类产业环节",
            "A股暴露检索词：2至3条，寻找本次A股行业、公司或ETF与受影响产业环节的官方业务、指数编制或成分依据",
            "若已提供确认后的A股研究对象，A股暴露检索词必须使用其中的具体行业、公司名称/代码或ETF跟踪指数，不得重新猜研究对象",
            "每条必须是可直接交给搜索引擎的简短关键词组合，不得写成要求模型分析投资机会的完整问句",
            "涉及境外公司时可加入其官方英文名；事件事实优先公司官网、交易所或监管披露；产业机制优先研究机构、行业组织和权威财经媒体；A股暴露优先公司年报、指数公司或基金公告",
            "不得编造基金代码、数据、来源或结论",
            "只返回合法 JSON 对象，不附加解释文字",
        ],
        "返回": {
            "event_fact_queries": ["检索词"],
            "industry_mechanism_queries": ["检索词"],
            "ashare_exposure_queries": ["检索词"],
        },
    }
    response = client.chat_json(
        "你是金融研究检索规划器，只生成检索式，不回答投资问题；只返回合法 JSON 对象。",
        json.dumps(prompt, ensure_ascii=False), max_tokens=900,
    )
    if not response.ok or not isinstance(response.data, dict):
        return fallback, [f"LLM 检索规划失败，改用规则检索词：{response.error or '返回格式无效'}"]
    raw = response.data
    facts = _unique_queries(raw.get("event_fact_queries") or raw.get("事件事实检索词") or [], limit=3)
    mechanisms = _unique_queries(
        raw.get("industry_mechanism_queries") or raw.get("产业机制检索词") or [], limit=3)
    exposures = _unique_queries(
        raw.get("ashare_exposure_queries") or raw.get("A股暴露检索词") or [], limit=3)
    # 兼容升级前接口：旧 transmission_queries 优先作为机制查询，不冒充 A 股暴露证据。
    legacy_links = _unique_queries(raw.get("transmission_queries") or raw.get("传导证据检索词") or [], limit=3)
    if legacy_links and not mechanisms:
        mechanisms = legacy_links
    # 兼容只返回 queries 的旧模型接口。
    legacy = _unique_queries(raw.get("queries") or [], limit=6)
    if legacy and not facts and not mechanisms and not exposures:
        facts, mechanisms, exposures = legacy[:2], legacy[2:4], legacy[4:]
    if not facts:
        facts = fallback["事件事实"]
    if not mechanisms:
        mechanisms = fallback["产业机制"]
    if not exposures:
        exposures = fallback["A股暴露"]
    return {
        "事件事实": _anchor_event_queries(facts[:3], topic),
        "产业机制": mechanisms[:3], "A股暴露": exposures[:3],
    }, []


def plan_queries(topic: str, client: DeepSeekClient | None = None) -> tuple[list[str], list[str]]:
    """兼容旧调用方的扁平入口；新流程应使用 ``plan_query_groups``。"""
    groups, warnings = plan_query_groups(topic, client)
    return _unique_queries(
        [*groups["事件事实"], *groups["产业机制"], *groups["A股暴露"]], limit=8), warnings


def search_web(query: str, *, limit: int = 6, session=requests) -> list[SearchHit]:
    """使用无需额外密钥的公开 RSS 搜索入口；失败时由上层保留诊断。"""
    response = session.get(
        SEARCH_URL.format(query=quote_plus(query)),
        timeout=18,
        headers={"User-Agent": "Mozilla/5.0 ResearchHelper/1.0"},
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    hits: list[SearchHit] = []
    for item in root.findall(".//item"):
        title = _compact(item.findtext("title") or "")
        url = _compact(item.findtext("link") or "")
        summary = _compact(item.findtext("description") or "")
        hit = SearchHit(title=title, url=url, summary=summary)
        # 页面质量由调用通道结合当前任务统一判定并记录过滤数量；搜索层仅保证可回查 HTTPS。
        if title and _public_https(url):
            hits.append(hit)
        if len(hits) >= limit:
            break
    return hits


def _usage_credits(raw: object) -> float:
    """兼容 Tavily 不同版本的 usage 结构，只记录额度，不影响搜索结果。"""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, dict):
        return 0.0
    value = raw.get("credits")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("total", "used", "search"):
            nested = value.get(key)
            if isinstance(nested, (int, float)):
                return float(nested)
    return 0.0


def load_search_settings(overrides: dict | None = None) -> dict:
    """返回不含日志副作用的统一搜索设置；API Key 只在内存中使用。"""
    values = {
        "provider": str(config.SEARCH_PROVIDER or "tavily").lower(),
        "api_key": str(config.TAVILY_API_KEY or ""),
        "search_depth": str(config.SEARCH_DEPTH or "basic").lower(),
        "country": str(config.SEARCH_COUNTRY or "").lower(),
        "language": str(config.SEARCH_LANGUAGE or "").lower(),
        "bing_fallback": bool(config.SEARCH_BING_FALLBACK),
    }
    if isinstance(overrides, dict):
        values.update({key: value for key, value in overrides.items() if value is not None})
    values["provider"] = str(values.get("provider") or "tavily").strip().lower()
    if values["provider"] not in {"tavily", "bing"}:
        values["provider"] = "tavily"
    values["search_depth"] = str(values.get("search_depth") or "basic").strip().lower()
    if values["search_depth"] not in {"basic", "advanced"}:
        values["search_depth"] = "basic"
    values["country"] = str(values.get("country") or "").strip().lower()
    values["language"] = str(values.get("language") or "").strip().lower()
    fallback = values.get("bing_fallback", True)
    if isinstance(fallback, str):
        fallback = fallback.strip().lower() not in {"0", "false", "no", "off", "否"}
    values["bing_fallback"] = bool(fallback)
    values["api_key"] = str(values.get("api_key") or "").strip()
    return values


def search_tavily(query: str, *, limit: int = 6, api_key: str = "",
                   search_depth: str = "basic", country: str = "",
                   language: str = "", include_raw_content: bool = True,
                   session=requests) -> tuple[list[SearchHit], dict]:
    """调用 Tavily 官方 REST Search API，并保留其清洗正文与额度信息。"""
    key = str(api_key or "").strip()
    if not key:
        raise RuntimeError("未配置 Tavily API Key")
    payload: dict[str, object] = {
        "query": query,
        "search_depth": search_depth if search_depth in {"basic", "advanced"} else "basic",
        "max_results": max(1, min(20, int(limit))),
        "topic": "general",
        "include_answer": False,
        "include_raw_content": "text" if include_raw_content else False,
        "include_images": False,
        "include_usage": True,
        "safe_search": True,
    }
    if country:
        payload["country"] = country
    if language:
        payload["language"] = language
    response = session.post(
        TAVILY_SEARCH_URL,
        json=payload,
        timeout=35,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        response.raise_for_status()
    except Exception as error:
        status = getattr(response, "status_code", "?")
        detail = _compact(getattr(response, "text", ""))[:240]
        raise RuntimeError(f"Tavily HTTP {status}: {detail or error}") from error
    try:
        data = response.json()
    except (ValueError, AttributeError) as error:
        raise RuntimeError("Tavily 返回的不是合法 JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError("Tavily 返回格式无效")
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = _compact(item.get("title") or "")
        url = _compact(item.get("url") or "")
        if not title or not _public_https(url):
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        hits.append(SearchHit(
            title=title,
            url=url,
            summary=_compact(item.get("content") or ""),
            raw_content=_compact(item.get("raw_content") or "")[:MAX_DOCUMENT_CHARS],
            provider="Tavily",
            score=score,
        ))
        if len(hits) >= limit:
            break
    return hits, {
        "provider": "Tavily",
        "query": query,
        "ok": True,
        "hit_count": len(hits),
        "credits": _usage_credits(data.get("usage")),
        "response_time": data.get("response_time"),
        "error": "",
    }


class UnifiedSearcher:
    """Tavily 主搜索与 Bing RSS 兜底的统一可调用接口。"""

    def __init__(self, settings: dict | None = None, *, session=requests) -> None:
        self.settings = load_search_settings(settings)
        self.session = session
        self._diagnostics: list[dict] = []

    @property
    def display_name(self) -> str:
        if self.settings["provider"] == "bing":
            return "Bing RSS"
        return "Tavily + Bing RSS兜底" if self.settings["bing_fallback"] else "Tavily"

    def take_diagnostics(self) -> list[dict]:
        values, self._diagnostics = self._diagnostics, []
        return values

    def _bing(self, query: str, limit: int) -> list[SearchHit]:
        try:
            hits = [replace(item, provider="Bing RSS")
                    for item in search_web(query, limit=limit, session=self.session)]
            self._diagnostics.append({
                "provider": "Bing RSS", "query": query, "ok": True,
                "hit_count": len(hits), "credits": 0.0, "error": "",
            })
            return hits
        except Exception as error:
            self._diagnostics.append({
                "provider": "Bing RSS", "query": query, "ok": False,
                "hit_count": 0, "credits": 0.0,
                "error": f"{type(error).__name__}: {str(error)[:240]}",
            })
            return []

    def __call__(self, query: str, *, limit: int = 6) -> list[SearchHit]:
        self._diagnostics = []
        if self.settings["provider"] == "bing":
            return self._bing(query, limit)
        try:
            hits, diagnostic = search_tavily(
                query, limit=limit, api_key=self.settings["api_key"],
                search_depth=self.settings["search_depth"],
                country=self.settings["country"], language=self.settings["language"],
                session=self.session,
            )
            self._diagnostics.append(diagnostic)
            if hits or not self.settings["bing_fallback"]:
                return hits
            self._diagnostics[-1]["fallback_reason"] = "Tavily 未返回结果"
        except Exception as error:
            self._diagnostics.append({
                "provider": "Tavily", "query": query, "ok": False,
                "hit_count": 0, "credits": 0.0,
                "error": f"{type(error).__name__}: {str(error)[:240]}",
            })
            if not self.settings["bing_fallback"]:
                return []
            self._diagnostics[-1]["fallback_reason"] = "Tavily 不可用"
        return self._bing(query, limit)


def build_searcher(settings: dict | None = None, *, session=requests) -> UnifiedSearcher:
    return UnifiedSearcher(settings, session=session)


def test_search_connection(settings: dict | None = None, *, session=requests) -> dict:
    """测试当前选中的入口；测试 Tavily 时不允许 Bing 掩盖配置错误。"""
    values = load_search_settings(settings)
    if values["provider"] == "tavily":
        try:
            hits, diagnostic = search_tavily(
                "Research Helper financial research official disclosure",
                limit=1, api_key=values["api_key"], search_depth="basic",
                country=values["country"], language=values["language"],
                include_raw_content=False, session=session,
            )
        except Exception as error:
            return {
                "provider": "Tavily", "ok": False, "hit_count": 0, "credits": 0.0,
                "error": f"{type(error).__name__}: {str(error)[:240]}", "sample_title": "",
            }
        return {**diagnostic, "ok": True, "sample_title": hits[0].title if hits else "未命中样例，但接口已响应"}
    searcher = UnifiedSearcher({**values, "bing_fallback": False}, session=session)
    hits = searcher("Research Helper financial research official disclosure", limit=1)
    diagnostics = searcher.take_diagnostics()
    diagnostic = diagnostics[-1] if diagnostics else {
        "provider": "Bing RSS", "ok": False, "error": "未返回连接诊断",
    }
    return {**diagnostic, "ok": bool(diagnostic.get("ok")),
            "sample_title": hits[0].title if hits else ""}


def _pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:12])
    except Exception:  # malformed/encrypted/scanned PDFs are simply unusable as automatic evidence
        return ""


def fetch_document(hit: SearchHit, *, session=requests) -> EvidenceDocument | None:
    if not _public_https(hit.url) or _is_low_value_hit(hit):
        return None
    # Tavily Search 可直接返回已经清洗的正文；优先使用它，避免再次访问动态网页、
    # 登录墙或易触发反爬的站点。摘要不足 80 字时仍回到原网页抓取路径。
    cleaned = _compact(hit.raw_content)
    if len(cleaned) >= 80:
        return EvidenceDocument(
            document_id="", title=hit.title, source=hit.title, reference=hit.url,
            text=cleaned[:MAX_DOCUMENT_CHARS], source_kind=f"{hit.provider or '搜索服务'}清洗正文",
        )
    response = session.get(
        hit.url, timeout=20, stream=True,
        headers={"User-Agent": "Mozilla/5.0 ResearchHelper/1.0"},
    )
    response.raise_for_status()
    if not _public_https(str(getattr(response, "url", hit.url))):
        return None
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(64 * 1024):
        size += len(chunk)
        if size > MAX_DOCUMENT_BYTES:
            return None
        chunks.append(chunk)
    raw = b"".join(chunks)
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if raw[:4] == b"%PDF" or "application/pdf" in content_type:
        text = _pdf_text(raw)
        kind = "公开PDF"
    elif "html" in content_type or raw.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<HTML")):
        encoding = response.encoding or "utf-8"
        page = raw.decode(encoding, errors="replace")
        text = _VisibleTextParser().feed(page)
        kind = "公开网页"
    else:
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        kind = "公开文本"
    text = _compact(text)
    if len(text) < 80:
        return None
    return EvidenceDocument(
        document_id="", title=hit.title, source=hit.title, reference=hit.url,
        text=text[:MAX_DOCUMENT_CHARS], source_kind=kind,
    )


def local_documents(root: Path, *, topic: str, limit: int = MAX_LOCAL_DOCUMENTS) -> list[EvidenceDocument]:
    """把 sources/ 里的相关逐字片段加入同一候选池，无需再次选择单个文件。"""
    from .material_evidence import extract_candidates

    if not root.is_dir():
        return []
    paths = [path for path in root.rglob("*") if path.suffix.lower() in SUPPORTED_LOCAL_SUFFIXES]
    documents: list[EvidenceDocument] = []
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)[:12]:
        try:
            candidates = extract_candidates(path, query=topic, limit=3, reference=str(path.resolve()))
        except (OSError, RuntimeError, ValueError):
            continue
        for candidate in candidates:
            documents.append(EvidenceDocument(
                document_id="", title=path.name, source=candidate.source,
                reference=candidate.reference, text=candidate.content, source_kind="已上传材料",
            ))
            if len(documents) >= limit:
                return documents
    return documents


def _assign_ids(documents: Iterable[EvidenceDocument]) -> list[EvidenceDocument]:
    out: list[EvidenceDocument] = []
    seen: set[tuple[str, str]] = set()
    for document in documents:
        key = (document.reference, _compact(document.text)[:240])
        if key in seen:
            continue
        seen.add(key)
        out.append(EvidenceDocument(
            document_id=f"D{len(out) + 1}", title=document.title, source=document.source,
            reference=document.reference, text=document.text, source_kind=document.source_kind,
        ))
    return out


def classify_documents(topic: str, documents: list[EvidenceDocument],
                       client: DeepSeekClient | None = None, *,
                       expected_type: str = "", limit: int = 8,
                       rejections: list[dict] | None = None) -> tuple[list[DiscoveredEvidence], list[str]]:
    """LLM 分类后做逐字校验；被改写、无来源或越界的结果一律不返回。"""
    if not documents:
        return [], []
    client = client or DeepSeekClient()
    if not client.available():
        return [], ["已找到原文，但未配置 DeepSeek，无法自动区分事实、产业机制与 A 股暴露。"]
    target_rule = (
        f"本轮只选择「{expected_type}」，任何其他类型均不得返回"
        if expected_type in EVIDENCE_TYPES
        else "可分别选择事件事实、产业机制、A股暴露或直接传导证据"
    )
    payload = {
        "任务": "从材料中挑选可核验的事件事实、产业机制与A股研究对象暴露依据",
        "客户需求": topic,
        "材料": [
            {"document_id": item.document_id, "来源": item.source,
             "链接": item.reference, "原文": item.text}
            for item in documents
        ],
        "规则": [
            "quote必须逐字复制自同一document_id的原文，不得概括、翻译或补充",
            "事件事实只收录已披露的事件、业绩实际值、指引、公告或进展",
            "产业机制说明某项变化如何通过价格、供需、产能、供应链、竞争或技术替代影响某类产业环节；无需在同一句中点名A股标的",
            "A股暴露必须用公司业务、收入构成、指数编制或成分资料证明本次A股行业、公司篮子或ETF暴露于该产业环节",
            "传导证据是兼容类型，仅当同一段原文直接说明事件如何影响本次A股对象时采用",
            "材料只提到相同关键词但没有上述关系时，不得归类",
            target_rule,
            f"正反证据都可保留；最多{limit}条；无合格内容返回空数组",
            "只返回合法 JSON 对象，不附加解释文字",
        ],
        "返回": {"candidates": [{
            "document_id": "D1", "type": "事件事实/产业机制/A股暴露/传导证据", "quote": "逐字原文",
            "relation": "直接竞争/供应链/客户需求/技术替代/估值情绪映射/其他",
            "reason": "为什么与客户问题相关",
        }]},
    }
    response = client.chat_json(
        "你是证据分类器，只能选择输入中的逐字原文；模型常识和推断不得成为证据；只返回合法 JSON 对象。",
        json.dumps(payload, ensure_ascii=False), max_tokens=2600,
    )
    if not response.ok or not isinstance(response.data, dict):
        return [], [f"LLM 证据分类失败：{response.error or '返回格式无效'}"]
    by_id = {item.document_id: item for item in documents}
    candidates: list[DiscoveredEvidence] = []
    raw_candidates = response.data.get("candidates") or []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            if rejections is not None:
                rejections.append({"reason": "LLM候选不是对象", "expected_type": expected_type})
            continue
        document = by_id.get(str(raw.get("document_id") or ""))
        evidence_type = str(raw.get("type") or "").strip()
        quote = _compact(raw.get("quote") or "")
        rejected_reason = ""
        if document is None:
            rejected_reason = "引用的document_id不存在"
        elif evidence_type not in EVIDENCE_TYPES:
            rejected_reason = "证据类型无效"
        elif expected_type and evidence_type != expected_type:
            rejected_reason = f"本通道只接受{expected_type}"
        if rejected_reason:
            if rejections is not None:
                rejections.append({
                    "document_id": str(raw.get("document_id") or ""),
                    "source": document.source if document else "",
                    "returned_type": evidence_type, "expected_type": expected_type,
                    "reason": rejected_reason,
                })
            continue
        if len(quote) < 16 or not _literal_in(quote, document.text):
            if rejections is not None:
                rejections.append({
                    "document_id": document.document_id, "source": document.source,
                    "returned_type": evidence_type, "expected_type": expected_type,
                    "reason": "逐字引文少于16字" if len(quote) < 16 else "引文不在已读取原文中",
                })
            continue
        relation = str(raw.get("relation") or "其他").strip()
        if evidence_type in {"产业机制", "传导证据"} and relation not in RELATIONS:
            relation = "其他"
        if evidence_type in {"事件事实", "A股暴露"}:
            relation = ""
        item = DiscoveredEvidence(
            evidence_type=evidence_type, content=quote, source=document.source,
            link=document.reference, relation=relation,
            reason=_compact(raw.get("reason") or ""), source_kind=document.source_kind,
        )
        duplicate = any(
            old.evidence_type == item.evidence_type and old.content == item.content
            for old in candidates
        )
        if not duplicate:
            candidates.append(item)
        elif rejections is not None:
            rejections.append({
                "document_id": document.document_id, "source": document.source,
                "returned_type": evidence_type, "expected_type": expected_type,
                "reason": "同类逐字引文重复",
            })
    rejected = max(0, len(raw_candidates) - len(candidates)) if isinstance(raw_candidates, list) else 0
    warnings = ([f"LLM 返回的 {rejected} 条候选未通过逐字原文、类型或来源校验，已丢弃。"]
                if rejected else [])
    return candidates[:limit], warnings


def _record_search_diagnostics(result: DiscoveryResult, searcher: object, *,
                               channel: str, query: str,
                               hits: list[SearchHit]) -> list[dict]:
    take = getattr(searcher, "take_diagnostics", None)
    diagnostics = take() if callable(take) else []
    if not diagnostics:
        provider = next((item.provider for item in hits if item.provider), "自定义搜索入口")
        diagnostics = [{
            "provider": provider, "query": query, "ok": True,
            "hit_count": len(hits), "credits": 0.0, "error": "",
        }]
    for raw in diagnostics:
        item = dict(raw)
        provider = str(item.get("provider") or "未知搜索入口")
        item["channel"] = channel
        item["urls"] = [hit.url for hit in hits if (hit.provider or provider) == provider][:10]
        result.search_calls_by_provider[provider] = result.search_calls_by_provider.get(provider, 0) + 1
        try:
            credits = float(item.get("credits") or 0.0)
        except (TypeError, ValueError):
            credits = 0.0
        result.search_credits_by_provider[provider] = (
            result.search_credits_by_provider.get(provider, 0.0) + credits)
        if provider not in result.search_providers:
            result.search_providers.append(provider)
        result.search_audit.append(item)
    return diagnostics


def _collect_web_documents(queries: Iterable[str], *, channel: str, limit: int,
                           searcher: Callable[[str], list[SearchHit]],
                           fetcher: Callable[[SearchHit], EvidenceDocument | None],
                           seen_urls: set[str], result: DiscoveryResult,
                           filtered_urls: set[str] | None = None,
                           progress: Callable[[int, str], None] | None = None,
                           progress_start: int = 0,
                           progress_end: int = 0,
                           required_entity_aliases: Iterable[str] = ()) -> list[EvidenceDocument]:
    """为一个证据通道保留独立原文配额，不能被另一通道或本地材料占满。"""
    queries = list(queries)
    if not queries:
        result.searched_by_channel[channel] = result.searched_by_channel.get(channel, 0)
        return []
    documents: list[EvidenceDocument] = []
    hits_before = result.search_hits_by_channel.get(channel, 0)
    failures_before = result.fetch_failures_by_channel.get(channel, 0)
    required_entity_aliases = tuple(required_entity_aliases)
    span = max(0, progress_end - progress_start)
    for query_index, query in enumerate(queries):
        if span:
            value = progress_start + round(span * query_index / max(1, len(queries)))
            _notify_progress(
                progress, value,
                f"正在检索{channel}（{query_index + 1}/{len(queries)}）：{query[:42]}…",
            )
        try:
            hits = searcher(query)
        except Exception as error:  # noqa: BLE001 - per-query recovery is deliberate
            result.warnings.append(f"{channel}公开检索失败（{query[:36]}）：{type(error).__name__}")
            continue
        diagnostics = _record_search_diagnostics(
            result, searcher, channel=channel, query=query, hits=hits)
        providers = " → ".join(str(item.get("provider") or "未知入口") for item in diagnostics)
        result.search_hits_by_channel[channel] = result.search_hits_by_channel.get(channel, 0) + len(hits)
        if span:
            _notify_progress(
                progress,
                progress_start + round(span * (query_index + 0.2) / max(1, len(queries))),
                f"{channel}：{providers} 命中 {len(hits)} 条，正在读取正文…",
            )
        query_start = progress_start + round(span * query_index / max(1, len(queries)))
        query_end = progress_start + round(span * (query_index + 1) / max(1, len(queries)))
        for hit_index, hit in enumerate(hits):
            if span:
                value = query_start + round(
                    max(0, query_end - query_start) * hit_index / max(1, len(hits)))
                _notify_progress(
                    progress, value,
                    f"正在读取{channel}原文（{hit_index + 1}/{len(hits)}）：{hit.title[:42]}…",
                )
            if hit.url in seen_urls:
                result.search_audit.append({
                    "channel": channel, "provider": hit.provider or "未知入口",
                    "query": query, "url": hit.url, "fetch_status": "跳过",
                    "reason": "同一证据通道内URL重复",
                })
                continue
            if required_entity_aliases and not _hit_matches_event_entity(hit, required_entity_aliases):
                seen_urls.add(hit.url)
                result.filtered_entity_mismatch_hits += 1
                result.search_audit.append({
                    "channel": channel, "provider": hit.provider or "未知入口",
                    "query": query, "url": hit.url, "fetch_status": "淘汰",
                    "reason": "网页标题、摘要和搜索正文均未出现事件主体或其确认别名",
                    "required_entity_aliases": list(required_entity_aliases),
                })
                continue
            seen_urls.add(hit.url)
            if _is_low_value_hit(hit):
                if filtered_urls is None or hit.url not in filtered_urls:
                    result.filtered_low_value_hits += 1
                if filtered_urls is not None:
                    filtered_urls.add(hit.url)
                result.search_audit.append({
                    "channel": channel, "provider": hit.provider or "未知入口",
                    "query": query, "url": hit.url, "fetch_status": "淘汰",
                    "reason": "纯行情、走势图或报价页",
                })
                continue
            try:
                document = fetcher(hit)
            except Exception as error:  # individual sites frequently block crawlers; skip, do not fail the run
                document = None
                fetch_error = f"{type(error).__name__}: {str(error)[:180]}"
            else:
                fetch_error = ""
            if document is not None:
                documents.append(document)
                result.search_audit.append({
                    "channel": channel, "provider": hit.provider or "未知入口",
                    "query": query, "url": hit.url, "fetch_status": "成功",
                    "source_kind": document.source_kind,
                    "chars": len(document.text),
                })
            else:
                result.fetch_failures_by_channel[channel] = (
                    result.fetch_failures_by_channel.get(channel, 0) + 1)
                result.search_audit.append({
                    "channel": channel, "provider": hit.provider or "未知入口",
                    "query": query, "url": hit.url, "fetch_status": "失败",
                    "reason": fetch_error or "正文为空、格式不支持或质量不足",
                })
            if len(documents) >= limit:
                break
        if len(documents) >= limit:
            break
        if span:
            value = progress_start + round(span * (query_index + 1) / max(1, len(queries)))
            _notify_progress(
                progress, value,
                f"{channel}已读取 {len(documents)} 份可回查原文，继续检索…",
            )
    result.searched_by_channel[channel] = result.searched_by_channel.get(channel, 0) + len(documents)
    hit_count = result.search_hits_by_channel.get(channel, 0) - hits_before
    failure_count = result.fetch_failures_by_channel.get(channel, 0) - failures_before
    if hit_count == 0:
        result.warnings.append(f"{channel}检索未返回可回查的 HTTPS 结果。")
    elif not documents:
        result.warnings.append(
            f"{channel}检索返回 {hit_count} 条结果，但没有成功读取正文"
            + (f"（其中 {failure_count} 条抓取或正文质量不合格）" if failure_count else "") + "。"
        )
    if span:
        _notify_progress(
            progress, progress_end,
            f"{channel}检索完成：读取 {len(documents)} 份可回查原文。",
        )
    return documents


def _mechanism_retry_queries(topic: str) -> list[str]:
    compact = _compact(topic)[:70]
    return [
        f"{compact} 行业影响机制 供需 价格 产能 供应链",
        f"{compact} 竞争格局 客户需求 技术替代 产业链",
    ]


def _exposure_retry_queries(topic: str, research_context: dict | None = None) -> list[str]:
    # 沿用已确认对象逐项生成的短检索式，避免把行业、多个公司和 ETF 全部
    # 拼进一个超长问句，降低搜索引擎命中质量。
    return _fallback_query_groups(topic, research_context)["A股暴露"][:2]


def _dedupe_candidates(values: Iterable[DiscoveredEvidence]) -> list[DiscoveredEvidence]:
    out: list[DiscoveredEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in values:
        key = (item.evidence_type, item.source, _compact(item.content))
        if key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _assign_evidence_ids(values: Iterable[DiscoveredEvidence]) -> list[DiscoveredEvidence]:
    prefixes = {"事件事实": "F", "产业机制": "M", "A股暴露": "E", "传导证据": "D"}
    counts: dict[str, int] = {}
    out: list[DiscoveredEvidence] = []
    for item in values:
        prefix = prefixes.get(item.evidence_type, "X")
        counts[prefix] = counts.get(prefix, 0) + 1
        out.append(replace(item, evidence_id=f"{prefix}{counts[prefix]}"))
    return out


def _numeric_tokens(value: str) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?%?", value or ""))


def assemble_inference_chains(topic: str, candidates: list[DiscoveredEvidence],
                              client: DeepSeekClient | None = None,
                              *, limit: int = 3) -> tuple[list[DiscoveredChain], list[str]]:
    """让 LLM 连接已有证据 ID；不允许它产生新的数字或无引用的结论。"""
    facts = [item for item in candidates if item.evidence_type == "事件事实"]
    mechanisms = [item for item in candidates if item.evidence_type == "产业机制"]
    exposures = [item for item in candidates if item.evidence_type == "A股暴露"]
    if not (facts and mechanisms and exposures):
        return [], []
    client = client or DeepSeekClient()
    if not client.available():
        return [], ["三类原文已找到，但未配置 DeepSeek，无法自动组合传导链；可手工补充直接传导证据。"]
    payload = {
        "任务": "只使用给定证据ID，组合事件到A股研究对象的可审核传导链",
        "客户需求": topic,
        "证据": [
            {"id": item.evidence_id, "type": item.evidence_type, "原文": item.content,
             "来源": item.source, "关系": item.relation}
            for item in candidates if item.evidence_type in {"事件事实", "产业机制", "A股暴露"}
        ],
        "规则": [
            "每条链必须至少引用1个事件事实ID、1个产业机制ID和1个A股暴露ID",
            "结论只说明三类证据怎样连接，不得补充输入原文之外的新事实、新数字或确定性预测",
            "弱证据或存在跳跃时降低置信度并在reason中说明",
            f"最多返回{limit}条；无法严谨连接时返回空数组",
            "只返回合法JSON对象",
        ],
        "返回": {"chains": [{
            "fact_ids": ["F1"], "mechanism_ids": ["M1"], "exposure_ids": ["E1"],
            "conclusion": "受控组合结论", "direction": "正向/负向/双向/中性/不确定",
            "confidence": "高/中/低", "reason": "链条边界或风险",
        }]},
    }
    response = client.chat_json(
        "你是证据链编排器，只能引用输入证据ID；不得把模型知识当作来源；只返回合法JSON对象。",
        json.dumps(payload, ensure_ascii=False), max_tokens=1800,
    )
    if not response.ok or not isinstance(response.data, dict):
        return [], [f"LLM 传导链组合失败：{response.error or '返回格式无效'}"]
    by_id = {item.evidence_id: item for item in candidates}
    allowed_directions = {"正向", "负向", "双向", "中性", "不确定"}
    allowed_confidence = {"高", "中", "低"}
    chains: list[DiscoveredChain] = []
    for raw in response.data.get("chains") or []:
        if not isinstance(raw, dict):
            continue
        fact_ids = tuple(str(value) for value in (raw.get("fact_ids") or []))
        mechanism_ids = tuple(str(value) for value in (raw.get("mechanism_ids") or []))
        exposure_ids = tuple(str(value) for value in (raw.get("exposure_ids") or []))
        if not fact_ids or not mechanism_ids or not exposure_ids:
            continue
        if any(value not in by_id or by_id[value].evidence_type != "事件事实" for value in fact_ids):
            continue
        if any(value not in by_id or by_id[value].evidence_type != "产业机制" for value in mechanism_ids):
            continue
        if any(value not in by_id or by_id[value].evidence_type != "A股暴露" for value in exposure_ids):
            continue
        conclusion = _compact(raw.get("conclusion") or "")
        if len(conclusion) < 12:
            continue
        cited_text = " ".join(by_id[value].content for value in (*fact_ids, *mechanism_ids, *exposure_ids))
        if not _numeric_tokens(conclusion).issubset(_numeric_tokens(cited_text)):
            continue
        direction = str(raw.get("direction") or "不确定").strip()
        confidence = str(raw.get("confidence") or "低").strip()
        chains.append(DiscoveredChain(
            fact_ids=fact_ids, mechanism_ids=mechanism_ids, exposure_ids=exposure_ids,
            conclusion=conclusion,
            direction=direction if direction in allowed_directions else "不确定",
            confidence=confidence if confidence in allowed_confidence else "低",
            reason=_compact(raw.get("reason") or ""),
        ))
        if len(chains) >= limit:
            break
    return chains, []


def _seed_candidates(raw: dict | None) -> list[DiscoveredEvidence]:
    """把分析师已确认的前三类原文作为组合链输入，不重新当作搜索结果。"""
    raw = raw or {}
    mapping = (("事件事实", "事件事实", "F"), ("产业机制", "产业机制", "M"),
               ("A股暴露", "A股暴露", "E"), ("传导关系", "传导证据", "D"))
    out: list[DiscoveredEvidence] = []
    for key, evidence_type, prefix in mapping:
        for index, item in enumerate(raw.get(key) or [], 1):
            if not isinstance(item, dict):
                continue
            content, source = _compact(item.get("内容") or ""), _compact(item.get("来源") or "")
            if not content or not source:
                continue
            out.append(DiscoveredEvidence(
                evidence_type=evidence_type, content=content, source=source,
                link=str(item.get("链接") or "").strip(),
                evidence_id=str(item.get("证据ID") or f"{prefix}{index}").strip(),
                relation=str(item.get("关系") or "").strip(),
                reason="分析师此前已确认，供本阶段组合传导链使用", source_kind="已确认事件证据",
            ))
    return out


def _notify_progress(callback: Callable[[int, str], None] | None,
                     value: int, message: str) -> None:
    if callback is None:
        return
    try:
        callback(max(0, min(100, int(value))), message)
    except Exception:
        pass


def discover(topic: str, *, sources_dir: Path | None = None,
             client: DeepSeekClient | None = None,
             searcher: Callable[[str], list[SearchHit]] | None = None,
             fetcher: Callable[[SearchHit], EvidenceDocument | None] | None = None,
             mode: str = "full", research_context: dict | None = None,
             existing_evidence: dict | None = None,
             progress: Callable[[int, str], None] | None = None) -> DiscoveryResult:
    """三路独立寻找事实、产业机制与 A 股暴露，再组合可审核传导链。"""
    result = DiscoveryResult()
    topic = _compact(topic)
    mode = mode if mode in {"foundation", "exposure", "full", "complete"} else "full"
    _notify_progress(progress, 2, "正在解析事件与研究对象…")
    if not topic:
        result.warnings.append("客户需求为空，无法生成事件证据检索词。")
        _notify_progress(progress, 100, "客户需求为空，自动查找已停止。")
        return result
    client = client or DeepSeekClient()
    all_query_groups, planning_warnings = plan_query_groups(
        topic, client, research_context=research_context)
    wanted = ({"事件事实", "产业机制"} if mode == "foundation" else
              {"A股暴露"} if mode == "exposure" else
              {"事件事实", "产业机制", "A股暴露"})
    seeds = _seed_candidates(existing_evidence)
    present_types = {item.evidence_type for item in seeds}
    if mode in {"complete", "exposure"}:
        if "事件事实" in present_types:
            wanted.discard("事件事实")
        if "产业机制" in present_types:
            wanted.discard("产业机制")
        if "A股暴露" in present_types:
            wanted.discard("A股暴露")
    result.query_groups = {key: value for key, value in all_query_groups.items() if key in wanted}
    result.queries = _unique_queries(
        [query for key in ("事件事实", "产业机制", "A股暴露")
         for query in result.query_groups.get(key, [])], limit=8)
    result.warnings.extend(planning_warnings)
    _notify_progress(progress, 10, "检索词规划完成，正在读取已上传材料…")
    local_docs = _assign_ids(local_documents(sources_dir or Path("sources"), topic=topic))
    result.searched_by_channel["已上传材料"] = len(local_docs)

    searcher = searcher or build_searcher()
    fetcher = fetcher or fetch_document
    result.configured_search_provider = str(getattr(searcher, "display_name", "自定义搜索入口"))
    _notify_progress(progress, 12, f"正在使用 {result.configured_search_provider} 检索公开原文…")
    seen_urls: dict[str, set[str]] = {"事件事实": set(), "产业机制": set(), "A股暴露": set()}
    filtered_urls: set[str] = set()
    active_channels = [key for key in ("事件事实", "产业机制", "A股暴露") if key in wanted]
    progress_ranges: dict[str, tuple[int, int]] = {}
    if active_channels:
        width = 56 / len(active_channels)
        for index, key in enumerate(active_channels):
            progress_ranges[key] = (round(12 + width * index), round(12 + width * (index + 1)))

    fact_docs = _assign_ids(_collect_web_documents(
        result.query_groups.get("事件事实", []), channel="事件事实", limit=MAX_FACT_WEB_DOCUMENTS,
        searcher=searcher, fetcher=fetcher, seen_urls=seen_urls["事件事实"], result=result,
        filtered_urls=filtered_urls, progress=progress,
        progress_start=progress_ranges.get("事件事实", (0, 0))[0],
        progress_end=progress_ranges.get("事件事实", (0, 0))[1],
        required_entity_aliases=_event_entity_profile(topic)[0],
    ))
    mechanism_docs = _assign_ids(_collect_web_documents(
        result.query_groups.get("产业机制", []), channel="产业机制", limit=MAX_MECHANISM_WEB_DOCUMENTS,
        searcher=searcher, fetcher=fetcher, seen_urls=seen_urls["产业机制"], result=result,
        filtered_urls=filtered_urls, progress=progress,
        progress_start=progress_ranges.get("产业机制", (0, 0))[0],
        progress_end=progress_ranges.get("产业机制", (0, 0))[1],
    ))
    exposure_docs = _assign_ids(_collect_web_documents(
        result.query_groups.get("A股暴露", []), channel="A股暴露", limit=MAX_EXPOSURE_WEB_DOCUMENTS,
        searcher=searcher, fetcher=fetcher, seen_urls=seen_urls["A股暴露"], result=result,
        filtered_urls=filtered_urls, progress=progress,
        progress_start=progress_ranges.get("A股暴露", (0, 0))[0],
        progress_end=progress_ranges.get("A股暴露", (0, 0))[1],
    ))
    _notify_progress(progress, 70, "原文读取完成，正在分类已上传材料…")

    # 四类输入独立送审：本地材料可命中任意类型，但不挤占三个外部通道的配额。
    local_candidates, local_warnings = classify_documents(
        topic, local_docs, client, limit=6, rejections=result.classification_rejections)
    local_candidates = [item for item in local_candidates if item.evidence_type in wanted]
    _notify_progress(progress, 73, "正在逐字校验事件事实候选…")
    fact_candidates, fact_warnings = classify_documents(
        topic, fact_docs, client, expected_type="事件事实", limit=4,
        rejections=result.classification_rejections)
    _notify_progress(progress, 76, "正在逐字校验产业机制候选…")
    mechanism_candidates, mechanism_warnings = classify_documents(
        topic, mechanism_docs, client, expected_type="产业机制", limit=4,
        rejections=result.classification_rejections)
    _notify_progress(progress, 79, "正在逐字校验A股暴露候选…")
    exposure_candidates, exposure_warnings = classify_documents(
        topic, exposure_docs, client, expected_type="A股暴露", limit=4,
        rejections=result.classification_rejections)
    _notify_progress(progress, 82, "证据分类完成，正在检查是否需要定向补检…")
    result.warnings.extend([
        *local_warnings, *fact_warnings, *mechanism_warnings, *exposure_warnings,
    ])

    if ("产业机制" in wanted
            and not any(item.evidence_type == "产业机制" for item in [*local_candidates, *mechanism_candidates])):
        retry_queries = _mechanism_retry_queries(topic)
        result.query_groups["产业机制补检"] = retry_queries
        result.queries = _unique_queries([*result.queries, *retry_queries], limit=8)
        retry_docs = _assign_ids(_collect_web_documents(
            retry_queries, channel="产业机制补检", limit=MAX_CHANNEL_RETRY_DOCUMENTS,
            searcher=searcher, fetcher=fetcher, seen_urls=seen_urls["产业机制"], result=result,
            filtered_urls=filtered_urls, progress=progress,
            progress_start=82, progress_end=85,
        ))
        retry_candidates, retry_warnings = classify_documents(
            topic, retry_docs, client, expected_type="产业机制", limit=4,
            rejections=result.classification_rejections)
        mechanism_candidates.extend(retry_candidates)
        result.warnings.extend(retry_warnings)

    if ("A股暴露" in wanted
            and not any(item.evidence_type == "A股暴露" for item in [*local_candidates, *exposure_candidates])):
        retry_queries = _exposure_retry_queries(topic, research_context)
        result.query_groups["A股暴露补检"] = retry_queries
        result.queries = _unique_queries([*result.queries, *retry_queries], limit=10)
        retry_docs = _assign_ids(_collect_web_documents(
            retry_queries, channel="A股暴露补检", limit=MAX_CHANNEL_RETRY_DOCUMENTS,
            searcher=searcher, fetcher=fetcher, seen_urls=seen_urls["A股暴露"], result=result,
            filtered_urls=filtered_urls, progress=progress,
            progress_start=85, progress_end=88,
        ))
        retry_candidates, retry_warnings = classify_documents(
            topic, retry_docs, client, expected_type="A股暴露", limit=4,
            rejections=result.classification_rejections)
        exposure_candidates.extend(retry_candidates)
        result.warnings.extend(retry_warnings)

    new_candidates = _assign_evidence_ids(_dedupe_candidates([
        *local_candidates, *fact_candidates, *mechanism_candidates, *exposure_candidates,
    ])[:16])
    # 已确认条目保留原ID；新条目与其冲突时重新编号，供链条稳定引用。
    used = {item.evidence_id for item in seeds}
    merged_new: list[DiscoveredEvidence] = []
    for item in new_candidates:
        prefix = {"事件事实": "F", "产业机制": "M", "A股暴露": "E", "传导证据": "D"}.get(item.evidence_type, "X")
        serial = 1
        evidence_id = item.evidence_id
        while not evidence_id or evidence_id in used:
            evidence_id = f"{prefix}{serial}"; serial += 1
        used.add(evidence_id)
        merged_new.append(replace(item, evidence_id=evidence_id))
    result.candidates = _dedupe_candidates([*seeds, *merged_new])[:20]
    _notify_progress(progress, 90, "证据分类完成，正在组合可审核传导链…")
    result.chains, chain_warnings = assemble_inference_chains(topic, result.candidates, client)
    result.warnings.extend(chain_warnings)
    result.searched_documents = sum(result.searched_by_channel.values())
    fact_count = sum(item.evidence_type == "事件事实" for item in result.candidates)
    mechanism_count = sum(item.evidence_type == "产业机制" for item in result.candidates)
    exposure_count = sum(item.evidence_type == "A股暴露" for item in result.candidates)
    direct_count = sum(item.evidence_type == "传导证据" for item in result.candidates)
    if "事件事实" in wanted and not fact_count:
        result.warnings.append("未找到可直接确认的事件事实：可上传官方披露、公告或手工补录。")
    if "产业机制" in wanted and not mechanism_count and not direct_count:
        result.warnings.append(
            "未找到可确认的产业机制：已完成定向补检；请补充事件变量如何影响相关产业环节的原文。"
        )
    if "A股暴露" in wanted and not exposure_count and not direct_count:
        result.warnings.append(
            "未找到可确认的A股暴露依据：已完成定向补检；请补充公司业务、指数编制或ETF成分等原文。"
        )
    if mode != "foundation" and fact_count and mechanism_count and exposure_count and not result.chains:
        result.warnings.append("三类证据均有候选，但未形成通过引用校验的组合传导链；请人工复核或补充直接传导证据。")
    if result.filtered_low_value_hits:
        result.warnings.append(f"已过滤 {result.filtered_low_value_hits} 条纯行情/走势图/报价页，它们不能作为事实、机制或暴露证据。")
    if result.filtered_entity_mismatch_hits:
        aliases = " / ".join(_event_entity_profile(topic)[0]) or "已识别事件主体"
        result.warnings.append(
            f"事件事实通道已在读取前过滤 {result.filtered_entity_mismatch_hits} 条主体不匹配结果；"
            f"本轮主体锚点为 {aliases}。")
    tavily_failures = sum(
        item.get("provider") == "Tavily" and not item.get("ok")
        for item in result.search_audit if isinstance(item, dict))
    bing_calls = result.search_calls_by_provider.get("Bing RSS", 0)
    if tavily_failures and bing_calls:
        result.warnings.append(
            f"Tavily 有 {tavily_failures} 次请求不可用或额度不足，系统已自动使用 Bing RSS 兜底；"
            "具体原因和命中网址保留在检索审计中。")
    if not result.candidates:
        result.warnings.append("没有形成可直接确认的候选；可调整客户问题、上传材料，或继续手工录入。")
    _notify_progress(progress, 100, "自动查找完成，等待分析师审核。")
    return result
