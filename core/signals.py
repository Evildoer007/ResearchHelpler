"""选题信号采集层。

职责：用 akshare 采集"近期市场在发生什么"的真实信号，供 LLM 归纳候选主题。
本层只负责取真实数据，不做主观判断（"谁热"由数据说话，不由 LLM 瞎编）。

设计原则：
- akshare 接口不稳且常改名，每类信号按"多接口尝试 + 优雅降级"处理。
- 单个接口失败只让该信号缺失，不拖垮整体；结果里带 status 说明拿到了什么。
- 所有网络调用统一用 config.no_proxy() 绕过本地代理。
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import no_proxy


@dataclass
class SignalBundle:
    """一次采集的信号包。data 里每个键是一类信号，errors 记录失败的来源。"""

    date: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        ok = ", ".join(k for k in self.data if self.data[k])
        bad = ", ".join(self.errors)
        return f"[{self.date}] 已采集: {ok or '无'}" + (f" | 缺失: {bad}" if bad else "")


def _safe(bundle: SignalBundle, key: str, fn: Callable[[], Any]) -> None:
    """执行一个采集动作，成功写入 data，失败记入 errors，绝不抛出。"""
    try:
        with no_proxy():
            bundle.data[key] = fn()
    except Exception as e:  # noqa: BLE001 - 采集层要求永不因单点失败中断
        bundle.errors[key] = f"{type(e).__name__}: {str(e)[:120]}"


# ---------- 各类信号的采集函数（返回精简后的 records） ----------

def _industry_movers(top: int = 12) -> list[dict]:
    """行业板块涨跌榜：涨/跌最猛的行业，反映资金关注方向。"""
    import akshare as ak

    df = ak.stock_board_industry_name_em()
    # 列名依东财返回（中文），做健壮映射：按已知语义列取值
    df = df.rename(columns=lambda c: str(c))
    name_col = _pick(df.columns, ["板块名称", "名称"])
    chg_col = _pick(df.columns, ["涨跌幅"])
    turn_col = _pick(df.columns, ["换手率"])
    lead_col = _pick(df.columns, ["领涨股票"])
    df = df[[c for c in [name_col, chg_col, turn_col, lead_col] if c]].dropna()
    df = df.sort_values(chg_col, ascending=False)
    head = df.head(top).to_dict("records")
    tail = df.tail(6).to_dict("records")
    return {"涨幅前列": head, "跌幅前列": tail} if head else []


# 打板情绪类伪概念：不是投资主题，选题时须剔除
_NOISE_KEYWORDS = ("昨日", "涨停", "跌停", "连板", "首板", "炸板", "破净", "融资融券",
                   "标准普尔", "MSCI", "富时", "沪股通", "深股通", "转融券")


def _is_noise(name: str) -> bool:
    return any(k in str(name) for k in _NOISE_KEYWORDS)


def _concept_movers(top: int = 12) -> list[dict]:
    """概念板块涨跌榜：比行业更细的题材热度（已剔除打板情绪类伪概念）。"""
    import akshare as ak

    df = ak.stock_board_concept_name_em()
    name_col = _pick(df.columns, ["板块名称", "名称", "概念名称"])
    chg_col = _pick(df.columns, ["涨跌幅"])
    if not (name_col and chg_col):
        return []
    df = df[[name_col, chg_col]].dropna()
    df = df[~df[name_col].map(_is_noise)]
    df = df.sort_values(chg_col, ascending=False)
    return df.head(top).to_dict("records")


def _sector_fund_flow(period: str = "10日", top: int = 12) -> dict:
    """行业多周期资金流：区间涨跌幅 + 主力净流入。

    选题的核心信号——比单日涨跌有意义得多，能识别"超跌+资金抄底"
    (区间跌但主力净流入) 与"趋势加速"(区间涨且主力净流入) 这类结构性机会。
    """
    import akshare as ak

    df = ak.stock_sector_fund_flow_rank(indicator=period, sector_type="行业资金流")
    name_col = _pick(df.columns, ["名称"])
    chg_col = _pick(df.columns, [f"{period}涨跌幅", "涨跌幅"])
    flow_col = _pick(df.columns, [f"{period}主力净流入-净额", "主力净流入-净额"])
    lead_col = _pick(df.columns, ["主力净流入最大股"])
    cols = [c for c in [name_col, chg_col, flow_col, lead_col] if c]
    if not cols:
        return {}
    d = df[cols].dropna(subset=[c for c in [name_col, flow_col] if c])

    def rows(sub):
        out = []
        for r in sub.to_dict("records"):
            flow = r.get(flow_col)
            out.append({
                "板块": r.get(name_col),
                f"{period}涨跌幅%": r.get(chg_col),
                "主力净流入亿元": round(float(flow) / 1e8, 2) if flow is not None else None,
                "主力流入最大股": r.get(lead_col),
            })
        return out

    by_flow = d.sort_values(flow_col, ascending=False)
    by_chg = d.sort_values(chg_col) if chg_col else d
    return {
        "周期": period,
        "主力净流入前列": rows(by_flow.head(top)),
        "主力净流出前列": rows(by_flow.tail(6)),
        "区间跌幅前列": rows(by_chg.head(8)) if chg_col else [],
    }


_flow_cache: dict[str, object] = {}

# 口语板块名 → 数据源板块名。LLM/用户常用俗称，数据源用行业分类名。
_SECTOR_ALIAS = {
    "券商": "证券", "地产": "房地产", "军工": "国防军工", "家电": "白色家电",
    "有色": "有色金属", "煤炭": "煤炭开采", "银行": "银行", "保险": "保险",
    "光伏": "光伏设备", "储能": "电池", "创新药": "化学制药", "医药": "医药商业",
}

# 主题概念 → 行业板块。brief 从口语需求里解析出的常是**主题**（"AI算力""AI应用"），
# 而数据源给的是**行业分类**（"半导体""消费电子"），两套词汇体系天然对不上——
# 实测 "AI存储芯片"/"AI算力"/"芯片" 全部匹配失败，导致板块涨跌、资金流三个字段集体缺失。
# 这里只做**保守**映射：一个主题落到它最主要的行业载体，宁可映射不全也不映射错
# （映射错会让报告拿着别的行业的资金流当本主题的证据，比缺数据更糟）。
_THEME_TO_SECTOR = {
    "AI算力": "半导体", "算力": "半导体", "AI芯片": "半导体", "芯片": "半导体",
    "存储芯片": "半导体", "AI存储芯片": "半导体", "存储": "半导体",
    "半导体设备": "半导体", "集成电路": "半导体", "先进封装": "半导体",
    "AI应用": "软件开发", "人工智能": "软件开发", "大模型": "软件开发",
    "AI服务器": "计算机设备", "服务器": "计算机设备",
    "消费电子": "消费电子", "光模块": "通信设备", "PCB": "元件",
    # “酒类”是口语分类，不是 iFinD 的 A 股标准行业；酒 ETF 的公开主题、
    # 用户口语和模型标题常在“酒/酒类/酒ETF”间切换，统一落到可核验的白酒。
    "酒": "白酒", "酒类": "白酒", "酒ETF": "白酒",
}
# 否定式板块名（如"非白酒"）：查询词不带否定时绝不可匹配到它，否则语义颠倒
_NEG_PREFIX = ("非", "无", "不")


def match_sector_name(query: str, names: list[str]) -> str | None:
    """把口语板块名对到数据源的板块名。

    防三类错：
      ① 语义颠倒——"白酒"曾匹配到"非白酒"（与"思源电气→中国石化"同类的危险错误）；
      ② 常用俗称匹配不上——"券商"在数据源里叫"证券Ⅱ"；
      ③ 主题名匹配不上——需求里说"AI算力"，数据源里只有"半导体"（见 _THEME_TO_SECTOR）。
    优先级：精确 > 别名精确 > 主题映射 > 前缀匹配 > 包含匹配；同级取更主干（更短）者。

    ⚠ 主题映射排在包含匹配之前：否则"AI存储芯片"会被"存储"之类的子串规则
    带到某个不相干的板块上——宁可用保守的显式映射，也不让模糊匹配去猜主题。
    """
    q = (query or "").strip()
    if not q:
        return None
    if q in names:
        return q
    alias = _SECTOR_ALIAS.get(q)
    if alias and alias in names:
        return alias
    theme = _THEME_TO_SECTOR.get(q)
    if theme and theme in names:
        return theme
    for base in filter(None, [alias, theme, q]):
        cands = []
        for n in names:
            # 查询词本身不含否定，就不能落到否定式板块上
            if n.startswith(_NEG_PREFIX) and not base.startswith(_NEG_PREFIX):
                continue
            if base in n or n in base:
                cands.append(n)
        if cands:
            cands.sort(key=lambda n: (not n.startswith(base), len(n), n))
            return cands[0]
    return None


def sector_snapshot(sector: str, period: str = "20日") -> dict | None:
    """取**指定板块**的区间表现与资金流，供 fetcher 作为报告字段使用。

    **iFinD 优先，akshare 兜底**：这三个字段曾是全流程里唯一依赖 akshare 的地方，
    也是唯一不稳定的一环（实测整段时间连不上，三个字段集体缺失）。
    现主路径走 iFinD 成分股聚合（口径与 aggregate.py 一致），
    akshare 保留作降级——它的板块是现成的分类聚合，iFinD 那边取不到成分股时还能顶一下。
    """
    snap = None
    try:
        days = int("".join(ch for ch in period if ch.isdigit()) or 20)
        snap = _sector_snapshot_ifind(sector, days)
    except Exception:
        snap = None
    if snap:
        return snap

    import akshare as ak

    # akshare 的 indicator 只认固定几档（今日/5日/10日），传 "20日" 会直接报错，
    # 把兜底路径也一并搞失效。主周期是 20 日，这里降到它支持的最近一档，
    # 并在返回值里标明实际口径——**宁可口径不同也要标出来，不能让调用方以为是 20 日**。
    _AK_OK = ("今日", "5日", "10日")
    ak_period = period if period in _AK_OK else "10日"

    if ak_period not in _flow_cache:
        with no_proxy():
            _flow_cache[ak_period] = ak.stock_sector_fund_flow_rank(
                indicator=ak_period, sector_type="行业资金流")
    df = _flow_cache[ak_period]

    name_col = _pick(df.columns, ["名称"])
    chg_col = _pick(df.columns, [f"{ak_period}涨跌幅", "涨跌幅"])
    flow_col = _pick(df.columns, [f"{ak_period}主力净流入-净额", "主力净流入-净额"])
    lead_col = _pick(df.columns, ["主力净流入最大股"])
    if not name_col:
        return None

    target = match_sector_name(sector, df[name_col].astype(str).tolist())
    if target is None:
        return None

    row = df[df[name_col].astype(str) == target].iloc[0]
    flow = row.get(flow_col) if flow_col else None
    try:
        flow_yi = round(float(flow) / 1e8, 2) if flow is not None else None
    except (TypeError, ValueError):
        flow_yi = None
    return {
        "板块": target,
        "周期": ak_period,          # 标实际口径，不是请求的口径
        "区间涨跌幅": float(row[chg_col]) if chg_col and row.get(chg_col) is not None else None,
        "主力净流入亿元": flow_yi,
        "领涨股": row.get(lead_col) if lead_col else None,
    }


def _sector_snapshot_ifind(sector: str, days: int = 10) -> dict | None:
    """板块区间表现与资金流——**走 iFinD**，由成分股聚合而来。

    为什么改用 iFinD：这三个字段原先是全流程里唯一依赖 akshare 的地方，
    也是唯一不稳定的一环（实测整段时间连不上，连"证券"这种已验证过的板块名也失败，
    导致三个字段集体缺失）。其余数据早已全部走 iFinD，没有理由为这三个字段
    单独吊在一个免费接口上。

    口径与 aggregate.py 一致：
      - 资金净流入 = Σ成分股区间主力资金流向（可加总的量，直接求和）
      - 区间涨跌幅 = **市值加权**，不是算术平均/中位数
        （同 §方案C 的理由：算术平均会让小盘股与龙头同权，偏离研报口径）
      - 领涨股 = 区间主力资金净流入最大的成分股（与原 akshare 字段同义）
    """
    from . import universe
    from .provider import iFinDProvider

    prov = iFinDProvider()
    if not prov.available():
        return None
    prov._ensure_login()
    import iFinDPy as ths

    # 主题名→行业名的解析统一由 universe.resolve_sector 负责（那里是成分股的唯一入口），
    # 这里只需要拿到解析结果用于口径标注。
    query_sector = universe.resolve_sector(sector)
    leaders = universe.sector_leaders(sector, top=20, provider=prov)
    codes = [x.代码 for x in leaders]
    if not codes:
        return None

    q = " ".join(codes) + f" 近{days}日区间主力资金流向 区间涨跌幅 总市值"
    for attempt in range(2):
        d = ths.THS_iwencai(q, "stock")
        if d.get("errorcode", -1) == 0:
            break
        if attempt == 0:
            time.sleep(1)
    else:
        return None

    tables = d.get("tables") or []
    t = tables[0].get("table", {}) if tables else {}

    def col(prefix):
        for k in t:
            if str(k).startswith(prefix):
                return t[k]
        return None

    names = t.get("股票简称") or []
    flow, chg, mv = col("区间主力资金流向"), col("区间涨跌幅"), col("总市值")
    if not (names and flow and chg):
        return None

    rows = []
    for i in range(len(names)):
        try:
            f = float(flow[i])
            c = float(chg[i])
            m = float(mv[i]) if mv and mv[i] not in (None, "") else None
            if f == f and c == c:
                rows.append((names[i], f, c, m))
        except (TypeError, ValueError, IndexError):
            continue
    if not rows:
        return None

    tot_flow = sum(r[1] for r in rows)
    mv_sum = sum(r[3] for r in rows if r[3])
    if mv_sum:
        chg_w = sum(r[2] * r[3] for r in rows if r[3]) / mv_sum
    else:                                   # 市值缺失时退回等权，并非静默——见 note
        chg_w = sum(r[2] for r in rows) / len(rows)

    # 口径如实标注：主题名被映射到行业时要写出来，别让报告以为这就是该主题的数
    label = (f"{query_sector}（{sector}对应行业）" if query_sector != sector else sector)
    return {
        "板块": label,
        "周期": f"{days}日",
        "区间涨跌幅": round(chg_w, 2),
        "主力净流入亿元": round(tot_flow / 1e8, 2),
        "领涨股": max(rows, key=lambda r: r[1])[0],
        "来源": f"iFinD·{query_sector}板块{len(rows)}只成分股聚合",
    }


def _ipo_calendar() -> list[dict]:
    """新股申购/发行日历：事件驱动型选题（IPO 窗口期）的来源。"""
    import akshare as ak

    df = ak.stock_xgsglb_em(symbol="全部股票")
    return df.head(20).to_dict("records")


def _recent_news(symbols: list[str] | None = None) -> list[dict]:
    """近期财经/个股新闻，作为热点线索。"""
    import akshare as ak

    out: list[dict] = []
    for sym in (symbols or ["300750", "600519", "000001"]):
        try:
            with no_proxy():
                df = ak.stock_news_em(symbol=sym)
            title_col = _pick(df.columns, ["新闻标题", "标题"])
            time_col = _pick(df.columns, ["发布时间", "时间"])
            if title_col:
                for r in df.head(5).to_dict("records"):
                    out.append({"标的": sym, "标题": r.get(title_col), "时间": r.get(time_col)})
        except Exception:
            continue
    return out


def _pick(columns, candidates: list[str]) -> str | None:
    """在实际列名里挑第一个匹配的候选列名（容忍 akshare 列名变动）。"""
    cols = list(columns)
    for cand in candidates:
        for c in cols:
            if cand in str(c):
                return c
    return None


def collect_signals(date: str | None = None, *, with_news: bool = True) -> SignalBundle:
    """采集当日选题信号包。任何单源失败都不会抛出。"""
    date = date or dt.date.today().strftime("%Y%m%d")
    bundle = SignalBundle(date=date)
    _safe(bundle, "sector_fund_flow", _sector_fund_flow)   # 核心：多周期区间表现+资金流
    _safe(bundle, "industry_movers", _industry_movers)
    _safe(bundle, "concept_movers", _concept_movers)
    _safe(bundle, "ipo_calendar", _ipo_calendar)
    if with_news:
        _safe(bundle, "recent_news", _recent_news)
    return bundle


if __name__ == "__main__":  # 冒烟测试：python -m core.signals
    b = collect_signals(with_news=False)
    print(b.summary())
    for k, v in b.data.items():
        n = len(v) if hasattr(v, "__len__") else "?"
        print(f"  {k}: {n} 条")
    for k, e in b.errors.items():
        print(f"  [缺失] {k}: {e}")
