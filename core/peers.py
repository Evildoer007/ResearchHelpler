"""同业对比：本板块 vs 可比板块的估值与盈利。

支撑论点 V3/V3b（相对估值折溢价）与 E3/E3b（盈利剪刀差）。

**为什么需要"可比"而不是"任意板块"**：
跨大类比 PE 无意义——半导体 126 倍 vs 证券 13.9 倍，不能推出"证券便宜"。
故可比板块限定为**同一行业层级下的兄弟板块**。

**可比板块由 iFinD 行业层级动态推断，不人工维护清单**：
① 取板块所属的完整行业串（一级-二级-三级，如"电子-半导体-集成电路制造"）
② 先在二级下找三级兄弟（精确同层比较，如半导体下的封测/设计/设备/材料）
③ 若兄弟数不足（如"白酒"二级下只有自己一个三级子类），
   升一级到一级行业下找二级兄弟——对应人工分析师"同层太窄就扩大到业务可比范围"的做法
   （实测：白酒→食品饮料一级下的[食品加工制造, 饮料制造]）
④ 每一步都对 iwencai 返回结果做**行业字段二次过滤**——
   iwencai 对行业范围限定的理解不稳定（同类问题见 aggregate.py 的板块成分过滤）：
   实测"军工装备 二级行业 成分股..."返回606只，仅76只行业字段真含"军工装备"，
   其余是风电零部件/家电零部件等完全无关行业，若不过滤会把错误板块混进可比组。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field as dfield
from pathlib import Path

from . import config
from .provider import DataProvider, iFinDProvider

# 可比样本下限。原为3，实测证券/白酒等板块因分类学结构性原因（如"非银金融"一级下
# 仅3个二级，证券排除自身后最多2个真兄弟）永远凑不满3，2026-08-03 经用户确认降至2——
# 2家可比公司对比在研报中也常见，代价是中位数退化为两者平均、统计意义弱于3+。
MIN_PEERS = 2


@dataclass
class PeerCompare:
    板块: str
    指标: str                       # PE / 归母净利同比
    本值: float | None = None
    样本数: int = 0
    取数失败: list[str] = dfield(default_factory=list)
    同业中位数: float | None = None
    差值: float | None = None       # 本值 - 中位数
    相对偏离: float | None = None   # (本值-中位数)/|中位数| ×100，%
    同业明细: list[dict] = dfield(default_factory=list)
    ok: bool = False
    error: str = ""


def sector_industry(sector: str, provider: DataProvider | None = None) -> str:
    """查板块（用其代表股）所属的同花顺三级行业串，如「电子-半导体-集成电路制造」。"""
    from . import universe

    leaders = universe.sector_leaders(sector, top=1, provider=provider)
    if not leaders:
        return ""
    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return ""
    prov._ensure_login()
    import iFinDPy as ths

    d = ths.THS_iwencai(f"{leaders[0].简称} 所属同花顺行业", "stock")
    if d.get("errorcode", -1) != 0:
        return ""
    tables = d.get("tables") or []
    t = tables[0].get("table", {}) if tables else {}
    for k, v in t.items():
        if "行业" in str(k) and isinstance(v, list) and v:
            return str(v[0])
    return ""


def _industry_rows(level_name: str, provider: DataProvider | None = None) -> list[str]:
    """查某行业层级（一级或二级名称）下的全部成分股所属完整行业串。

    **已用行业字段自身二次过滤**——不信任 iwencai 返回的原始范围（见模块说明的军工装备案例）。
    """
    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()
    import iFinDPy as ths

    d = ths.THS_iwencai(f"{level_name} 成分股 所属同花顺行业", "stock")
    if d.get("errorcode", -1) != 0:
        return []
    tables = d.get("tables") or []
    t = tables[0].get("table", {}) if tables else {}
    ind_col = None
    for k in t:
        if "所属同花顺行业" in str(k):
            ind_col = t[k]
            break
    if not ind_col:
        return []
    # 二次过滤：只保留行业字段真的包含 level_name 的行，防 iwencai 误配无关板块
    return [str(x) for x in ind_col if level_name in str(x)]


def _industry_children(level_name: str, child_index: int,
                       provider: DataProvider | None = None) -> list[str]:
    """从过滤后的行业串中取第 child_index 段的去重集合（0=一级,1=二级,2=三级）。"""
    rows = _industry_rows(level_name, provider)
    out: set[str] = set()
    for r in rows:
        parts = [p.strip() for p in r.split("-")]
        if len(parts) > child_index:
            out.add(parts[child_index])
    return sorted(out)


def _cache_path(sector: str) -> Path:
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    return config.DATA_CACHE_DIR / f"peers_{safe}_{dt.date.today():%Y%m%d}.json"


def dynamic_peers(sector: str, *, min_siblings: int = MIN_PEERS,
                  provider: DataProvider | None = None, use_cache: bool = True) -> list[str]:
    """动态推断可比板块（不依赖人工维护清单）。

    行业分类当天不会变，故按天缓存——避免每次都要 2~3 次 iwencai 调用
    （查代表股行业 + 查兄弟板块，可能还要再查一级）。
    """
    path = _cache_path(sector)
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    industry_str = sector_industry(sector, provider)
    parts = [p.strip() for p in industry_str.split("-") if p.strip()]
    result: list[str] = []
    if len(parts) >= 2:
        level1, level2 = parts[0], parts[-2]

        def _exclude_self(names: list[str]) -> list[str]:
            return [n for n in names if n != sector and sector not in n and n not in sector]

        # ① 精确同层：二级下的三级子行业
        siblings = _exclude_self(_industry_children(level2, 2, provider))

        # ② 兄弟太少（如"白酒"二级仅自身一个三级）→ 升一级找二级兄弟
        if len(siblings) < min_siblings and level1:
            broader = _exclude_self(_industry_children(level1, 1, provider))
            if broader:
                siblings = broader

        result = siblings

    if use_cache:
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def compare(sector: str, metric: str = "PE", *, top: int = 20,
            provider: DataProvider | None = None) -> PeerCompare:
    """本板块该指标 vs 可比板块中位数。

    metric 取 aggregate 聚合结果的键，如 "PE" / "PB" / "归母净利同比"。
    用**中位数**而非均值——板块间差异大，均值易被极端值带偏。
    """
    from . import aggregate as ag

    c = PeerCompare(板块=sector, 指标=metric)
    peers = dynamic_peers(sector, provider=provider)
    if not peers:
        c.error = f"未能动态推断出「{sector}」的可比板块（行业分类过窄或查询失败）"
        return c

    own = ag.sector_aggregate(sector, top=top, provider=provider)
    if not own.ok or own.get(metric) is None:
        c.error = f"本板块 {metric} 取数失败：{own.error}"
        return c
    c.本值 = own.get(metric)

    vals: list[float] = []
    failed: list[str] = []
    for p in peers:
        a = ag.sector_aggregate(p, top=top, provider=provider)
        v = a.get(metric) if a.ok else None
        if v is None:
            failed.append(p)
            continue
        vals.append(v)
        c.同业明细.append({"板块": p, metric: round(v, 2)})

    # 同业样本太少时中位数不可靠——实测半导体 4 个可比板块中 2 个取数失败，
    # 若不设下限，会用仅 2 个样本的"中位数"下相对高估/低估的结论。
    if len(vals) < MIN_PEERS:
        c.error = (f"可比样本不足（有效 {len(vals)} 个 < {MIN_PEERS}，候选：{peers}）"
                   + (f"，取数失败：{failed}" if failed else ""))
        return c
    c.样本数 = len(vals)
    c.取数失败 = failed

    vals.sort()
    n = len(vals)
    c.同业中位数 = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    c.差值 = c.本值 - c.同业中位数
    if abs(c.同业中位数) > 1e-9:
        c.相对偏离 = c.差值 / abs(c.同业中位数) * 100
    c.ok = True
    return c


if __name__ == "__main__":  # python -m core.peers
    for sec in ["半导体", "证券", "白酒", "教育"]:
        peers = dynamic_peers(sec, use_cache=False)
        print(f"=== {sec} ===  动态推断可比板块({len(peers)}个): {peers}")
        for m in ("PE", "归母净利同比"):
            r = compare(sec, m, top=15)
            if r.ok:
                print(f"   {m:<8} 本值 {r.本值:.2f} | 同业中位 {r.同业中位数:.2f} | "
                      f"偏离 {r.相对偏离:+.1f}%  (样本{r.样本数})")
            else:
                print(f"   {m}: {r.error}")
        print()
