"""端到端入口。

两种选题方式：
  A【主】人工给需求（老板/销售/客户的一段话）
      python main.py -b "昨晚SK海力士发了业绩，芯片板块最近明显回调，有无相关板块建议？"
  B【辅】App 扫市场推荐候选主题
      python main.py            # 打印候选清单
      python main.py 1 4        # 生成第 1、4 个候选的一页通

加 --pick 进入**人工勾选论点**模式（DESIGN §7.4）：
      python main.py -b "需求…" --pick
  先打印本次被真实数据触发的论点清单，由分析师勾选 2~3 条作正文主轴，
  再继续生成。不加 --pick 时仍由 planner 自动挑，行为不变——
  批量生成与将来的 GUI 都不能被交互卡住。

流程（DESIGN §3）：
  需求/信号 → brief或topics → 【人工确认】 → 触发引擎 →【人工勾选(可选)】
  → planner → fetcher → writer → validator → 一页通
"""

from __future__ import annotations

import sys
from pathlib import Path

from core import brief, pipeline, thesis, topics, validator, writer
from render import gaps, layout


def _safe_name(s: str) -> str:
    return "".join(c for c in s if c.isalnum() or c in "（）()-_")[:30]


def _finish(ma, title: str) -> str | None:
    """共用收尾：撰写 → 校验 → 输出一页通。"""
    if not ma.ok:
        print(f"  ✗ 规划/取数失败：{ma.error}")
        return None
    auto = sum(len(lw.auto) for lw in ma.logics)
    print(f"  · 规划 {len(ma.logics)} 条逻辑，自动取数 {auto} 项，缺口 {len(ma.gap_fields)} 项")
    if ma.gap_fields:
        # 连原因一起打印。只报字段名时排查会误判——实测把 akshare 接口故障
        # 当成了板块名匹配 bug，两者的 note 其实写得很清楚，只是没被显示出来。
        print("      待补字段：")
        for f in ma.gap_fields:
            fv = ma.field_values.get(f)
            print(f"        · {f}：{getattr(fv, 'note', '') or '原因未记录'}")
    if ma.外部事实待补:
        print(f"  · 外部事实待人工填写：{ma.外部事实待补}")

    rc = writer.write(ma)
    if not rc.ok:
        print(f"  ✗ 撰写失败：{rc.error}")
        return None

    if rc.空缺逻辑:
        print(f"  ⚠ 正文空缺：{rc.空缺逻辑}（模型漏写，建议重跑或人工补写）")

    vr = validator.validate(rc, ma.field_values)
    flag = "全部可溯源" if vr.ok else f"⚠ {len(vr.suspects)} 处待复核"
    print(f"  · 数字校验：{len(vr.findings)} 个数字，{flag}")
    for f in vr.suspects:
        if f.判定 == validator.DIRECTION:
            # 方向抄反比数字对不上更危险：数字都对，只是涨说成跌
            print(f"      ‼ [{f.位置}] {f.数字} **方向可能抄反**（真值 {f.匹配字段} 符号相反）")
        else:
            print(f"      ✗ [{f.位置}] {f.数字} 无匹配真值")

    out_dir = Path(__file__).resolve().parent / "output"
    out_dir.mkdir(exist_ok=True)
    out = str(out_dir / f"onepager_{_safe_name(title)}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(layout.build_html(ma, rc))
    print(f"  ✓ 已输出：{out}")

    # 内部底稿落盘：终端打印关窗即失，而"补数据/复核"往往是隔几天才回头做的事。
    # 分析口径、观点包、缺口、复核项合成一份，不拆成多个文件让人对着看（#70）。
    gap_path = gaps.write_gap_report(ma, rc, vr, title=title, html_path=out)
    print(f"  ✓ 内部底稿：{gap_path}")
    return out


def _ask_picks(prepared) -> list[str] | None:
    """打印候选清单并读取分析师勾选。返回选中的 id 列表；直接回车 = 交回自动挑选。

    候选含两类：数据触发的论点（阈值判定）与研报提炼的观点（需点开原文核对）。
    编号跨两类连续，由 pipeline.candidates 统一分配，渲染与选号共用同一顺序。
    """
    cands = pipeline.candidates(prepared)
    print()
    print(pipeline.render_candidates(cands))
    if not cands:
        return None

    n_doc = sum(1 for c in cands if c.kind == "doc")
    tip = "；研报观点必须核对原文后再选" if n_doc else ""
    print(f"请勾选作正文主轴的序号（空格分隔，建议 2~3 条{tip}；直接回车＝由系统自动挑）")
    try:
        raw = input("  > ").strip()
    except EOFError:          # 非交互环境（重定向/管道）下退回自动
        return None
    if not raw:
        return None

    picks = [int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()]
    chosen = pipeline.select_candidates(cands, picks)
    if not chosen:
        print("  ⚠ 未识别到有效序号，改由系统自动挑选。")
        return None

    print(f"  已选 {len(chosen)} 条：" + "、".join(c.名称[:18] for c in chosen))
    # 以下均为出声提示，不阻拦——挑哪几条是分析师的判断
    cats = list(dict.fromkeys(c.类别 for c in chosen))
    if len(chosen) >= 2 and len(cats) == 1:
        print(f"  ⓘ 这几条都来自「{cats[0]}」，报告将只有一个观察视角。")
    dirs = [c.方向 for c in chosen if c.方向]
    if len(dirs) >= 2 and len({d[:2] for d in dirs}) == 1:
        print(f"  ⓘ 这几条方向一致（{dirs[0]}），报告不会呈现分歧。")
    if len(chosen) > 3:
        print(f"  ⓘ 选了 {len(chosen)} 条，一页通版面通常只容得下 2~3 条，每条会被压薄。")
    return [c.id for c in chosen]


def generate(cand: topics.TopicCandidate, *, pick: bool = False) -> str | None:
    """B路径：对一个已勾选的候选主题生成一页通。"""
    if not cand.可直接取数:
        print(f"  ⚠ 标的未通过校验（{cand.代码校验}），跳过。请人工确认代码后重试。")
        return None
    print(f"\n▶ 生成：{cand.主题}〔{cand.类型}〕代表标的 {cand.建议标的} {cand.建议标的代码}")

    prepared = chosen = None
    if pick:
        print("  摸底取数、跑触发引擎、抽取 sources/ 研报…")
        prepared = pipeline.prepare(cand.建议标的代码, with_docs=True)
        chosen = _ask_picks(prepared)
    ma = pipeline.run(cand.主题, cand.类型, cand.建议标的代码,
                      prepared=prepared, chosen=chosen)
    return _finish(ma, cand.主题)


def generate_from_brief(text: str, *, pick: bool = False) -> str | None:
    """A路径【主】：人工给一段口语化需求 → 解析 → 生成一页通。"""
    print("解析需求…")
    b = brief.parse(text)
    print(brief.render(b))
    if not b.ok:
        return None
    t = b.代表标的
    if t is None or not t.可用:
        print("  ⚠ 未能确定可用的代表标的，请人工指定证券代码后重试。")
        return None
    print(f"\n▶ 生成：{b.主题}  代表标的 {t.名称} {t.代码}")

    prepared = chosen = None
    if pick:
        print("  摸底取数、跑触发引擎、抽取 sources/ 研报…")
        prepared = pipeline.prepare_from_brief(b, with_docs=True)
        if prepared is not None:
            chosen = _ask_picks(prepared)
    return _finish(pipeline.run_from_brief(b, prepared=prepared, chosen=chosen), b.主题)


def main() -> None:
    args = sys.argv[1:]
    pick = "--pick" in args
    args = [a for a in args if a != "--pick"]

    # A【主路径】人工给需求
    if args and args[0] in ("-b", "--brief"):
        text = " ".join(args[1:]).strip()
        if not text:
            print('用法：python main.py -b "你的需求，例如：昨晚SK海力士发了业绩……"')
            return
        generate_from_brief(text, pick=pick)
        return

    # B【辅路径】App 扫市场推荐候选
    picks = [int(a) for a in args if a.isdigit()]
    print("采集市场信号并提炼候选主题…")
    slate = topics.propose()
    print(topics.render_slate(slate))
    if not slate.ok:
        return

    if not picks:
        print("→ 勾选方式：python main.py 1 4   （可选多个，数量不固定）")
        print("→ 想自己挑论证角度：加 --pick")
        return

    chosen = topics.select(slate, picks)
    print(f"已勾选 {len(chosen)} 个主题，开始生成…")
    outs = [p for c in chosen if (p := generate(c, pick=pick))]
    print(f"\n完成 {len(outs)}/{len(chosen)} 份一页通：{outs}")


if __name__ == "__main__":
    main()
