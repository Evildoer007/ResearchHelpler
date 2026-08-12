"""限售股解禁：未来解禁压力 vs 历史常态。

支撑论点 F5（解禁高峰已过）/ F5b（解禁高峰临近）/ C6（解禁窗口期）。

**为什么这类数据对场外衍生品特别重要**：解禁是可预知的**供给冲击**——
时点确定、规模可算，与"事件发生后才知道"的多数催化不同。
对雪球这类怕大跌敲入的结构，未来 3 个月有无解禁高峰直接影响敲入线的设置。

口径：
- 规模用**解禁金额占板块流通市值的比例**而非绝对额——绝对额在大小板块之间不可比
  （半导体 300 亿解禁与白酒 300 亿，压力完全不是一回事）。这也是 §15 里
  "阈值宜从绝对额改相对口径"那条待办在本模块的落地。
- 「高峰」的判据是**未来 3 个月 vs 后续 9 个月的月均**，即和该板块自身的
  解禁节奏比，而不是和固定百分比阈值比。

⚠ iwencai 的板块限定不稳定（见 aggregate.py / peers.py 同类记录）：
实测"证券行业 解禁日期 解禁市值"返回 1433 行，首行却是许继电气（电力设备）。
故本模块**一律用 universe 取到的板块成分股代码做二次过滤**，不信任 iwencai 的范围限定。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field as dfield

from . import config
from .provider import DataProvider, iFinDProvider

NEAR_MONTHS = 3     # "未来 3 个月"——与 F5/F5b 的触发条件表述一致
FAR_MONTHS = 12     # 对比基准区间总长度（近端 3 个月 + 远端 9 个月）


@dataclass
class UnlockPressure:
    板块: str
    近端月数: int = NEAR_MONTHS
    近端金额: float = 0.0        # 未来 3 个月解禁总额（元）
    远端月均: float = 0.0        # 第 4~12 个月的月均解禁额（元）
    近端月均: float = 0.0
    倍数: float | None = None    # 近端月均 / 远端月均，>1 表示解禁前置
    占流通市值: float | None = None   # 近端金额 / 板块流通市值 ×100，%
    涉及股票数: int = 0
    明细: list[dict] = dfield(default_factory=list)   # 近端解禁前列
    ok: bool = False
    error: str = ""


def _cache_path(sector: str) -> "config.Path":
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    return config.DATA_CACHE_DIR / f"unlock_{safe}_{dt.date.today():%Y%m%d}.json"


def _query(sector: str, months: int, prov: iFinDProvider) -> dict:
    import iFinDPy as ths

    d = ths.THS_iwencai(
        f"{sector} 未来{months}个月限售股解禁金额 解禁日期 所属同花顺行业", "stock")
    if d.get("errorcode", -1) != 0:
        return {}
    prov.total_data_vol += int(d.get("dataVol", 0) or 0)
    tables = d.get("tables") or []
    return tables[0].get("table", {}) if tables else {}


def _col(t: dict, *keys: str) -> list:
    """iwencai 列名带动态日期后缀（如「解禁金额[20260805-20261104]」），按关键词模糊取列。"""
    for k in t:
        if all(x in str(k) for x in keys):
            return t[k]
    return []


def sector_unlock(sector: str, *, provider: DataProvider | None = None,
                  use_cache: bool = True) -> UnlockPressure:
    """板块未来解禁压力。一次取未来 12 个月，再按日期切成近端/远端两段。"""
    path = _cache_path(sector)
    if use_cache and path.exists():
        try:
            return UnlockPressure(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    from . import aggregate as ag
    from . import universe

    u = UnlockPressure(板块=sector)
    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        u.error = "iFinD 不可用"
        return u
    prov._ensure_login()

    # 板块成分股，用于二次过滤（不信任 iwencai 的范围限定）
    leaders = universe.sector_leaders(sector, top=60, provider=provider)
    member = {x.代码 for x in leaders}
    if not member:
        u.error = f"未取到板块「{sector}」成分股，无法过滤解禁结果"
        return u

    t = _query(sector, FAR_MONTHS, prov)
    if not t:
        u.error = "解禁查询无返回"
        return u

    codes = _col(t, "股票代码") or _col(t, "代码")
    names = _col(t, "股票简称") or _col(t, "简称")
    amts = _col(t, "解禁金额")
    dates = _col(t, "解禁日期")
    if not (codes and amts and dates):
        u.error = f"解禁结果缺列（现有列：{list(t)[:6]}）"
        return u

    today = dt.date.today()
    near_end = today + dt.timedelta(days=NEAR_MONTHS * 30)
    near_total = far_total = 0.0
    hit: set[str] = set()
    rows: list[dict] = []

    for i, code in enumerate(codes):
        code = str(code)
        if code not in member:          # ← 二次过滤：iwencai 常把无关行业混进来
            continue
        try:
            amt = float(amts[i])
            day = dt.datetime.strptime(str(dates[i])[:8], "%Y%m%d").date()
        except (TypeError, ValueError, IndexError):
            continue
        if amt <= 0 or day < today:
            continue
        hit.add(code)
        if day <= near_end:
            near_total += amt
            rows.append({"代码": code, "简称": str(names[i]) if i < len(names) else "",
                         "金额": amt, "日期": day.isoformat()})
        else:
            far_total += amt

    if not hit:
        u.error = f"板块「{sector}」未来 {FAR_MONTHS} 个月无解禁记录（或全部被成分股过滤剔除）"
        return u

    u.近端金额 = near_total
    u.近端月均 = near_total / NEAR_MONTHS
    far_months = FAR_MONTHS - NEAR_MONTHS
    u.远端月均 = far_total / far_months if far_months else 0.0
    if u.远端月均 > 0:
        u.倍数 = u.近端月均 / u.远端月均
    u.涉及股票数 = len(hit)
    u.明细 = sorted(rows, key=lambda r: r["金额"], reverse=True)[:5]

    # 相对口径：占板块流通市值比例（大小板块可比）
    agg = ag.sector_aggregate(sector, provider=provider)
    if agg.ok and agg.合计市值:
        u.占流通市值 = near_total / agg.合计市值 * 100

    u.ok = True
    if use_cache:
        path.write_text(json.dumps(u.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return u


if __name__ == "__main__":  # python -m core.unlock
    for sec in ["证券", "半导体", "白酒"]:
        r = sector_unlock(sec, use_cache=False)
        if not r.ok:
            print(f"[{sec}] {r.error}\n")
            continue
        mult = f"{r.倍数:.2f}x" if r.倍数 is not None else "-"
        pct = f"{r.占流通市值:.2f}%" if r.占流通市值 is not None else "-"
        print(f"[{sec}] 未来{NEAR_MONTHS}月解禁 {r.近端金额/1e8:.1f}亿（占流通市值 {pct}），"
              f"月均是后9个月的 {mult}，涉及 {r.涉及股票数} 只")
        for d in r.明细[:3]:
            print(f"     {d['日期']} {d['简称']} {d['金额']/1e8:.1f}亿")
        print()
