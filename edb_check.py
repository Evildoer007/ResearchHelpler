"""EDB 宏观指标查询助手 —— 在接入判定函数之前，先确认指标 ID 是否可用。

背景：宏观类 9 条论点（M1~M7）卡在"不知道 iFinD EDB 里这些指标的 ID 是多少"。

⚠ **只能用 ID，不能用名称**（2026-08-05 实测推翻了此前结论）：
`THS_EDB` / `THS_EDBQuery` 传中文名时**不做匹配**——它会返回某个**别的**指标，
同时把你传的名字原样回填到 `id` 字段，看起来像是查到了。真实身份只在 `index_name` 里。

    查 "中债国债到期收益率:10年"  → 实际返回「36个城市平均零售价:鸡蛋」(5.79)
    查 "中债国债到期收益率:7年"   → 实际返回「36个城市平均零售价:牛肉」(40.18)
    查 "36个城市平均零售价:鸡蛋"  → 实际返回「36城市零售价:油菜:当月值」(2.96)

胡编的名字返回空，这点为真——但"返回了数据"并**不**等于"返回了你要的指标"，
此前正是据此误判 10Y 国债已可取，把鸡蛋价格当成了利率。故本工具一律以
`index_name` 为准：名称查询一律判 ✗（除非返回名与请求名完全一致），ID 查询才可能判 ✔。

用法（可一次传多个，支持分号批量，速度与条数几乎无关）：

    python edb_check.py M001620326
    python edb_check.py M001620326 M001620327 M001620330

输出会给出每个 ID 的**真实指标名**、数据点数与最新值。看到 ✔ 即可用，把那几行发回即可接入。
"""

from __future__ import annotations

import sys

BEGIN, END = "2024-01-01", "2030-12-31"


def _looks_like_id(key: str) -> bool:
    """ID = 单个字母前缀 + 全数字。前缀不止 M：实测 L（中债/CFETS）、G（境外源）同样有效。"""
    return len(key) > 1 and key[:1].isalpha() and key[1:].isdigit()


def _query(ids: list[str]) -> dict[str, tuple[str, int, str, object]]:
    """按 ID 批量查。返回 {id: (真实指标名, 点数, 最新时间, 最新值)}。

    分号批量：实测 500 个 ID 与 1 个 ID 耗时相当（均 3 秒上下），
    单条查询的旧口径（3.47 秒/条）不适用于批量。
    """
    import iFinDPy as ths

    d = ths.THS_EDB(";".join(ids), "", BEGIN, END)
    df = getattr(d, "data", None)
    if df is None or len(df) == 0:
        return {}

    out: dict[str, tuple[str, int, str, object]] = {}
    for iid in ids:
        sub = df[df["id"].astype(str) == iid]
        if len(sub) == 0:
            continue
        # 返回按时间倒序，取首行为最新
        out[iid] = (str(sub["index_name"].iloc[0]), len(sub),
                    str(sub["time"].iloc[0]), sub["value"].iloc[0])
    return out


def _check_name(name: str) -> bool:
    """名称查询：一律核对 index_name，不一致即判失败（这正是此前误判的根因）。"""
    import iFinDPy as ths

    d = ths.THS_EDBQuery(name, BEGIN, END)
    t = (d.get("tables") or [{}])[0]
    vals, times = t.get("value") or [], t.get("time") or []
    if not vals:
        print(f"  ✗ 「{name}」  查不到")
        return False

    got = t.get("index_name") or []
    real = str(got[0]) if isinstance(got, list) and got else str(got)
    if real.strip() == name.strip():
        print(f"  ✔ 「{name}」  {len(vals)} 点，最新 {times[0]} = {vals[0]}")
        return True
    print(f"  ✗ 「{name}」  **返回的不是这个指标**——实际是「{real}」"
          f"（{len(vals)} 点，最新 {times[0]} = {vals[0]}）")
    print("      名称通道不可信，请改用 ID。见本文件顶部说明。")
    return False


def main() -> None:
    keys = sys.argv[1:]
    if not keys:
        print(__doc__)
        print("示例（已知可用的一条，可用来确认环境正常）：")
        print("    python edb_check.py M001620326      # GDP:现价:累计值")
        return

    from core.provider import iFinDProvider

    prov = iFinDProvider()
    if not prov.available():
        print("iFinD 不可用：请检查 config.local.json 里的账号密码，或 iFinDPy 是否已安装")
        return
    prov._ensure_login()

    ids = [k for k in keys if _looks_like_id(k)]
    names = [k for k in keys if not _looks_like_id(k)]
    ok = 0

    if ids:
        print(f"按 ID 查询 {len(ids)} 项：")
        got = _query(ids)
        for iid in ids:
            if iid in got:
                nm, n, tm, v = got[iid]
                print(f"  ✔ {iid}  「{nm}」  {n} 点，最新 {tm} = {v}")
                ok += 1
            else:
                print(f"  ✗ {iid}  查不到（ID 不存在或该区间无数据）")

    if names:
        print(f"\n按名称查询 {len(names)} 项（⚠ 该通道不可信，仅用于确认）：")
        ok += sum(_check_name(n) for n in names)

    print(f"\n可用 {ok}/{len(keys)}。把带 ✔ 的行发回即可接入判定函数。")
    if names:
        print("提示：名称查询即使返回数据也基本不可信，请从 iFinD 终端取指标 ID（形如 M001620326）。")


if __name__ == "__main__":
    main()
