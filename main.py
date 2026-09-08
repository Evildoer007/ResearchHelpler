"""端到端入口。

两种选题方式：
  A【主】人工给需求（老板/销售/客户的一段话）
      python main.py -b "昨晚SK海力士发了业绩，芯片板块最近明显回调，有无相关板块建议？"
  B【辅】App 扫市场推荐候选主题
      python main.py            # 打印候选清单
      python main.py 1 4        # 生成第 1、4 个候选的一页通

加 --pdf 同时导出 PDF（A2，走 QtWebEngine）：
      python main.py -b "需求…" --pdf
  默认只出 HTML；--pdf 会额外渲染一份 PDF 并报出**真实页数**。

加 --optionhelper recommend 先运行最新版 OptionHelper Recommender；确认候选后以
--optionhelper quote 生成正式参考报价。两者均要求 Skill 根目录、明确选择的解释器与项目
memory 就绪；报价成功时页面最下方展示本次冻结的报价表。失败不阻断主报告。

客户条件可在首次运行时明确输入（未填项使用项目默认档案）：
      --horizon 6个月 --max-loss 20% --principal-fluctuation yes
      --return-preference "更偏上涨参与"

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
import warnings
import json
from dataclasses import asdict
from pathlib import Path

# 终端输出含 ✓ ✗ ⚠ 与中文，而 Windows 控制台/重定向默认走 GBK，
# 一遇到这些字符就 UnicodeEncodeError 整个进程崩掉——报告已经生成完了，
# 却因为一句提示语打不出来而报错退出。各测试脚本一直在自己开头做这件事，
# 入口反而漏了。放在 import 之后、任何 print 之前。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", line_buffering=True)
    except (AttributeError, OSError):   # 已是 utf-8 或不支持 reconfigure
        pass

# AkShare 内部 DataFrame 切片会触发 pandas 的 SettingWithCopyWarning；它不对应本项目
# 的数据失败，却会淹没阶段进度。只过滤 akshare 模块发出的这一类第三方告警。
try:
    from pandas.errors import SettingWithCopyWarning
    warnings.filterwarnings("ignore", category=SettingWithCopyWarning, module=r"akshare\\..*")
except ImportError:
    pass

_WANT_PDF = False        # 由 --pdf 打开，见 main()
_OH_OUTPUT = ""          # 由 --optionhelper quote|recommend 打开，见 main()
_CLIENT_CONSTRAINTS = None  # 在 main() 解析为 ClientConstraints

from core import (brief, event_evidence, market_confirmation, overrides as ov, pipeline,
                  thesis, topics, validator, writer)
from core.client_constraints import ClientConstraints, parse_cli as parse_client_constraints
from core.provider import get_provider
from core.run_tracker import RunTracker
from render import gaps, layout


def _safe_name(s: str) -> str:
    return "".join(c for c in s if c.isalnum() or c in "（）()-_")[:30]


def _finish(ma, title: str, *, tracker: RunTracker | None = None,
            client_product_intent: str = "", research_only: bool = False) -> str | None:
    """共用收尾：撰写 → 校验 → 输出一页通。"""
    if not ma.ok:
        print(f"  ✗ 规划/取数失败：{ma.error}")
        if tracker:
            tracker.add_recovery("核对数据源与需求解析错误后重试研究流程。")
        return None
    auto = sum(len(lw.auto) for lw in ma.logics)
    手填 = [f for f, v in ma.field_values.items() if ov.is_manual(v)]
    print(f"  · 规划 {len(ma.logics)} 条逻辑，自动取数 {auto} 项，缺口 {len(ma.gap_fields)} 项"
          + (f"，人工填写 {len(手填)} 项" if 手填 else ""))
    if 手填:
        print(f"      人工填写字段（来源见底稿）：{'、'.join(手填)}")
    if ma.gap_fields:
        # 连原因一起打印。只报字段名时排查会误判——实测把 akshare 接口故障
        # 当成了板块名匹配 bug，两者的 note 其实写得很清楚，只是没被显示出来。
        print("      待补字段：")
        for f in ma.gap_fields:
            fv = ma.field_values.get(f)
            print(f"        · {f}：{getattr(fv, 'note', '') or '原因未记录'}")
    # 外部事实待补**不在终端打印**：它此刻无法行动（要填覆盖文件再重跑），
    # 完整清单连同可复制的覆盖文件模板都在内部底稿里，末尾会给出路径。
    if ma.外部事实已填:
        print(f"  · 已采用人工填写的外部事实 {len(ma.外部事实已填)} 条")

    if tracker:
        research_mode = str((getattr(ma, "市场确认", None) or {}).get("research_mode") or "")
        basket = list(getattr(ma, "研究篮子", None) or [])
        if research_mode == "theme_etf" and ma.field_values.get("__etf__"):
            research_target = f"主题 ETF：{ma.field_values.get('__etf__')} 的真实跟踪指数成分"
        elif research_mode == "theme_basket" or basket:
            research_target = f"人工主题篮子（{len(basket)}只）"
        else:
            research_target = f"标准行业：{ma.field_values.get('__sector__') or getattr(ma, 'sector', '') or '—'}"
        tracker.add_metadata("研究取数目标", research_target)
        # 兼容旧运行摘要读取器；值与“研究取数目标”一致，不再写成自动发现 ETF。
        tracker.add_metadata("分析标的", research_target)
        dates = sorted({getattr(value, "as_of", "") for value in ma.field_values.values()
                        if getattr(value, "ok", False) and getattr(value, "as_of", "")})
        if dates:
            tracker.add_metadata("数据截至/口径", "；".join(dates[:6]))
    if tracker:
        with tracker.stage("market_outlook", "生成研究观点") as stage:
            rc = writer.write(ma)
            if not rc.ok:
                tracker.fail_stage(stage, rc.error or "研究观点生成失败")
    else:
        rc = writer.write(ma)
    if not rc.ok:
        print(f"  ✗ 撰写失败：{rc.error}")
        if tracker:
            tracker.add_recovery("检查 DeepSeek 调用记录；瞬时网络错误可直接重试，额度截断需调整输出要求。")
        return None

    if rc.空缺逻辑:
        print(f"  ⚠ 正文空缺：{rc.空缺逻辑}（模型漏写，建议重跑或人工补写）")

    if tracker:
        with tracker.stage("validation", "校验数据与数字溯源"):
            vr = validator.validate(rc, ma.field_values)
    else:
        vr = validator.validate(rc, ma.field_values)
    flag = "全部可溯源" if vr.ok else f"⚠ {len(vr.suspects)} 处待复核"
    print(f"  · 数字校验：{len(vr.findings)} 个数字，{flag}")
    for f in vr.suspects:
        if f.判定 == validator.DIRECTION:
            # 方向抄反比数字对不上更危险：数字都对，只是涨说成跌
            print(f"      ‼ [{f.位置}] {f.数字} **方向可能抄反**（真值 {f.匹配字段} 符号相反）")
        else:
            print(f"      ✗ [{f.位置}] {f.数字} 无匹配真值")

    # 正式报价是慢速外部环节，不能让已完成的研究报告被它卡住。先写一份不含报价表的
    # 可用 HTML；报价完成/失败后再覆写同一路径并补上最终内部底稿。
    out = _report_path(title)
    # 研究完成后固化一份只含已验证市场事实的 OptionHelper 交接快照。桌面端后续推荐/报价
    # 直接消费它，绝不为产品环节重新解析需求、取数或调用研究 LLM。
    if tracker:
        try:
            from core import optionhelper_bridge as snapshot_ohb, viewpoint as snapshot_vpmod

            # 研究阶段的交接包只固化共同市场观点，不预选挂钩标的。
            # 标准行业/人工篮子/主题 ETF 都是研究取数路径；真正待报价
            # 标的必须在研究完成后由分析师从客户点名/系统候选中确认。
            snapshot_vp = snapshot_vpmod.build(ma, rc, include_underlying=False)
            if snapshot_vp.ok:
                snapshot_path = tracker.directory / f"{tracker.run_id}.optionhelper-handoff.json"
                snapshot_path.write_text(json.dumps({
                    "run_id": tracker.run_id,
                    "underlying": snapshot_vp.标的代码,
                    "market_view": snapshot_vp.市场展望.方向 or snapshot_vp.整体方向,
                    "client_product_intent": client_product_intent,
                    "market_prompt": snapshot_ohb.build_prompt(snapshot_vp),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                tracker.add_artifact("OptionHelper 观点包", snapshot_path)
        except Exception as error:
            print(f"  ⚠ 未能保存 OptionHelper 观点包快照：{type(error).__name__}: {error}")
    if _OH_OUTPUT == "quote" and not research_only:
        if tracker:
            with tracker.stage("research_report", "先交付研究报告"):
                _write_research_html(ma, rc, out, oh_result=None)
                tracker.add_artifact("研究报告（报价前）", out)
        else:
            _write_research_html(ma, rc, out, oh_result=None)
        print(f"  ✓ 研究报告已先输出：{out}（正式报价将独立更新）")

    # OptionHelper Recommender 先形成受控候选；它不写 pending selection、更不报价。
    oh_result = None
    if _OH_OUTPUT and not research_only:
        from core import optionhelper_bridge as ohb, viewpoint as vpmod

        vp = vpmod.build(ma, rc)
        if not vp.ok:
            print(f"  ⚠ OptionHelper 未调用：观点包不可用（{vp.error}）")
            if tracker:
                tracker.add_recovery("修复观点包缺口后，可仅重新发起正式报价。")
        else:
            if _OH_OUTPUT == "recommend":
                print("  · 正在调用 OptionHelper 结构推荐链路；不会发起正式报价…")
                # 结构推荐前由 Research Helper 为本轮挂钩标的取得统一画像；正式报价
                # 仍由 OptionHelper 自取实时定价行情、波动率曲面和交易日历。
                from core import product_profile
                profile = product_profile.collect(vp.标的代码, name=vp.标的名称)
                if tracker:
                    with tracker.stage("optionhelper_recommender", "生成 OptionHelper 结构推荐") as stage:
                        recommendation = ohb.recommend(
                            vp, client_constraints=_CLIENT_CONSTRAINTS,
                            client_product_intent=client_product_intent,
                            product_profile=profile,
                        )
                        if not recommendation.ok:
                            tracker.fail_stage(stage, recommendation.error)
                            tracker.add_recovery("补齐客户约束或检查 OptionHelper Recommender 后重新推荐。")
                        else:
                            path = tracker.directory / f"{tracker.run_id}.optionhelper-recommendation.json"
                            path.write_text(json.dumps({
                                "run_id": tracker.run_id,
                                "product_profile": profile,
                                "constraints": recommendation.constraints,
                                "candidates": recommendation.candidates,
                            }, ensure_ascii=False, indent=2), encoding="utf-8")
                            tracker.add_artifact("OptionHelper 推荐候选", path)
                            print(f"  ✓ OptionHelper 已形成 {len(recommendation.candidates)} 个结构候选")
                else:
                    recommendation = ohb.recommend(
                        vp, client_constraints=_CLIENT_CONSTRAINTS,
                        client_product_intent=client_product_intent,
                        product_profile=profile,
                    )
                    if not recommendation.ok:
                        print(f"  ⚠ OptionHelper Recommender 未完成：{recommendation.error}")
            else:
                print("  · 正在调用 OptionHelper 正式参考报价链路；研究报告会继续交付…")
                if tracker:
                    with tracker.stage("optionhelper_quote", "生成 OptionHelper 正式参考报价") as stage:
                        oh_result = ohb.run_full(vp, output_type=_OH_OUTPUT,
                                                  client_constraints=_CLIENT_CONSTRAINTS,
                                                  client_product_intent=client_product_intent,
                                                  run_id=tracker.run_id)
                        if not oh_result.ok:
                            tracker.fail_stage(stage, oh_result.error, detail=oh_result.stage)
                            tracker.add_recovery(oh_result.recovery_action)
                else:
                    oh_result = ohb.run_full(vp, output_type=_OH_OUTPUT,
                                              client_constraints=_CLIENT_CONSTRAINTS,
                                              client_product_intent=client_product_intent)
                if oh_result.ok:
                    row_count = sum(len(group.rows) for group in oh_result.quote_groups)
                    print(f"  ✓ OptionHelper：{oh_result.product_name}（{oh_result.product_id}）"
                          f" · 参考报价 {row_count} 行")
                    if oh_result.report_path:
                        print(f"      报告：{oh_result.report_path}")
                else:
                    print(f"  ⚠ OptionHelper 未完成（{oh_result.stage}）：{oh_result.error}")
                    if oh_result.recovery_action:
                        print(f"      下一步：{oh_result.recovery_action}")
                    if oh_result.missing:
                        for m in oh_result.missing:
                            print(f"      · 缺：{m}")
                if oh_result.selection_archive_path:
                    print(f"      本次 selection 已归档并失效：{oh_result.selection_archive_path}")
                    if tracker:
                        tracker.add_artifact("OptionHelper selection归档", oh_result.selection_archive_path)

    if tracker:
        with tracker.stage("report", "更新报告并生成内部底稿"):
            out, gap_path = _write_report_artifacts(ma, rc, vr, title, oh_result, html_path=out)
    else:
        out, gap_path = _write_report_artifacts(ma, rc, vr, title, oh_result, html_path=out)
    print(f"  ✓ 已输出：{out}")
    print(f"  ✓ 内部底稿：{gap_path}")
    if tracker:
        tracker.add_artifact("研究报告", out)
        tracker.add_artifact("内部底稿", gap_path)

    interactive_path, interactive_error = _write_interactive_review(ma, rc, out)
    if interactive_path:
        print(f"  ✓ 内部交互复核：{interactive_path}")
        if tracker:
            tracker.add_artifact("内部交互复核", interactive_path)
    elif interactive_error:
        # 不把一份辅助复核页的错误升级为研究交付失败，但必须留下可读诊断。
        print(f"  ⚠ 未生成内部交互复核：{interactive_error}")
        if tracker:
            tracker.add_metadata("内部交互复核", f"未生成：{interactive_error}")

    # PDF 页数是“一页通”正式交付的硬门：HTML 可无限滚动，不能替代真实打印结果。
    # 不导出 PDF 的运行仍保留研究 HTML，但只能算内部草稿；绝不能把“没有测过”写成“一页”。
    if _WANT_PDF:
        try:
            from render import pdf_out
            layout_audit: dict = {}
            if tracker:
                with tracker.stage("pdf", "导出 PDF 并校验一页") as stage:
                    pdf = pdf_out.html_to_pdf(out, layout_audit=layout_audit)
                    n = pdf_out.page_count(pdf)
                    if n != 1:
                        tracker.fail_stage(stage, f"PDF 实测 {n} 页，不满足一页通正式交付条件")
            else:
                pdf = pdf_out.html_to_pdf(out, layout_audit=layout_audit)
                n = pdf_out.page_count(pdf)
            passed = n == 1
            if passed:
                print(f"  ✓ PDF：{pdf}（实测 1 页，已通过一页通交付校验）")
            else:
                print(f"  ⚠ PDF：{pdf}（实测 {n} 页，**不得作为正式一页通交付**；"
                      "压缩建议已写入内部底稿）")
            gaps.append_delivery_check(gap_path, ma, rc, oh_result, pdf_pages=n,
                                       layout_audit=layout_audit)
            if tracker:
                tracker.add_artifact("PDF（正式交付）" if passed else "PDF（超页，仅供内部复核）", pdf)
                tracker.add_metadata("一页通交付校验", "通过：PDF 实测 1 页" if passed
                                     else f"不通过：PDF 实测 {n} 页，禁止正式交付")
                if not passed:
                    overflow = (layout_audit.get("overflow") or [])
                    labels = []
                    for item in overflow:
                        if isinstance(item, dict) and str(item.get("label") or "").strip():
                            label = str(item["label"]).strip()
                            if label not in labels:
                                labels.append(label)
                    if labels:
                        tracker.add_metadata("超页区块（渲染定位）", "、".join(labels[:5]))
                    tracker.add_recovery("压缩底稿“正式交付校验”列出的图表/正文后重新导出 PDF；"
                                         "实测 1 页前不得对外发送。")
        except Exception as e:
            print(f"  ⚠ PDF 导出失败（不影响 HTML 与底稿）：{type(e).__name__}: {e}")
            gaps.append_delivery_check(gap_path, ma, rc, oh_result,
                                       pdf_error=f"{type(e).__name__}: {e}")
            if tracker:
                tracker.add_metadata("一页通交付校验", "未通过：PDF 未成功导出，不能确认单页")
                tracker.add_recovery("PDF 导出失败不影响 HTML；检查 Chromium/QtWebEngine 后可单独重试导出。")
    else:
        print("  ⓘ 未导出 PDF：研究 HTML 已生成，但一页通正式交付状态为“未校验”。")
        gaps.append_delivery_check(gap_path, ma, rc, oh_result)
        if tracker:
            tracker.add_metadata("一页通交付校验", "未校验：未导出 PDF，仅可作内部草稿")
            tracker.add_recovery("正式交付前请用 --pdf 重跑并确认 PDF 实测为 1 页。")
    return out


def _report_path(title: str) -> str:
    out_dir = Path(__file__).resolve().parent / "output"
    out_dir.mkdir(exist_ok=True)
    return str(out_dir / f"onepager_{_safe_name(title)}.html")


def _write_research_html(ma, rc, html_path: str, oh_result=None) -> None:
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(layout.build_html(ma, rc, oh_result=oh_result))


def _write_report_artifacts(ma, rc, vr, title: str, oh_result,
                            *, html_path: str = "") -> tuple[str, str]:
    """先交付研究报告，再保留正式报价的成败；报价失败不得吞掉研究成果。"""
    out = html_path or _report_path(title)
    _write_research_html(ma, rc, out, oh_result=oh_result)
    gap_path = gaps.write_gap_report(ma, rc, vr, title=title, html_path=out, oh_result=oh_result)
    return out, gap_path


def _interactive_review_path(report_path: str) -> str:
    path = Path(report_path)
    return str(path.with_name(f"{path.stem}_内部交互复核.html"))


def _write_interactive_review(ma, rc, report_path: str) -> tuple[str, str]:
    """生成非阻断的分析师交互复核页。

    它只复用本次已经确认的数据；pyecharts 或本地 JS 缺失不能影响客户版 HTML、
    内部底稿和 PDF 的交付。
    """
    try:
        from render.interactive import render_internal_review
        path = render_internal_review(ma, rc, _interactive_review_path(report_path))
        return path, ""
    except Exception as error:
        return "", f"{type(error).__name__}: {error}"


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

    # GUI 与命令行共用这一段交互。终端仍显示可读清单并等待编号；GUI 则消费
    # 这条结构化标记，弹出勾选卡后把同样的编号写回 stdin。候选的原文和出处
    # 只在分析师勾选后才进入正文，不能由模型静默挑选研报结论。
    picker_payload = {
        "candidates": [
            {
                "index": index,
                "id": candidate.id,
                "kind": candidate.kind,
                "name": candidate.名称,
                "category": candidate.类别,
                "direction": candidate.方向,
                "basis": candidate.依据,
                "source": candidate.出处,
                "evidence_scope": candidate.证据范围,
                "evidence_subject": candidate.证据主体,
            }
            for index, candidate in enumerate(cands, 1)
        ],
        "suggested": pipeline.recommend(cands),
    }
    print("LOGIC_PICK_REQUIRED=" + json.dumps(picker_payload, ensure_ascii=False), flush=True)

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


def generate(cand: topics.TopicCandidate, *, pick: bool = False,
             tracker: RunTracker | None = None) -> str | None:
    """B路径：对一个已勾选的候选主题生成一页通。"""
    if not cand.可直接取数:
        print(f"  ⚠ 标的未通过校验（{cand.代码校验}），跳过。请人工确认代码后重试。")
        return None
    print(f"\n▶ 生成：{cand.主题}〔{cand.类型}〕代表标的 {cand.建议标的} {cand.建议标的代码}")

    prepared = chosen = None
    if pick:
        print("  摸底取数、跑触发引擎、抽取 sources/ 研报…")
        if tracker:
            with tracker.stage("data_collection", "拉取行情并准备论点"):
                prepared = pipeline.prepare(cand.建议标的代码, with_docs=True)
        else:
            prepared = pipeline.prepare(cand.建议标的代码, with_docs=True)
        chosen = _ask_picks(prepared)
    if tracker:
        with tracker.stage("market_research", "拉取市场数据并形成研究底稿") as stage:
            ma = pipeline.run(cand.主题, cand.类型, cand.建议标的代码,
                              prepared=prepared, chosen=chosen)
            if not ma.ok:
                tracker.fail_stage(stage, ma.error or "市场研究生成失败")
    else:
        ma = pipeline.run(cand.主题, cand.类型, cand.建议标的代码,
                          prepared=prepared, chosen=chosen)
    return _finish(ma, cand.主题, tracker=tracker)


def generate_from_brief(text: str, *, pick: bool = False,
                        overrides_path: str = "", tracker: RunTracker | None = None,
                        confirm_market: bool = False) -> str | None:
    """A路径【主】：人工给一段口语化需求 → 解析 → 生成一页通。"""
    o = ov.load(overrides_path)
    if not o.ok:
        # 覆盖文件不合法直接停，不静默忽略——静默忽略会让分析师以为自己补上了，
        # 而报告里那条依然是缺的，且他不会再去检查（同 resolve_sector 的 n==0 处置）。
        print("✗ 覆盖文件不可用，已中止：")
        for e in o.errors:
            print(f"    · {e}")
        print(f"  可覆盖字段共 {len(ov.overridable_fields())} 个，"
              f"清单见 `python -m core.overrides`")
        return None

    print("解析需求…")
    if tracker:
        with tracker.stage("brief", "解析需求") as stage:
            b = brief.parse(text)
            if not b.ok:
                tracker.fail_stage(stage, b.error or "需求解析失败")
    else:
        b = brief.parse(text)
    # 略去"外部事实待补"：此刻无法行动，完整清单与覆盖模板都在内部底稿里
    print(brief.render(b, 含外部事实=False))
    if not b.ok:
        return None

    if market_confirmation.needs_confirmation(b):
        if not confirm_market:
            print("  ✗ 该需求需要分析师确认研究市场与取数目标；GUI 会自动弹出确认页。")
            print("    命令行请加 --confirm-market，并在提示后输入一行确认 JSON。")
            return None
        provider = get_provider()
        payload = market_confirmation.proposal(b, provider=provider)
        while True:
            print("MARKET_CONFIRMATION_REQUIRED=" + json.dumps(payload, ensure_ascii=False), flush=True)
            line = sys.stdin.readline()
            if not line:
                print("  ✗ 未收到分析师确认，已停止。")
                return None
            try:
                raw_confirmation = json.loads(line)
            except json.JSONDecodeError as error:
                payload["errors"] = [f"确认 JSON 无效：{error}"]
                continue
            if raw_confirmation.get("cancelled"):
                print("  ✗ 分析师取消确认，已停止。")
                return None
            value = market_confirmation.from_dict(raw_confirmation)
            if tracker:
                with tracker.stage("market_confirmation", "校验分析师确认的研究市场与取数目标") as stage:
                    checked = market_confirmation.verify(value, b, provider=provider)
                    if not checked.ok:
                        tracker.fail_stage(stage, "；".join(checked.errors))
            else:
                checked = market_confirmation.verify(value, b, provider=provider)
            if not checked.ok:
                payload["errors"] = checked.errors
                if checked.exposure is not None:
                    payload["etf_exposure"] = asdict(checked.exposure)
                # 重新弹窗时保留分析师刚才的选择；代码采用后端规范化后的值，
                # 例如 516520.SS 会显示为 iFinD 使用的 516520.SH。
                payload["previous_confirmation"] = asdict(value)
                print("MARKET_CONFIRMATION_REJECTED=" + json.dumps(
                    {"errors": checked.errors}, ensure_ascii=False), flush=True)
                continue
            market_confirmation.apply_to_brief(checked, b, provider=provider)
            if tracker:
                tracker.add_metadata("分析师确认", json.dumps(b.市场确认, ensure_ascii=False))
                # 标准行业路径可以不预先选 ETF，但若客户明确需要产品，确认页已经
                # 根据主题/行业发现了一批可交易候选。冻结到本次 run，供研究完成后的
                # 报价审核选择；不能只因分析师暂不把 ETF 用作研究数据源就丢掉它们。
                suggested = [
                    {key: item.get(key) for key in ("code", "name", "type", "note", "origin",
                                                     "average_daily_amount", "exposure", "exposure_level",
                                                     "tracking_index")}
                    for item in (payload.get("suggested_instruments") or [])
                    if isinstance(item, dict) and str(item.get("code") or "").strip()
                ]
                if suggested:
                    tracker.add_metadata("系统建议挂钩工具", json.dumps(suggested, ensure_ascii=False))
            for warning in checked.warnings:
                print(f"  ⚠ {warning}")
            if checked.exposure is not None:
                print(f"  · ETF 主题暴露：{checked.exposure.label}｜{checked.exposure.reason}")
                if checked.exposure.tracking_index:
                    print(f"    跟踪指数：{checked.exposure.tracking_index}")
                if checked.exposure.major_constituents:
                    print(f"    主要成分：{'、'.join(checked.exposure.major_constituents[:5])}")
            target_label = (
                f"主题ETF {value.underlying_code}" if value.research_mode == "theme_etf"
                else (f"人工主题篮子 {len(value.theme_basket_codes)}只"
                      if value.research_mode == "theme_basket" else f"标准行业 {value.research_scope}")
            )
            print(f"  ✓ 已确认研究取数目标：{value.market}｜{target_label}"
                  + ("｜仅研究不报价" if value.research_only else ""))
            break

    # 事件本体不能由模型常识补全；A股暴露又必须等研究取数对象确认后才能精确检索。
    # 因此这里在行情研究开始前完成第二阶段证据审核，证据链不完整就停止生成报告。
    evidence = getattr(o, "事件证据", None) or event_evidence.parse(None)
    gate = event_evidence.assess(b, evidence)
    if gate.required and not gate.ready and confirm_market:
        confirmed = getattr(b, "市场确认", None) or {}
        exposure = confirmed.get("etf_exposure") if isinstance(confirmed.get("etf_exposure"), dict) else {}
        basket = [
            {"code": str(getattr(item, "代码", "") or ""),
             "name": str(getattr(item, "名称", "") or "")}
            for item in (getattr(b, "主题篮子候选", None) or [])
        ]
        research_context = {
            "market": str(getattr(b, "市场范围", "") or ""),
            "research_theme": str(getattr(b, "研究主题", "") or getattr(b, "主题", "") or ""),
            "research_mode": str(confirmed.get("research_mode") or "industry"),
            "research_scope": str(
                confirmed.get("research_scope")
                or getattr(b, "研究篮子口径", "")
                or "、".join(getattr(b, "涉及板块", None) or [])
            ),
            "theme_basket": basket,
            "underlying_code": str(confirmed.get("underlying_code") or ""),
            "underlying_name": str(exposure.get("official_name") or ""),
            "tracking_index": str(exposure.get("tracking_index") or ""),
            "major_constituents": list(exposure.get("major_constituents") or []),
        }
        print("EVENT_EVIDENCE_CONTEXT_REQUIRED=" + json.dumps({
            "message": "研究取数对象已确认，现可针对该对象查找A股暴露并组合传导链。",
            "missing": list(gate.missing),
            "research_context": research_context,
            "existing_evidence": event_evidence.to_dict(evidence),
        }, ensure_ascii=False), flush=True)
        line = sys.stdin.readline()
        if not line:
            print("  ✗ 未收到事件证据确认，已停止。")
            return None
        try:
            raw_evidence = json.loads(line)
        except json.JSONDecodeError as error:
            print(f"  ✗ 事件证据确认 JSON 无效：{error}")
            return None
        if raw_evidence.get("cancelled"):
            print("  ✗ 分析师取消事件证据确认，已停止。")
            return None
        evidence = event_evidence.parse(raw_evidence.get("event_evidence") or raw_evidence)
        gate = event_evidence.assess(b, evidence)
        if tracker and not evidence.errors:
            tracker.add_metadata("分析师确认事件证据", json.dumps(
                event_evidence.to_dict(evidence), ensure_ascii=False))
    if gate.required and not gate.ready:
        entity = getattr(getattr(b, "触发实体", None), "名称", "该事件主体")
        message = gate.message(entity)
        payload = {
            "message": message,
            "entity": entity,
            "missing": list(gate.missing),
            "template": event_evidence.template(),
        }
        if tracker:
            with tracker.stage("event_evidence", "校验事件事实与产业链传导证据") as stage:
                tracker.fail_stage(stage, message)
            tracker.add_recovery("补充事件事实与传导证据后重跑；未补齐前不会生成报告或正式报价。")
        print("EVENT_EVIDENCE_REQUIRED=" + json.dumps(payload, ensure_ascii=False), flush=True)
        print(f"  ✗ {message}")
        print("    本次不会生成一页通或 OptionHelper 正式报价。")
        print("    请在 GUI 的「管理事件证据」中从已上传材料导入，或手工补录：")
        for item in gate.missing:
            print(f"      · {item}")
        print("    可用来源：公司 IR/交易所披露、已上传研报（写明页码）、或其他可核验材料。")
        return None

    research_only = bool((getattr(b, "市场确认", None) or {}).get("research_only"))
    t = b.代表标的
    # 只有“主题 ETF 路径”中的 ETF 是研究取数目标；它会在研究完成后作为报价
    # 候选再次展示，但此处不能提前把它认定为正式挂钩标的。
    confirmed_code = str(getattr(b, "确认挂钩标的", "") or "").strip()
    confirmed_type = str(getattr(b, "确认挂钩标的类型", "") or "")
    confirmed = getattr(b, "市场确认", None) or {}
    confirmed_mode = str(confirmed.get("research_mode") or "industry")
    if (confirmed_code and "ETF" in confirmed_type.upper()
            and confirmed_mode == "theme_etf"):
        from core import instruments
        item = instruments.get(confirmed_code)
        t = brief.TargetRef(item.简称 if item else confirmed_code, confirmed_code,
                            "ok:分析师确认ETF")
    if (t is None or not t.可用) and b.市场范围 == "A股" and \
            confirmed_mode == "industry":
        # 标准行业路径本来就允许不选 ETF。若行业龙头检索在确认阶段短暂失败，
        # 再按分析师确认的标准行业补一次内部取数锚点；无论是否报价都不能误报成
        # “请确认 ETF”。
        from core import universe
        scope = str(confirmed.get("research_scope") or "").strip()
        lead = universe.pick_representative([scope], provider=get_provider()) if scope else None
        if lead:
            t = brief.TargetRef(lead.简称, lead.代码, f"ok:{lead.简称}（仅研究取数锚点）")
            b.候选标的 = [t]
    if t is None or not t.可用:
        message = ("当前研究口径没有取得可用的研究数据对象；"
                   "标准行业路径不需要 ETF，主题 ETF 路径才需要确认 ETF 代码。")
        print(f"  ⚠ {message}")
        print("    请改选可用的标准行业路径，或选择主题 ETF 取数路径并填写 ETF 代码后重试。")
        if tracker:
            tracker.add_metadata("终止原因", message)
            tracker.add_recovery("确认研究取数路径和可用研究对象后重试；无需为标准行业研究补填报价 ETF。")
        return None
    print(f"\n▶ 生成：{b.主题}  代表标的 {t.名称} {t.代码}")
    if not o.为空:
        print(f"  · 已载入覆盖文件 {o.path}"
              f"（字段 {len(o.字段覆盖)} 项、外部事实 {len(o.外部事实)} 条）")

    prepared = chosen = None
    if pick:
        print("  摸底取数、跑触发引擎、抽取 sources/ 研报…")
        if tracker:
            with tracker.stage("data_collection", "拉取行情并准备论点"):
                prepared = pipeline.prepare_from_brief(b, with_docs=True, overrides=o)
        else:
            prepared = pipeline.prepare_from_brief(b, with_docs=True, overrides=o)
        if prepared is not None:
            chosen = _ask_picks(prepared)
    if tracker:
        with tracker.stage("market_research", "拉取市场数据并形成研究底稿") as stage:
            ma = pipeline.run_from_brief(b, prepared=prepared, chosen=chosen, overrides=o)
            if not ma.ok:
                tracker.fail_stage(stage, ma.error or "市场研究生成失败")
    else:
        ma = pipeline.run_from_brief(b, prepared=prepared, chosen=chosen, overrides=o)
    if research_only and _OH_OUTPUT:
        print("  · 分析师选择“仅研究”，本次跳过 OptionHelper 正式报价。")
    return _finish(ma, b.主题, tracker=tracker, client_product_intent=b.客户产品诉求,
                   research_only=research_only)


def _run_tracked(tracker: RunTracker, work) -> object:
    """统一收尾，保证提前返回和异常也留下可追踪运行摘要。"""
    result = None
    try:
        with tracker.activate():
            result = work()
        status = "completed" if result else "failed"
        delivery = str(tracker.metadata.get("一页通交付校验") or "")
        if status == "completed" and delivery and not delivery.startswith("通过"):
            # 研究已生成，但不是可发给客户的正式一页通；不能用笼统的 completed 掩盖它。
            status = "completed_not_deliverable"
        elif status == "completed" and tracker.recovery_actions:
            status = "completed_with_warnings"
        error = ""
        if status == "failed":
            error = str(tracker.metadata.get("终止原因") or "运行未形成交付结果，请查看实时输出。")
        tracker.finish(status=status, error=error)
        return result
    except Exception as error:  # noqa: BLE001
        message = f"{type(error).__name__}: {str(error)[:300]}"
        print(f"\n✗ 运行异常：{message}")
        tracker.finish(status="failed", error=message)
        return None
    finally:
        tracker.print_summary()


def main() -> None:
    args = sys.argv[1:]
    pick = "--pick" in args
    args = [a for a in args if a != "--pick"]
    confirm_market = "--confirm-market" in args
    args = [a for a in args if a != "--confirm-market"]

    # --pdf：导出并实测一页。未加开关仍会出研究 HTML，但会明确标为“未校验草稿”。
    global _WANT_PDF
    if "--pdf" in args:
        _WANT_PDF = True
        args = [a for a in args if a != "--pdf"]

    # --optionhelper recommend|quote：先生成推荐候选，或使用已确认候选发起正式报价。
    global _OH_OUTPUT
    if "--optionhelper" in args:
        i = args.index("--optionhelper")
        if i + 1 >= len(args) or args[i + 1] not in {"recommend", "quote"}:
            print("用法：--optionhelper recommend|quote")
            return
        _OH_OUTPUT = args[i + 1]
        args = args[:i] + args[i + 2:]

    global _CLIENT_CONSTRAINTS
    args, parsed_constraints, constraint_error = parse_client_constraints(args)
    if constraint_error:
        print(f"客户条件错误：{constraint_error}")
        return
    _CLIENT_CONSTRAINTS = parsed_constraints

    # --overrides 路径：人工补数文件（模板由内部底稿生成，复制填好即可）
    overrides_path = ""
    if "--overrides" in args:
        i = args.index("--overrides")
        if i + 1 >= len(args):
            print("用法：--overrides 覆盖文件.json")
            return
        overrides_path = args[i + 1]
        args = args[:i] + args[i + 2:]

    # A【主路径】人工给需求
    if args and args[0] in ("-b", "--brief"):
        text = " ".join(args[1:]).strip()
        if not text:
            print('用法：python main.py -b "你的需求，例如：昨晚SK海力士发了业绩……"')
            return
        tracker = RunTracker.create(root=Path(__file__).resolve().parent, request=text, mode="brief")
        tracker.add_metadata("客户约束", _CLIENT_CONSTRAINTS.describe())
        print(f"运行编号：{tracker.run_id}（日志会写入 output/runs）")
        _run_tracked(tracker, lambda: generate_from_brief(
            text, pick=pick, overrides_path=overrides_path, tracker=tracker,
            confirm_market=confirm_market))
        return

    # B【辅路径】App 扫市场推荐候选
    tracker = RunTracker.create(root=Path(__file__).resolve().parent, request="市场扫描", mode="topics")
    tracker.add_metadata("客户约束", _CLIENT_CONSTRAINTS.describe())
    print(f"运行编号：{tracker.run_id}（日志会写入 output/runs）")

    def _topics_work():
        picks = [int(a) for a in args if a.isdigit()]
        print("采集市场信号并提炼候选主题…")
        with tracker.stage("market_scan", "采集市场信号并生成候选") as stage:
            slate = topics.propose()
            if not slate.ok:
                tracker.fail_stage(stage, slate.error or "市场扫描失败")
        print(topics.render_slate(slate))
        if not slate.ok:
            tracker.add_recovery("检查市场信号数据源与 DeepSeek 调用记录后重试扫描。")
            return None

        if not picks:
            print("→ 勾选方式：python main.py 1 4   （可选多个，数量不固定）")
            print("→ 想自己挑论证角度：加 --pick")
            return "候选已生成"

        chosen = topics.select(slate, picks)
        print(f"已勾选 {len(chosen)} 个主题，开始生成…")
        outs = [p for c in chosen if (p := generate(c, pick=pick, tracker=tracker))]
        print(f"\n完成 {len(outs)}/{len(chosen)} 份一页通：{outs}")
        return outs

    _run_tracked(tracker, _topics_work)


if __name__ == "__main__":
    main()
