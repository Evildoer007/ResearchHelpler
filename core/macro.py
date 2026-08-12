"""宏观指标（iFinD EDB）—— 支撑论点库第七类 M1~M7。

## 为什么这个模块处处在做"身份核对"

EDB 有一个**会静默出错**的坑（DESIGN #35）：按**中文名**查询时接口不做任何匹配，
会返回另一个不相干的指标，却把你传的名字原样回填进 `id` 字段。实测查
「中债国债到期收益率:10年」返回的是「36个城市平均零售价:鸡蛋」(5.79)。
数据真实存在、点数对得上、能溯源——**只是它是鸡蛋**。这类错误 validator 抓不到
（数字确实来自一个真实序列），人工复核也几乎发现不了。

因此本模块的两条铁律：
  1. **只用 ID 取数，绝不用中文名**；
  2. 每次取数都拿返回的 `index_name` 与登记的期望名**逐字核对**，不一致就报错返回空，
     宁可让论点判不出来，也不能拿错指标去判。

## 频率

EDB 指标频率不一（10Y国债日频、M2月频、GDP季频），故窗口一律按**自然日**回溯
并取"不晚于目标日的最后一个观测"，而不是按观测条数。这样同一套代码对日/月/季频都成立。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from . import config
from .provider import iFinDProvider

# 已实测核验的 EDB 指标：业务名 → (ID, 接口返回的指标全称)
# 全称用于每次取数后的身份核对，多一个字都算不一致。
# 新增指标前先跑 `python edb_check.py <ID>`，把它打印的指标名原样抄进来。
# 同一指标常有多个来源，选取原则：优先**高频 + 长历史 + 权威源**。
# 备选源不删除（记在注释里），它们的价值是交叉校验——三条 10Y 国债分别报
# 1.7126/1.7121/1.7162，四条汇率报 6.7889/6.7533/6.7516/6.7509，互相咬合即说明取数没串。
INDICATORS: dict[str, tuple[str, str]] = {
    # 日频。备选 L001619518(CFETS,2008起)、M005959895(外汇交易中心,2015起)
    "10Y国债收益率": ("M001619604", "中债国债到期收益率:10年"),
    # 月频。备选 M003786497 为**季频**，粒度更粗，不用
    "M2同比": ("M001625222", "M2(货币和准货币):同比"),
    # 月频。⚠ 另一条 M004028010「社会融资规模存量:同比」是**年频**且只到 2025-12，不可用
    "社融存量同比": ("M004891021", "社会融资规模存量:期末同比"),
    # 月频，国家统计局，1987 起。备选 G020207919 为欧盟统计局转载，历史更短
    "CPI当月同比": ("M002826730", "CPI:当月同比"),
    # 日频，1994 起，更新最及时。备选 M004370159(即期16:30)、G003146252(XE)、G002600864(美联储)
    "美元兑人民币": ("M002842089", "中间价:美元兑人民币"),
    # 月频，1996 起。同名变体中只有"当月同比"与 CPI 口径可比；环比/累计同比/年频均不取
    "PPI当月同比": ("M002826865", "PPI:当月同比"),
    # 月频，2005 起。备选 M004386385(综合PMI产出指数,2017起) 历史更短；制造业 PMI 才是市场看的那条
    "制造业PMI": ("M002043802", "制造业PMI"),
}


@dataclass
class Trend:
    """某宏观指标的当前值、区间变动与历史分位。"""

    指标: str = ""
    当前值: float | None = None
    当前日期: str = ""
    前值: float | None = None
    前值日期: str = ""
    变动: float | None = None        # 当前值 - 前值，原单位（利率为百分点）
    变动bp: float | None = None      # 利率类专用：变动 × 100
    窗口天: int = 0
    分位: float | None = None        # 当前值在全序列中的分位（%）
    样本数: int = 0
    起始: str = ""
    ok: bool = False
    error: str = ""


def _cache_path(ind_id: str, years: int):
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_CACHE_DIR / f"edb_{ind_id}_{years}y_{dt.date.today():%Y%m%d}.json"


def series(key: str, *, years: int = 10, use_cache: bool = True) -> list[tuple[str, float]]:
    """取某宏观指标的历史序列，按日期升序返回 [(日期, 值)]。

    key 为 INDICATORS 的业务名（如 "10Y国债收益率"）。取不到或**身份核对不通过**均返回空。
    """
    reg = INDICATORS.get(key)
    if reg is None:
        return []
    ind_id, expect_name = reg

    path = _cache_path(ind_id, years)
    if use_cache and path.exists():
        try:
            return [(d, float(v)) for d, v in json.loads(path.read_text(encoding="utf-8"))]
        except Exception:
            pass

    prov = iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()
    import iFinDPy as ths

    end = dt.date.today()
    begin = end.replace(year=end.year - years)
    d = ths.THS_EDB(ind_id, "", begin.isoformat(), end.isoformat())
    df = getattr(d, "data", None)
    if df is None or len(df) == 0:
        return []

    # ★ 身份核对：这一步就是 #35 的防线，不可省。
    got_name = str(df["index_name"].iloc[0]).strip()
    if got_name != expect_name:
        raise RuntimeError(
            f"EDB 指标身份不符：ID {ind_id} 期望「{expect_name}」，实际返回「{got_name}」。"
            f"请用 `python edb_check.py {ind_id}` 复核后更新 macro.INDICATORS。")

    rows: list[tuple[str, float]] = []
    for t, v in zip(df["time"], df["value"]):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:                       # NaN
            continue
        rows.append((str(t), f))
    rows.sort()                          # 接口返回为倒序，统一成升序

    if rows and use_cache:
        path.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def trend(key: str, *, lookback_days: int = 90, years: int = 10) -> Trend:
    """指标在近 lookback_days 个**自然日**内的变动，以及当前值的历史分位。

    按自然日而非观测条数回溯，故日频/月频/季频指标共用同一套逻辑。
    """
    t = Trend(指标=key, 窗口天=lookback_days)
    try:
        rows = series(key, years=years)
    except RuntimeError as e:
        t.error = str(e)
        return t
    if len(rows) < 30:
        t.error = f"{key} 历史样本不足({len(rows)}点)"
        return t

    cur_d, cur = rows[-1]
    target = dt.date.fromisoformat(cur_d) - dt.timedelta(days=lookback_days)
    past = [r for r in rows if dt.date.fromisoformat(r[0]) <= target]
    if not past:
        t.error = f"{key} 无 {lookback_days} 天前的观测"
        return t

    prev_d, prev = past[-1]
    vals = [v for _d, v in rows]
    t.当前值, t.当前日期 = cur, cur_d
    t.前值, t.前值日期 = prev, prev_d
    t.变动 = cur - prev
    t.变动bp = (cur - prev) * 100
    t.分位 = sum(1 for v in vals if v <= cur) / len(vals) * 100
    t.样本数 = len(rows)
    t.起始 = rows[0][0]
    t.ok = True
    return t


def rate_10y(*, lookback_days: int = 90) -> Trend:
    """10 年期国债到期收益率的走势——M1/M2 的判定依据（DDM 分母端）。"""
    return trend("10Y国债收益率", lookback_days=lookback_days)


if __name__ == "__main__":  # python -m core.macro
    r = rate_10y()
    if not r.ok:
        print("失败:", r.error)
    else:
        print(f"{r.指标}：{r.当前日期} = {r.当前值:.4f}%")
        print(f"  近{r.窗口天}天：{r.前值日期} {r.前值:.4f}% → {r.当前值:.4f}%"
              f"  变动 {r.变动bp:+.1f}bp")
        print(f"  历史分位 {r.分位:.1f}%（{r.起始} 起，{r.样本数} 个观测）")
