"""OptionHelper 完整版（推荐+定价+回测+报告）桥接层。

OptionHelper 是独立项目，不在本仓库内；通过子进程调用它的
`scripts/tool_entry.py --project-json`，不在本进程内 import——它的
`requirements.lock` 锁定了 numpy/pandas/scipy/numba 的精确版本（见
`core.config.OPTIONHELPER_PYTHON` 的注释），混进本项目自己的环境有版本
冲突风险，必须用独立解释器隔离。

## 结构推荐的边界（呼应 core/viewpoint.py 的两条边界）

本模块只把 ViewPackage 拼成一段自然语言市场观点（方向、波动率、风险提示），
**不指定挂哪个产品结构**——OptionHelper 自己的 Recommender 会依据实时波动率
曲面和报价选结构，这是本系统做不到、也不该做的事（既没有曲面也没有报价）。
所以这里不走它 `selection`/`_agent_native_candidate` 的直传通道（那是给
"已经研究过、自己选好产品"的场景用的），而是走它正常的自然语言推荐入口，
让它自己的 Recommender（复用本项目已有的 DeepSeek key 做模型网关）来选结构。

## 为什么用子进程而不是 host_url 模式

`tool_entry._configuration()` 支持 `OPTIONHELPER_HOST_URL` 指向一个受控 Host
服务，但那是给"已经部署了 OptionHelper 服务端"的场景用的——本项目没有部署，
直接跑它本地项目级入口（`run_project_request`）更简单，且这正是它自己
文档里"对话 Agent 已经是模型，不该被要求配一个独立模型网关"这条设计的
适用场景（我们把 DeepSeek key 转手给它当模型网关用）。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field as dfield
from pathlib import Path

from . import config
from .viewpoint import ViewPackage

_TIMEOUT_S = 600  # 实时取数 + 路径模拟回测，比一次 LLM 调用慢得多，留够时间


@dataclass
class OptionHelperResult:
    ok: bool = False
    status: str = ""
    stage: str = ""            # 失败时来自哪一环：configuration/recommender/datafetcher/pricer/backtester/reporter/host
    message: str = ""
    error: str = ""
    missing: list = dfield(default_factory=list)
    product_id: str = ""
    product_name: str = ""
    reason: str = ""
    main_risks: list = dfield(default_factory=list)
    report_path: str = ""
    report_format: str = ""
    coverage_status: str = ""      # complete / partial
    module_failures: dict = dfield(default_factory=dict)
    assumptions: list = dfield(default_factory=list)
    raw: dict = dfield(default_factory=dict)


def missing_setup() -> list[str]:
    """未就绪的前置条件，供调用方在真正发起子进程前先给出可读提示。"""
    missing = []
    if not config.OPTIONHELPER_ROOT:
        missing.append("OPTIONHELPER_ROOT（option-helper 项目根目录，config.local.json 里配）")
    elif not (Path(config.OPTIONHELPER_ROOT) / "scripts" / "tool_entry.py").exists():
        missing.append(f"OPTIONHELPER_ROOT 指向的目录下找不到 scripts/tool_entry.py：{config.OPTIONHELPER_ROOT}")
    if not Path(config.OPTIONHELPER_PYTHON).exists():
        missing.append(f"OPTIONHELPER_PYTHON 指向的解释器不存在：{config.OPTIONHELPER_PYTHON}（需先装好其 requirements.lock）")
    if not config.OPTIONHELPER_IFIND_REFRESH_TOKEN:
        missing.append("IFIND_REFRESH_TOKEN（OptionHelper 自己的 iFind Refresh Token，与本项目 IFIND_ACCOUNT/PASSWORD 不是同一套）")
    if not config.has_llm():
        missing.append("DEEPSEEK_API_KEY（转手给 OptionHelper 当它的模型网关用）")
    return missing


def build_prompt(vp: ViewPackage) -> str:
    """把观点包整理成 OptionHelper 能读的**自然语言**需求——只描述市场状态，不点名结构。

    刻意用完整句子的散文，**不用表格、不用论点库代号（V1/E2…）、不带内部字段
    （数据代表标的等）**：OptionHelper 的 Recommender 是让另一个模型读这段文字来
    理解市场观点的，表格和代号它读不出意思，内部锚点字段只会干扰判断。每句都是
    人话，把"看多还是看空、依据是什么、有什么风险"讲清楚，别的都不给。
    """
    lines: list[str] = []
    if vp.标的名称:
        lines.append(f"拟挂钩标的是{vp.标的名称}（{vp.标的代码}）。")

    判断 = []
    if vp.整体方向:
        判断.append(f"整体方向{vp.整体方向}")
    if vp.波动率看法:
        判断.append(vp.波动率看法)
    if 判断:
        lines.append("当前市场判断：" + "，".join(判断) + "。")

    # 逻辑写成完整句子。市场含义本身就是 writer 归结的"这条逻辑对结构的含义"
    # （方向/时间尺度/波动率/失效条件），是干净散文，直接罗列即可，不带代号。
    含义 = [(lv.市场含义 or "").strip() for lv in vp.逻辑要点]
    含义 = [c for c in 含义 if c]
    if 含义:
        lines.append("主要判断依据如下：")
        lines += [f"{i}）{c}" for i, c in enumerate(含义, 1)]

    if vp.风险提示汇总:
        lines.append("需要注意的主要风险：" + "；".join(vp.风险提示汇总[:3]) + "。")

    lines.append("请据此推荐合适的期权结构，并给出定价与历史回测。")
    return "\n".join(lines)


def run_full(
    vp: ViewPackage,
    *,
    output_type: str = "card",
    project_root: str | Path | None = None,
) -> OptionHelperResult:
    """调用完整版：推荐 + 定价 + 回测 + 报告。

    output_type: "card"（研究简报，字段少）或 "report"（完整研究报告）。
    project_root: OptionHelper 自己的项目级 Store（data/result/.optionhelper/runtime）
    放在哪；默认放本项目根目录下的 `.optionhelper/`。
    """
    if not vp.ok or not vp.标的代码:
        return OptionHelperResult(ok=False, stage="request", error="观点包未就绪或挂钩标的未定，无法提交给 OptionHelper")

    missing = missing_setup()
    if missing:
        return OptionHelperResult(ok=False, stage="configuration", missing=missing,
                                   error="OptionHelper 完整版尚未就绪，缺：" + "；".join(missing))

    tool_entry = Path(config.OPTIONHELPER_ROOT) / "scripts" / "tool_entry.py"
    body = {"prompt": build_prompt(vp), "output_type": output_type, "format": "html"}

    root = Path(project_root) if project_root else Path.cwd() / ".optionhelper"
    root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    # 同 core/config.no_proxy()：DeepSeek、iFind 都是国内站点，本机常开的代理
    # 转到境外出口会 ProxyError；子进程是独立环境变量表，需在这里单独清一次。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    # 实测坑：Windows 上子进程的 stdout 一旦被管道接走（不是真终端），Python
    # 默认按系统区域码页编码（这台机器是 gbk），不是 UTF-8——即使子进程自己用
    # `json.dumps(..., ensure_ascii=False)` 输出了干净的中文，写出来的字节仍是
    # gbk。下面这行 subprocess.run 用 encoding="utf-8" 严格解码，两边一对不上，
    # 第一次直接把读线程崩了（proc.stdout 变 None），加了 errors="replace" 后
    # 不崩了但中文错误信息全烂成替换符。根子在这里：逼子进程自己按 UTF-8 编码。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["OPTIONHELPER_MODEL_BASE_URL"] = config.DEEPSEEK_BASE_URL
    env["OPTIONHELPER_MODEL"] = config.DEEPSEEK_MODEL
    env["OPTIONHELPER_MODEL_API_KEY"] = config.DEEPSEEK_API_KEY
    env["IFIND_REFRESH_TOKEN"] = config.OPTIONHELPER_IFIND_REFRESH_TOKEN
    env["OPTIONHELPER_DATA_ROOT"] = str(root / "data")
    env["OPTIONHELPER_RESULT_ROOT"] = str(root / "result")
    env["OPTIONHELPER_RUNTIME_ROOT"] = str(root / "runtime")

    try:
        proc = subprocess.run(
            [config.OPTIONHELPER_PYTHON, str(tool_entry),
             "--project-json", json.dumps(body, ensure_ascii=False)],
            cwd=str(root), env=env, capture_output=True, text=True, encoding="utf-8",
            # 子进程栈内某个依赖（numba/iFinD SDK 之类）偶发往 stderr 打非 UTF-8
            # 字节（Windows 默认 GBK 控制台），strict 解码会直接炸穿 subprocess 的
            # 后台读线程，导致 proc.stdout 整个变 None——不是取不到数据，是读都没读到。
            errors="replace",
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return OptionHelperResult(ok=False, stage="host",
                                   error=f"OptionHelper 完整版运行超过 {_TIMEOUT_S}s 未完成，可能卡在取数或路径回测")
    except OSError as e:
        return OptionHelperResult(ok=False, stage="host", error=f"无法启动 OptionHelper 子进程：{e}")

    # main() 无论成功失败都只向 stdout 打印一段 json.dumps(..., indent=2)；
    # 逐步进度走 stderr（--project-json 内部的 emit 回调），这里不需要它。
    try:
        out = json.loads(proc.stdout) if proc.stdout and proc.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    if not out:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:]) if proc.stderr else "（无 stderr 输出）"
        return OptionHelperResult(ok=False, stage="host",
                                   error=f"OptionHelper 未返回可解析结果（退出码 {proc.returncode}）：{tail}")

    if out.get("ok") is not True:
        return OptionHelperResult(
            ok=False, status=str(out.get("status", "")), stage=str(out.get("stage", "")),
            error=str(out.get("message", "")), missing=list(out.get("missing", [])), raw=out,
        )

    summary = out.get("user_summary", {}) if isinstance(out.get("user_summary"), dict) else {}
    rec = summary.get("recommendation", {}) if isinstance(summary.get("recommendation"), dict) else {}
    rep = summary.get("report", {}) if isinstance(summary.get("report"), dict) else {}
    return OptionHelperResult(
        ok=True, status=str(out.get("status", "")), message=str(out.get("message", "")),
        product_id=str(rec.get("product_id", "")), product_name=str(rec.get("product_name", "")),
        reason=str(rec.get("reason", "")), main_risks=list(rec.get("main_risks", [])),
        report_path=str(rep.get("path", "")), report_format=str(rep.get("format", "")),
        coverage_status=str(rep.get("coverage_status", "")),
        module_failures=dict(summary.get("module_failures", {})) if isinstance(summary.get("module_failures"), dict) else {},
        assumptions=list(summary.get("assumptions", [])) if isinstance(summary.get("assumptions"), list) else [],
        raw=out,
    )


if __name__ == "__main__":  # python -m core.optionhelper_bridge <代码> [板块] [card|report]
    import sys

    from . import genres as gr, pipeline, viewpoint, writer

    code = sys.argv[1] if len(sys.argv) > 1 else "600030.SH"
    sector = sys.argv[2] if len(sys.argv) > 2 else "证券"
    output_type = sys.argv[3] if len(sys.argv) > 3 else "card"

    problem = missing_setup()
    if problem:
        print("前置条件未就绪：")
        for m in problem:
            print(f"  · {m}")
        sys.exit(1)

    ma = pipeline.run("板块投资机会测试", gr.TYPE_SECTOR, code, sector=sector)
    if not ma.ok:
        print(f"报告生成失败：{ma.error}")
        sys.exit(1)
    rc = writer.write(ma)
    vp = viewpoint.build(ma, rc)
    print("提交给 OptionHelper 的需求：")
    print("  " + build_prompt(vp))
    print()
    result = run_full(vp, output_type=output_type)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
