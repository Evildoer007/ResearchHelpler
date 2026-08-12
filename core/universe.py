"""标的池：按板块取**真实**龙头标的，作为报告的代表标的。

为什么需要它：
1. 不靠 LLM 记忆猜证券代码（实测 LLM 要么留空、要么给错——"思源电气"曾被写成中国石化的代码）；
   这里用 iFinD 智能选股(THS_iwencai)按板块取真实成分与代码。
2. 修正方案A 的失真（DESIGN §9.1）：默认**剔除上市不足 N 年的次新股**——
   用刚上市的新股代表整个板块会让 PB/分红/净利同比完全不具代表性。

配额：iwencai 单次查询 dataVol 约 40，属较贵调用，且板块龙头是慢变数据，故按天缓存到 data_cache/。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from . import config


@dataclass
class Leader:
    代码: str
    简称: str
    总市值: float | None = None
    上市日期: str = ""

    @property
    def 市值亿元(self) -> float | None:
        return round(self.总市值 / 1e8, 1) if self.总市值 else None


# ── 宽口径主题 → 一组一级行业 ────────────────────────────────────────────
# "消费""周期""科技"这类说法在 A 股是**跨行业的大类**，不是任何一级行业的名字。
# 把它们塞给 iwencai 直查会拿到垃圾（实测）：
#   "消费 总市值排名前20"  → 锦江酒店/宋城演艺/黄山旅游…（旅游+小家电，茅台都不在里面）
#   "消费板块 所属同花顺行业" → 深科技/歌尔股份/立讯精密…（按字面匹配到**消费电子**，
#                              整个跑到电子行业去了，与消费品毫无关系）
# 而拆成一级行业逐个查全部准确（家用电器→美的/格力/海尔，商贸零售→中国中免，
# 美容护理→爱美客/珀莱雅）。故宽口径一律拆开查再合并，绝不整名直查。
#
# **口径定义放在 `sector_groups.json`，不写死在这里**：大类聚合没有官方标准，
# 各家研究部口径不一样（汽车算不算消费、医药算不算，都有分歧），
# 这是业务决策而非实现细节，应当能被研究部直接维护并留下修改痕迹。
# 文件缺失或写坏时回退到下面的内置默认值，功能不受影响。
# 一个"板块"至少要有这么多成分股才配叫板块。低于此数多半是名字匹配到了
# 同名公司或极窄的细分（实测"机器人"只匹配到 300024.SZ 沈阳新松一家），
# 拿一家公司的估值/盈利当板块整体法，是最隐蔽的一种以偏概全。
_MIN_SECTOR_LEADERS = 5

_DEFAULT_BASIS = "申万一级行业分类"
_DEFAULT_GROUPS: dict[str, list[str]] = {
    "消费": ["食品饮料", "家用电器", "商贸零售", "社会服务", "纺织服饰", "美容护理"],
    "大消费": ["食品饮料", "家用电器", "商贸零售", "社会服务", "纺织服饰", "美容护理"],
    "消费板块": ["食品饮料", "家用电器", "商贸零售", "社会服务", "纺织服饰", "美容护理"],
    "必选消费": ["食品饮料", "商贸零售"],
    "可选消费": ["家用电器", "社会服务", "纺织服饰", "美容护理"],
}


def _load_groups() -> tuple[dict[str, list[str]], str]:
    """读 sector_groups.json，展平成 {名字或别名: [成分行业]} + 分类基准名。"""
    path = config._PROJECT_ROOT / "sector_groups.json"
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_DEFAULT_GROUPS), _DEFAULT_BASIS

    flat: dict[str, list[str]] = {}
    for name, spec in (d.get("宽口径板块") or {}).items():
        parts = [str(x).strip() for x in (spec.get("成分行业") or []) if str(x).strip()]
        if not parts:
            continue
        for key in [name, *(spec.get("别名") or [])]:
            key = str(key).strip()
            if key:
                flat[key] = list(parts)
    if not flat:                      # 文件在但内容不可用，仍回退，不静默变成"没有宽口径"
        return dict(_DEFAULT_GROUPS), _DEFAULT_BASIS
    return flat, str(d.get("分类基准") or _DEFAULT_BASIS)


_BROAD_THEMES, _BASIS = _load_groups()
# 每个宽口径名字的"依据"。文件里定义的记标准名（申万一级…），
# 运行时自动拆解出来的记成另一个措辞——两者在报告里必须能区分开：
# 一个是研究部认过的口径，一个是本次临时拆的，可信度不是一回事。
_BROAD_BASIS: dict[str, str] = {k: _BASIS for k in _BROAD_THEMES}
AUTO_BASIS = "按需求自动拆解（成分行业已逐个校验可取数，非研究部既定口径）"


def broad_parts(sector: str) -> list[str]:
    """宽口径主题 → 其成分一级行业；不是宽口径则返回空列表。"""
    return list(_BROAD_THEMES.get((sector or "").strip(), []))


def is_broad(sector: str) -> bool:
    return bool(broad_parts(sector))


def classification_basis(sector: str | None = None) -> str:
    """该口径依据什么。印进报告——读者据此判断这个口径有多少分量。"""
    if sector:
        return _BROAD_BASIS.get(sector.strip(), _BASIS)
    return _BASIS


def _is_real_industry(name: str, provider=None, *, min_hit: float = 0.6) -> bool:
    """这个名字是不是数据源认的**行业分类名**（而非概念名或臆造名）。

    ⚠ 不能用"能不能取到成分股"来判——**iwencai 对任何字符串都会模糊匹配返回
    一批股票，从不报错**。实测 `瞎编行业`、`低空经济板块` 都能返回足够多的股票，
    拿它当校验等于没校验。

    真正的判据是**返回的股票，其行业链里是否真的含这个名字**：
      "食品饮料"   → 链全是 `食品饮料-白酒-白酒Ⅲ` 之类，含 ✔
      "消费板块"   → 链全是 `电子-消费电子-…`，不含 ✘（它匹配到了消费电子）
      "低空经济板块" → 概念，成分来自各行各业，链里不会一致出现这个名字 ✘
    """
    from .provider import iFinDProvider

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return False
    prov._ensure_login()
    import iFinDPy as ths

    try:
        d = ths.THS_iwencai(f"{name} 所属同花顺行业", "stock")
    except Exception:
        return False
    if d.get("errorcode", -1) != 0:
        return False
    t = (d.get("tables") or [{}])[0].get("table", {}) or {}
    col = next((k for k in t if "所属" in str(k)), None)
    chains = [str(x) for x in (t.get(col) or [])] if col else []
    if len(chains) < 5:                      # 样本太少，判不了，保守拒绝
        return False
    # 还要求链是**多级**的。概念名（"人工智能"）iwencai 会把查询词原样回显成
    # 一个光秃秃的 "人工智能"，而真行业链一定分级（"食品饮料-白酒-白酒Ⅲ"）。
    # 不加这一条，概念名会因为"链里含这个名字"而通过——实测人工智能就是这么混进来的。
    hit = 0
    for c in chains:
        parts = [p.strip() for p in c.split("-") if p.strip()]
        if len(parts) >= 2 and name in parts:
            hit += 1
    return hit / len(chains) >= min_hit


def validate_industries(names: list[str], *, provider=None) -> tuple[list[str], list[str]]:
    """校验一批行业名是否真是数据源认的一级行业，返回 (可用, 不可用)。

    这是把"模型说的"变成"数据源认的"的唯一闸门。它能挡住名字不存在、
    概念名冒充行业名；**挡不住"名字是真的但归错大类"**——把汽车拆进医药，
    每个名字都合法，校验必然放行。故自动拆解的口径要在报告里标注来源并列出成分，
    最终由人否决（见 `register_broad` 与 brief 的展开打印）。
    """
    ok, bad = [], []
    for n in dict.fromkeys(x.strip() for x in names if x and x.strip()):
        if is_broad(n):            # 拆出来的还是个宽口径名，说明没拆到底，不收
            bad.append(n)
            continue
        (ok if _is_real_industry(n, provider) else bad).append(n)
    return ok, bad


def register_broad(name: str, parts: list[str], *, basis: str = AUTO_BASIS) -> list[str]:
    """把一个**本次会话内**的宽口径口径登记进来，返回实际生效的成分行业。

    只写进程内存，不回写 `sector_groups.json`——那个文件是研究部的口径决策，
    不该被程序按某次提问的临时拆解悄悄改掉。若某个拆解值得固化，
    由人看过之后手工加进文件（终端会打印提示）。

    登记后 `resolve_sector`/`sector_leaders`/`_subsector_detail`/成品口径行
    全部自动按宽口径走，与文件里定义的那些走同一条路径，不需要各处再改。
    """
    name = (name or "").strip()
    parts = [p for p in dict.fromkeys(parts) if p]
    if not name or len(parts) < 2:      # 只剩一个行业就不算"宽口径"，按普通板块处理更准确
        return []
    _BROAD_THEMES[name] = parts
    _BROAD_BASIS[name] = basis
    return parts


def _cache_path(sector: str, date: str):
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in sector if ch.isalnum() or ch in "－_")[:24]
    return config.DATA_CACHE_DIR / f"leaders_{safe}_{date}.json"


def _pick_col(table: dict, *keys: str) -> list | None:
    """iwencai 列名常带日期后缀（如 总市值[20260729]），按前缀匹配。"""
    for k in keys:
        for col in table:
            if str(col).startswith(k):
                return table[col]
    return None


def _query_iwencai(query: str, provider=None) -> list[Leader]:
    from .provider import iFinDProvider

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()
    import iFinDPy as ths

    d = ths.THS_iwencai(query, "stock")
    if d.get("errorcode", -1) != 0:
        return []
    prov.total_data_vol += int(d.get("dataVol", 0) or 0)

    tables = d.get("tables") or []
    if not tables:
        return []
    t = tables[0].get("table", {}) or {}
    codes = _pick_col(t, "股票代码", "代码")
    names = _pick_col(t, "股票简称", "简称")
    mvs = _pick_col(t, "总市值")
    lists = _pick_col(t, "新股上市日期", "上市日期")
    if not codes or not names:
        return []

    out = []
    for i, code in enumerate(codes):
        out.append(Leader(
            代码=str(code),
            简称=str(names[i]) if i < len(names) else "",
            总市值=float(mvs[i]) if mvs and i < len(mvs) and mvs[i] is not None else None,
            上市日期=str(lists[i]) if lists and i < len(lists) and lists[i] else "",
        ))
    return out


def _listed_years(d: str) -> float | None:
    """上市日期(YYYYMMDD 或 YYYY-MM-DD) → 已上市年数。"""
    s = str(d).replace("-", "").strip()[:8]
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        day = dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return (dt.date.today() - day).days / 365.25


def _industry_chain_of_stock(name_or_code: str, provider=None) -> list[str]:
    """查某只股票的同花顺行业**全链**，如 ["食品饮料", "白酒", "白酒Ⅲ"]。

    返回整条链而非只取一级：调用方既要"该细化到哪一级"（取第二级），
    也要判断"给定的板块名是不是链上已有的一级"（见 resolve_sector 的不下钻规则），
    后者只有拿到全链才判得了。
    """
    from .provider import iFinDProvider

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()
    import iFinDPy as ths

    try:
        d = ths.THS_iwencai(f"{name_or_code} 所属同花顺行业", "stock")
    except Exception:
        return []
    if d.get("errorcode", -1) != 0:
        return []
    tables = d.get("tables") or []
    t = tables[0].get("table", {}) if tables else {}
    col = next((k for k in t if "所属" in str(k)), None)
    if not col or not t[col]:
        return []
    return [x.strip() for x in str(t[col][0]).split("-") if x.strip()]


def _industry_of_stock(name_or_code: str, provider=None) -> str | None:
    """查某只股票的同花顺行业（取第二级）。

    返回形如 "电子-半导体-数字芯片设计" 的第二级 "半导体"——
    实测这一级正好对应 iwencai 认的板块名（半导体/证券/白酒/通信设备/软件开发），
    第一级太宽（电子）、第三级太细（数字芯片设计）。写法同 peers.py。
    """
    chain = _industry_chain_of_stock(name_or_code, provider)
    return chain[1] if len(chain) >= 2 else None


def resolve_sector(sector: str, *, rep_code: str | None = None, provider=None) -> str:
    """主题概念 → 行业分类。**成分股的唯一入口在这里做，不由各调用方各自处理。**

    brief 从口语需求解析出的常是主题（"AI算力""AI存储芯片"），而 iwencai 认的是
    行业分类。直接拿主题名查，iwencai 会按**宽泛的AI概念**理解——
    实测 "AI存储芯片" 返回的前几大是中国移动、工业富联、海康威视，
    据此聚合出的板块 PE 为 38.5 倍，而真实半导体行业是 139.4 倍，**差 3.6 倍**。
    拿 38.5 倍去判 V1（估值低分位）会得出与事实相反的结论——
    数字真、论断假（DESIGN §9.1 那一类）。

    放在这里而不是各调用方：aggregate/fundamentals/unlock/peers/signals 共 9 处
    都要取成分股，逐个改必然漏，且以后新增调用方还会再踩一遍。

    两级解析：
      ① 查 `_THEME_TO_SECTOR` 对照表——快、无网络、结果确定，覆盖高频主题；
      ② 表里没有且给了代表标的时，**查该标的的所属行业**。
         这一步让表不必穷举所有主题（主题是无限的，维护对照表是无底洞）。
         为什么用代表标的而不是"主题的成分股取众数"：实测后者不可靠，
         iwencai 对主题查询返回的是**概念标签**而非行业
         （"AI算力"→"人工智能"、"AI存储芯片"→一串概念串），且列名还不统一。
         而代表标的是 brief 选定并经代码校验的真实个股，它的行业归属明确。
    """
    from .signals import _THEME_TO_SECTOR

    s = (sector or "").strip()
    if not s:
        return sector
    # 宽口径主题不下钻：它本就代表一组行业，下钻到代表标的的单一行业
    # 会把"消费"悄悄变成"白酒"——正是本函数最该防的那类口径收缩。
    if is_broad(s):
        return s
    if s in _THEME_TO_SECTOR:
        return _THEME_TO_SECTOR[s]
    # ⚠ 只有**个股**的行业归属才可信。代表标的是 ETF/基金时，查行业分类会返回
    # 无意义的值——实测 `159995.SZ`（芯片ETF）查出"医疗服务"，
    # 于是 resolve_sector("元件", rep_code="159995.SZ") 返回"医疗服务"，
    # 整份芯片报告的成分股全成了医药股（正文里冒出泰格医药、凯莱英、昭衍新药）。
    # 基金没有行业归属，这一级兜底对它本就不适用，宁可退回原名。
    if rep_code and not _is_fund(rep_code):
        chain = _industry_chain_of_stock(rep_code, provider)
        # ⚠ 只对**概念主题**下钻，不对**已经是行业分类的名字**下钻。
        # 二者靠"给定名是否落在代表标的的行业链上"区分：
        #   "AI存储芯片" 不在 ["电子","半导体","数字芯片设计"] 里 → 是概念，下钻到"半导体"；
        #   "食品饮料"   就在 ["食品饮料","白酒","白酒Ⅲ"] 里     → 已是合法一级，原样保留。
        # 不加这条判断的后果（实测）：问"消费板块如何"，brief 解析出"食品饮料"，
        # 代表标的取贵州茅台，这里把板块悄悄换成"白酒"，整份报告的口径从
        # 食品饮料（含白酒/啤酒/软饮/肉制品/休闲食品）缩到只剩白酒，
        # 而用户从头到尾没要求只看白酒——口径被改了却没有任何地方说明。
        if s in chain:
            # ⚠ 分类学上合法 **不等于** 数据源取得出来。实测"机器人"确实是
            # 绿的谐波行业链上的三级行业（机械设备-自动化设备-机器人），
            # 但 `sector_leaders("机器人")` 只返回 1 只——**同名公司**
            # 300024.SZ「机器人」（沈阳新松）。于是整份报告的"板块聚合"
            # 其实是一家公司的数据，却通篇写作"机器人板块"。
            # 这正是 #67 那条"在链上就保留"引入的回归：它只看了分类学，没看可取性。
            # 故再加一道可取性校验，取不出就沿行业链**往上退**到取得出的那级。
            n = len(sector_leaders(s, top=_MIN_SECTOR_LEADERS, provider=provider))
            if n >= _MIN_SECTOR_LEADERS:
                return s
            # **0 只 = 取数失败，1~4 只 = 匹配到同名公司或过窄细分**，两者处置相反：
            # 取数失败时保留原名，让下游以"字段缺口"的形式**暴露**出来；
            # 若这里改名，一次网络抖动就会把板块悄悄换掉，而成品上完全看不出——
            # 与 `series_multi` 静默丢成分股是同一类错（#69），宁可显式失败。
            if n == 0:
                return s
            for name in [x for x in reversed(chain) if x != s]:
                if len(sector_leaders(name, top=_MIN_SECTOR_LEADERS,
                                      provider=provider)) >= _MIN_SECTOR_LEADERS:
                    return name
            return chain[1] if len(chain) >= 2 else s
        if len(chain) >= 2 and chain[1] != s:
            return chain[1]
    return sector


def _is_fund(code: str) -> bool:
    """是否为基金/ETF 代码。

    沪深 ETF 与 LOF 的数字段有明确区段：深市 15/16/18 开头，沪市 51/52/56/58 开头。
    个股不会落在这些区段（深市主板 00、创业板 30，沪市主板 60、科创板 68）。
    """
    c = (code or "").split(".")[0].strip()
    return c[:2] in {"15", "16", "18", "51", "52", "56", "58"} and len(c) == 6


def sector_leaders(
    sector: str, *, top: int = 8, min_listed_years: float = 2.0,
    provider=None, use_cache: bool = True,
) -> list[Leader]:
    """取某板块的真实龙头标的（按市值降序，默认剔除上市不足 min_listed_years 年的次新股）。

    传入主题名（如"AI算力"）时会先解析成行业名再查，见 resolve_sector。
    传入宽口径主题（如"消费"）时拆成各一级行业分别查再合并，见 _BROAD_THEMES。
    """
    # 宽口径：逐个子行业查完合并，再按市值全局排序取前 top。
    # 放在这里而不是各调用方：aggregate/fundamentals/unlock/peers/signals 共 9 处
    # 取成分股都经由本函数，改这一处即可让整条链路支持宽口径。
    if is_broad(sector):
        pool: dict[str, Leader] = {}
        for sub in broad_parts(sector):
            for l in sector_leaders(sub, top=20, min_listed_years=min_listed_years,
                                    provider=provider, use_cache=use_cache):
                pool.setdefault(l.代码, l)      # 行业间理论上不重叠，去重只为保险
        merged = sorted(pool.values(), key=lambda x: x.总市值 or 0, reverse=True)
        return merged[:top]

    sector = resolve_sector(sector)
    date = dt.date.today().strftime("%Y%m%d")
    path = _cache_path(sector, date)
    raw: list[Leader] = []

    if use_cache and path.exists():
        try:
            raw = [Leader(**x) for x in json.loads(path.read_text(encoding="utf-8"))]
        except Exception:
            raw = []

    if not raw:
        raw = _query_iwencai(f"{sector} 总市值排名前20 上市日期", provider)
        if raw and use_cache:
            path.write_text(
                json.dumps([l.__dict__ for l in raw], ensure_ascii=False),
                encoding="utf-8",
            )

    kept = []
    for l in raw:
        yrs = _listed_years(l.上市日期)
        if min_listed_years and yrs is not None and yrs < min_listed_years:
            continue          # 次新股不能代表板块（DESIGN §9.1）
        kept.append(l)
    return kept[:top]


def pick_representative(sectors: list[str], *, provider=None, **kw) -> Leader | None:
    """在若干候选板块中挑一个代表标的（取市值最大的成熟龙头）。"""
    best: Leader | None = None
    for s in sectors:
        for l in sector_leaders(s, provider=provider, **kw):
            if best is None or (l.总市值 or 0) > (best.总市值 or 0):
                best = l
    return best


if __name__ == "__main__":  # python -m core.universe
    for sec in ["半导体", "证券"]:
        ls = sector_leaders(sec, top=5)
        print(f"[{sec}] 成熟龙头 {len(ls)} 只：")
        for l in ls:
            print(f"   {l.代码} {l.简称:<8} 市值{l.市值亿元}亿  上市{l.上市日期}")
