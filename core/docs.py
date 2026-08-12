"""文档抽取：从 `sources/` 的研报 PDF 里提炼**可核查的观点候选**。

## 为什么这个模块通篇在做"校验"

论点库那 58 条走的是"阈值判定"——机器自己算真假。文档观点没有阈值可判，
它的可信度只能来自**出处可查**：分析师点开原文看一眼，确认研报真这么说了。

于是最大的风险变成：**AI 编一条看起来很像研报说过的话**。这比 DESIGN #35 那次
（把鸡蛋价格当成利率）更危险——那次至少数字来自一个真实序列，
而编造的引用是数字和出处双重造假，validator 抓不到，人也难发现。

所以本模块的铁律：**AI 只做抽取，不做生成**。每条观点必须附一段**逐字原文**，
且观点里出现的每个数字都必须字面出现在那段原文里。这是**机械校验**，
不依赖模型自觉——校验不过的条目直接标记，不进候选清单。

## 与论点库的关系

文档观点按论点库的九大类归位，但**不进 `_JUDGES`**——它们不是阈值触发的，
是人工确认的。论点库在这里的角色是**分类框架**，告诉 AI 该找哪些类型的论证，
让抽取有结构而不是自由发挥。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field as dfield
from pathlib import Path

from llm.client import DeepSeekClient

SOURCES_DIR = Path(__file__).resolve().parent.parent / "sources"

# 单次喂给 LLM 的正文上限。实测两篇券商研报全文 17k~20k 字符，
# 取 30000 可整篇喂入——此前设 15000 会把靠后的页整段丢掉，
# 而**盈利预测这类关键表格恰恰常在报告后半部**，抽不到表就等于丢掉研报一半价值。
_MAX_CHARS = 30000


@dataclass
class DocClaim:
    """一条从文档抽取的观点候选。"""

    id: str = ""            # doc_1/doc_2…，由 pipeline 在合成候选清单时分配
    图表: dict | None = None   # 可选的配图数列（见 _verify_chart）
    观点: str = ""
    方向: str = ""          # 看涨 / 看跌 / 中性
    类别: str = ""          # 论点库九大类之一
    原文: str = ""          # 逐字片段（叙述正文），供分析师核对
    # 表格原文：模型另行逐字引出的表格行。存在的理由是——研报里成体系的数列几乎
    # 都在财务摘要表/可比公司表里，而模型引 `原文` 时引的是叙述段落，两者不重合。
    # 若只拿 `原文` 当比对锚，表格数字永远校验不过（实测拦掉全部表格类观点）；
    # 若放宽成拿全文当锚，20 页里随便撞上一个同值数字就算过，等于没校验。
    # 故要求它把表格行**原样**引出来，这段自身同样要能在文档里找到，
    # 机械链条"数字→所引表格行→文档"才不断。
    表格原文: str = ""
    页码: int = 0
    数值: list[str] = dfield(default_factory=list)
    来源: str = ""          # 机构 + 标题 + 日期（取自文件名）
    文档日期: str = ""      # YYYY-MM-DD，取自文件名
    时效: str = ""          # 如 "4天前" / "⚠ 已过 412 天"
    校验: str = ""          # ok / 原文未匹配 / 数值未出现:xxx
    ok: bool = False


@dataclass
class DocExtract:
    文件: str = ""
    来源: str = ""
    文档日期: str = ""
    时效: str = ""
    页数: int = 0
    claims: list[DocClaim] = dfield(default_factory=list)
    tokens: int = 0
    ok: bool = False
    error: str = ""
    图表错误: str = ""      # 数列调用本身失败（不影响已抽出的观点）
    图表拦截: int = 0       # 数列抽出来了但没通过校验，被丢弃的条数
    备选数列: list = dfield(default_factory=list)   # 已通过校验但观点都配满了的多余数列
    拦截原因: Counter = dfield(default_factory=Counter)   # 调参用：拦在哪道闸上
    滤除提问: int = 0       # 被机械规则判为提问句而丢弃的条数

    @property
    def 通过(self) -> list[DocClaim]:
        return [c for c in self.claims if c.ok]

    @property
    def 未通过(self) -> list[DocClaim]:
        return [c for c in self.claims if not c.ok]


# ---------------- PDF 读取 ----------------

def read_pages(path: Path) -> list[str]:
    """逐页抽取文本。返回 [第1页文本, 第2页文本, ...]。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # 老环境回退
        from PyPDF2 import PdfReader  # type: ignore

    r = PdfReader(str(path))
    return [(p.extract_text() or "") for p in r.pages]


def _norm(s: str) -> str:
    """归一化后再比对。

    PDF 抽出来的文本空格极不规则（"营业利润 60.5 万亿韩元， 同环比+557/61%"），
    逐字比对必然失败；数字又常带千分位逗号。故比对前一律去掉空白与逗号。
    """
    return re.sub(r"[\s,，]", "", s)


# 财报期/年份标识：形如 FY2026Q2、26Q2、2026E、28E、H1、7/29。
# 这些是**时间标签不是数据**，必须先剥掉再抽数字——否则 "26Q2" 里的 26、
# "28E营收" 里的 28 会被当成待核对的数值，而原文写的是连在一起的 "26Q2"，
# 数值比对自然对不上，把本来合格的观点误拦下来（实测一次拦掉 6 条，占 35%）。
_PERIOD = re.compile(
    r"FY\d{2,4}(?:Q[1-4]|H[12])?"      # FY2026Q2 / FY26H1
    r"|\d{2,4}[-~—]\d{2,4}E"           # 26-28E / 2026~2028E（区间写法，须排在单个之前）
    r"|\d{2,4}(?:Q[1-4]|H[12])"        # 26Q2 / 2026H1
    r"|\d{2,4}E"                       # 2026E / 28E
    r"|\d{1,2}/\d{1,2}",               # 7/29
    re.I,
)


def _numbers(s: str) -> list[str]:
    """抽出文本里**作为数据的**数字（含小数）。用于校验观点里的数字是否真在原文里。

    ⚠ **不能复用 `_norm`**：它为了子串匹配把所有空白都去掉，而表格行里的数字正是
    靠空格分隔的——`营业总收入 44,622 32,766 66,193` 去掉空白后会粘成
    `446223276666193` 这一个巨型数字，原本的每个数都再也匹配不上。
    散文里数字之间隔着汉字，所以这个坑只在表格行上暴露，一度让全部表格类数列
    100% 被拦（表格原文明明逐字对得上文档，却报"数值未出现"）。
    这里只去掉**数字之间**的千分位逗号，保留空白作为分隔。
    """
    t = re.sub(r"(?<=\d)[,，](?=\d)", "", s)
    return re.findall(r"\d+(?:\.\d+)?", _PERIOD.sub(" ", t))


# 业绩说明会纪要里大量是"Q：…？"的提问，而抽取器只看"这是不是研报里的一句话"，
# 会把**提问**当成论断抽出来（实测："…主要原因是什么？"、
# "…市场也担忧未来可能出现供过于求的情况，公司对此有何看法？"）。
# 提问不是观点：它陈述的是"有人问了什么"，不是"研报判断什么"，拿它做论据是空的。
# 用机械规则滤掉——比在提示词里叮嘱可靠，且零成本。
_Q_HEAD = re.compile(r"^\s*[QＱ]\s*[：:]")


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_Q_HEAD.match(t)) or t.endswith(("？", "?"))


def _rows_in_doc(rows: str, full: str) -> bool:
    """所引表格行是否**逐行**都能在文档里找到。

    ⚠ 必须逐行查，不能把整段拼起来当一个子串查。因为提示词要求的是
    "抄下表头行 + 你取数的那几行"——这些行在表格里天然**不相邻**：

        会计年度 (韩元)   2025    2026E   2027E
        营业收入 (十亿)   97,147  332,970 474,132
        +/-%             46.76   242.75  42.39
        归属母公司净利润   42,919  209,231 299,627   ← 引的是这行

    引 [表头行, 归属母公司净利润行] 时，两行中间隔着"营业收入""+/-%"，
    拼接后的整段作为连续子串永远查不到。实测 27 组数列因此拦掉 22 组，
    而它们其实每一行都是真的——指令要"挑着抄"、校验却要"连续"，两者自相矛盾。
    grounding 要保证的是"每一行都真实存在"，不是"这些行彼此相邻"。
    """
    lines = [_norm(x) for x in rows.split("\n")]
    lines = [x for x in lines if x]
    return bool(lines) and all(x in full for x in lines)


def _join_rows(v) -> str:
    """表格原文允许模型给字符串或字符串数组（一行一个元素），统一成一段文本。"""
    if isinstance(v, list):
        return "\n".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def parse_filename(path: Path) -> tuple[str, str]:
    """从文件名提炼 (来源标签, 文档日期)。

    形如 `YYYYMMDD-机构-标题` 或 `YYYYMMDD_机构_..._标题`——
    不同平台导出的命名习惯不同（横杠/下划线都见过），两种分隔符都认。

    取不到日期时返回空串——不猜、也不用文件修改时间顶替，
    因为"这篇研报什么时候写的"和"这个文件什么时候拷进来的"是两回事。
    """
    stem = path.stem
    m = re.match(r"^(\d{8})[-_](.+)$", stem)
    if not m:
        return stem[:60], ""

    date, rest = m.group(1), m.group(2)
    d = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    seg = re.split(r"[-_]", rest)
    org = seg[0] if seg else ""
    # 标题取最后一段：下划线命名里中间常夹着分类和作者（量化投资_蒋可欣_…），
    # 真正的标题在末尾；横杠命名里剩下的部分本身就是标题。
    title = seg[-1] if len(seg) > 1 else rest
    return f"{org}《{title[:40]}》{d}", d


def source_label(path: Path) -> str:
    return parse_filename(path)[0]


# 超过这个天数就在候选清单上标警示。研报是事件驱动的快照，
# 季度业绩点评、目标价这类内容过一个季度基本作废，故阈值取一个季度。
STALE_DAYS = 90


def freshness(doc_date: str) -> str:
    """把文档日期换算成人能一眼看懂的时效标签。"""
    if not doc_date:
        return "日期未知（文件名不含 YYYYMMDD）"
    import datetime as dt

    try:
        d = dt.date.fromisoformat(doc_date)
    except ValueError:
        return "日期未知"
    days = (dt.date.today() - d).days
    if days < 0:
        return f"{-days}天后（日期在未来，请核对）"
    if days > STALE_DAYS:
        return f"⚠ 已过 {days} 天，内容可能失效"
    return f"{days}天前" if days else "今天"


# ---------------- LLM 抽取 ----------------

_SYSTEM = """你是研报的"观点抽取器"。从给定的研报正文中，提炼出可用于投资策略报告的观点。

铁律（违反即作废）：
1. **只能抽取，不得生成**。观点必须是研报正文里明确表达的，不得引入正文之外的任何信息、
   不得推理演绎、不得补充你自己知道的行业知识。
2. 每条观点必须附 `原文` —— 从正文里**逐字复制**的一段连续文字（30~120 字），
   不得改写、不得拼接不相邻的句子、不得省略中间内容。
2.1 **数字若来自表格，另填 `表格原文`**，不要硬塞进 `原文`。
   `原文` 引叙述正文，`表格原文` 逐字引你读的那几行表格（含表头行与数据行，
   可给字符串数组，一行一个元素）。两者都会被逐字回查文档，改写一个字就整条作废。
   之所以分成两个字段：表格数字和叙述文字在研报里根本不相邻，
   硬要一段连续文字同时覆盖两者是做不到的。
3. 观点表述里出现的**每一个数字**，都必须在 `原文` 或 `表格原文` 里字面出现。
   做不到就不要在观点里写这个数字。
   3.1 **不得做单位换算**。原文写"547,022 十亿韩元"就照写，不要折成"547万亿"——
       换算后的数字在原文里找不到，会被判为杜撰。宁可照抄原始量纲。
4. **正反两面都要抽**。研报通常偏乐观，但也会有风险提示、不及预期、下调目标价等内容，
   这些同样重要，必须一并抽出。只抽正面观点是不合格的。
5. `页码` 填该段原文所在的句所在页（正文里用 ===第N页=== 标注）。
6. **只抽与"本次主题"相关的观点**（主题在输入里给出；未给出时不设此限）。
   6.1 相关 = 论及该主题所涉的行业、产业链环节、上下游、竞争格局、需求或政策。
       同属一份文档**不算**相关——每日通讯、晨会纪要这类汇编里往往塞着几十家
       不相干公司的业绩预告，把它们抽出来只会污染候选清单。
       例（主题=芯片/半导体）：某乳业公司中期扭亏 → **不相关，不要抽**；
       某光伏玻璃企业亏损扩大 → **不相关，不要抽**。
   6.2 拿不准就不抽。漏抽一条无妨，抽进一条不相干的会让分析师白读一遍。
7. **只抽论断，不抽提问**。业绩说明会纪要里大量是"Q：……？"的问句，
   它陈述的是"有人问了什么"，不是"研报判断什么"，拿它做论据是空的。
   以"？"结尾或以"Q："开头的段落一律不作为 `原文`；
   若该问题**下方的回答**有实质结论，抽那段回答。
只输出一个 JSON 对象，不要多余文字。"""


# 配图数列单独一次调用。
# **为什么不和观点抽取合并**：合并过三版，每版都加强"务必抽表格"的措辞，
# 结果全部为零——模型在同一次调用里同时承担"抽观点"和"抽表格"时，稳定地忽略后者。
# 排查中一度怀疑 PDF 抽取把表格结构打乱了，实测证伪：抽出的文本形如
# "营业收入 (十亿) 97,147 332,970 474,132 547,022"，干净且完全可引。
# 所以问题不在数据、也不在措辞强度，而在**一次调用里塞了两个任务**。拆开即可。
_SYSTEM_CHART = """你是研报的"表格数列抽取器"。任务只有一件事：
把研报正文里表格中的数字，抽成可以直接作图的数列。

铁律（违反即作废）：
1. **只能抄，不得算、不得推断**。数字必须原样出现在正文里。
   不得做单位换算——正文写"547,022 十亿韩元"就照写 547022，不要折成 547 万亿。
2. 每组数列必须附 `表格原文`：把该表的**表头行**和你取数的**每一行**逐字抄下来
   （字符串数组，一行一个元素）。**每一行都会被单独逐字回查文档**，改一个字整条作废。
   抄漏哪个数，哪个数就会被丢掉。
   2.1 这几行**不必在表格里彼此相邻**——表头行与你要的数据行中间隔着别的行是正常的，
       照原样各抄各的即可，不要为了"看起来连续"而删改、合并或补齐中间内容。
3. `标签` 与 `值` 长度必须相等；`值` 只填数字，单位写进 `单位`。
4. 类型：`line` 用于按时间排列的序列；`bar` 用于同类可比的一组；
   `bar_line` 用于两组量纲不同、需要对比的数（如"前值 vs 新值"、"预期 vs 实际"），
   此时 `值` 与 `值2` 都要填且等长。
5. `归属观点` 填该数列**能直接充当论据**的那条观点的序号。判断标准是严格的：
   把这张图放在那条观点下面，读者能一眼看出它在支撑该观点。
   5.1 只是"同属一家公司""都跟业绩有关"**不算**能支撑。
       例："会长增持致股价大涨 30%"配 DRAM 营收序列——两者无关，应填 0。
       例："NAND 营收超预期"配 NAND 营收序列——直接支撑，填该序号。
   5.2 **拿不准就填 0，不要硬凑。** 填 0 的数列一样会被采纳、留作备选，
       不会浪费；而硬挂到不相关的观点上，会产出一张文不对题的图，比没有图更糟。
6. **把正文里能找到的表格逐一抽出来，不要只挑一两张。** 一份券商研报通常有
   十几张表（财务预测表、可比公司估值表、盈利预测调整表、分季度数据表等），
   它们各自支撑不同的论点，只抽一张等于丢掉其余全部。
   6.1 表格通常长这样：一行"图表N：xxx"标题，其后若干行"项目 + 一串数字"，
       例如 `营业总收入 335,371 332,970 -0.72%`、`毛利率 82.71% 82.05%`。
       **看到这类结构就抽一组。**
   6.2 同一张表里若有多行值得作图（如营收行与毛利率行），可分别抽成多组数列。
   6.3 **只抽你能逐字抄下原文的表**。抄不全、记不准、需要自己拼接或补齐的，直接跳过——
       宁可少抽几张，也不要交出一组抄不准的 `表格原文`：它会被逐字回查拦下，
       整组作废，等于白做，还挤占了本可以抽准的表。
只输出一个 JSON 对象，不要多余文字。"""


def _build_prompt(text: str, categories: list[str], topic: str = "") -> str:
    import json

    spec = {
        "任务": "从下面的研报正文中抽取观点，每条附逐字原文",
        **({"本次主题_只抽与之相关的观点": topic} if topic else {}),
        "可用类别": categories,
        "研报正文": text,
        "输出格式": {
            "观点列表": [
                {
                    "观点": "一句话论断，不超过40字",
                    "方向": "看涨|看跌|中性",
                    "类别": "从可用类别中选一个最贴切的",
                    "原文": "从叙述正文逐字复制的连续片段，30~120字",
                    "表格原文": ["数字取自表格时必填：逐字抄下表头行",
                                 "以及你取数的每一行，一行一个元素"],
                    "页码": "数字",
                    "数值": ["观点或原文里的关键数字，如 60.5、557"],
                }
            ]
        },
    }
    return json.dumps(spec, ensure_ascii=False, indent=1)


def _build_chart_prompt(text: str, claims: list[DocClaim]) -> str:
    """第二次调用：只抽表格数列，并指认它支撑哪一条已抽出的观点。"""
    import json

    spec = {
        "任务": "从下面的研报正文中，把表格里的数字抽成可作图的数列",
        "已抽出的观点": [{"序号": i, "观点": c.观点}
                         for i, c in enumerate(claims, 1)],
        "研报正文": text,
        "输出格式": {
            "数列列表": [
                {
                    "归属观点": "已抽出的观点的序号；对不上任何一条填 0",
                    "标题": "字符串",
                    "类型": "line|bar|bar_line",
                    "标签": ["2026E", "2027E", "2028E"],
                    "值": [332970, 474132, 547022],
                    "值2": ["仅 bar_line 需要，与值等长；否则省略"],
                    "值名": "如 新值",
                    "值2名": "如 前值",
                    "单位": "如 十亿韩元 / %",
                    "表格原文": ["逐字抄下表头行",
                                 "以及取数的每一行，一行一个元素"],
                }
            ]
        },
    }
    return json.dumps(spec, ensure_ascii=False, indent=1)


def _verify_chart(chart, snip_nums: set[float]) -> tuple[dict | None, str]:
    """校验配图数列。返回 (合格的图表规格 或 None, 说明)。

    铁律与观点一致：**每个要画出来的数，都必须出现在所附原文里**。
    图比文字更危险——读者不会去核对柱子的高度，一个编造的数列看起来和真的一模一样。
    故校验不过一律丢弃图表（保留观点本身），绝不"姑且画上"。
    """
    if not isinstance(chart, dict):
        return None, ""
    labels = [str(x).strip() for x in (chart.get("标签") or [])]
    try:
        vals = [float(str(x).replace(",", "")) for x in (chart.get("值") or [])]
    except (TypeError, ValueError):
        return None, "图表已丢弃：值含非数字"
    if not labels or len(labels) != len(vals):
        return None, "图表已丢弃：标签与值长度不一致"

    vals2 = None
    raw2 = chart.get("值2")
    if isinstance(raw2, list) and raw2 and not isinstance(raw2[0], str):
        try:
            vals2 = [float(str(x).replace(",", "")) for x in raw2]
        except (TypeError, ValueError):
            vals2 = None
        if vals2 is not None and len(vals2) != len(vals):
            vals2 = None

    missing = [v for v in vals + (vals2 or []) if v not in snip_nums]
    if missing:
        return None, f"图表已丢弃：{len(missing)} 个数未出现于所引原文/表格"

    typ = str(chart.get("类型") or "").strip()
    if typ not in ("line", "bar", "bar_line"):
        typ = "bar_line" if vals2 else "bar"
    if typ == "bar_line" and vals2 is None:
        typ = "bar"

    pts = []
    for i, lab in enumerate(labels):
        p = {"标签": lab, "值": vals[i]}
        if vals2 is not None:
            p["值2"] = vals2[i]
        pts.append(p)
    unit = str(chart.get("单位") or "").strip()
    return {
        "类型": typ,
        "标题": (str(chart.get("标题") or "").strip() + (f"（{unit}）" if unit else "")),
        "数据点": pts,
        "柱标签": str(chart.get("值名") or "").strip(),
        "线标签": str(chart.get("值2名") or "").strip(),
    }, ""


def _verify(c: DocClaim, pages: list[str]) -> DocClaim:
    """机械校验：原文必须真在文档里；观点里的数字必须真在原文里。"""
    full = _norm("".join(pages))
    snip = _norm(c.原文)

    if not snip:
        c.校验, c.ok = "原文为空", False
        return c
    if snip not in full:
        c.校验, c.ok = "原文未匹配（疑似改写或杜撰）", False
        return c

    # 表格原文与 原文 同等待遇：也必须逐字在文档里，找不到就整条丢掉，
    # 不能只丢表格——否则模型可以靠编一段表格把数字"洗白"。
    evidence = c.原文
    if c.表格原文:
        if not _rows_in_doc(c.表格原文, full):
            c.校验, c.ok = "表格原文未匹配（疑似改写或杜撰）", False
            return c
        evidence += "\n" + c.表格原文

    # 观点里出现的数字，必须出现在所引证据里——**按数值比对，不按字符串**。
    # 实测踩过：研报写"27E 7x PE"，LLM 在观点里写成"7.0x"，字符串比对会误判为杜撰。
    # 数值比对既能放过这种无害的写法差异，又照样能抓住 7x→17x 这类致命漂移。
    # 只校验 2 位以上的数字——个位数（如"3家客户"）误报率高且信息量低。
    snip_nums = {float(n) for n in _numbers(evidence)}
    missing = [n for n in _numbers(c.观点)
               if len(n) >= 2 and float(n) not in snip_nums]
    if missing:
        c.校验, c.ok = f"数值未出现于所引原文/表格: {'、'.join(missing)}", False
        return c

    # 配图数列：与观点同一套校验，不合格只丢图不丢观点
    chart_note = ""
    if c.图表:
        c.图表, chart_note = _verify_chart(c.图表, snip_nums)

    # 页码核对：对不上不算失败（LLM 常把跨页内容归到相邻页），但记下来供人工留意
    p = c.页码
    if 1 <= p <= len(pages) and snip in _norm(pages[p - 1]):
        c.校验 = "ok"
    else:
        c.校验 = "ok（页码存疑，原文在文档中但不在所标页）"
    if chart_note:
        c.校验 += f"；{chart_note}"
    c.ok = True
    return c


def extract(path: Path, client: DeepSeekClient | None = None,
            topic: str = "") -> DocExtract:
    """抽取单篇研报的观点候选。

    topic：本次报告主题（含涉及板块）。给出时只抽与之相关的观点——
    每日通讯、晨会纪要这类汇编里塞着几十家不相干公司的业绩预告，
    不过滤会让候选清单混入大量噪声，全靠分析师人工排除。
    """
    label, doc_date = parse_filename(path)
    out = DocExtract(文件=path.name, 来源=label, 文档日期=doc_date,
                     时效=freshness(doc_date))
    pages = read_pages(path)
    out.页数 = len(pages)
    if not any(p.strip() for p in pages):
        out.error = "PDF 无可抽取文本（可能是扫描件，需 OCR）"
        return out

    # 超额时**跳过该页继续**，不是 break——原先一遇到放不下的页就停，
    # 后面所有页（含关键表格）全丢。页码有 ===第N页=== 标注，跳页不会让 LLM 错乱。
    marked, total = [], 0
    for i, p in enumerate(pages, 1):
        seg = f"\n===第{i}页===\n{p}"
        if total + len(seg) > _MAX_CHARS:
            continue
        marked.append(seg)
        total += len(seg)
    text = "".join(marked)

    from . import thesis as th
    categories = sorted({t.类别 for t in th.load_library().values()})

    client = client or DeepSeekClient()
    if not client.available():
        out.error = "未配置 DeepSeek key"
        return out

    prompt = _build_prompt(text, categories, topic)
    res = client.chat_json(_SYSTEM, prompt, temperature=0.2)
    out.tokens = client.total_tokens
    if not res.ok or not isinstance(res.data, dict):
        out.error = res.error or "LLM 返回非预期结构"
        return out

    # 合法 JSON 但一条观点都没有 —— 实测遇到过（同一篇单跑出 10 条、批量跑出 0 条），
    # 属接口偶发。此前没区分这种情况，会静默产出"抽取 0 条"，
    # 看起来像"这篇研报没内容"，而不是"这次调用失败了"。重试一次再判。
    if not res.data.get("观点列表"):
        res = client.chat_json(_SYSTEM, prompt, temperature=0.2)
        out.tokens = client.total_tokens
        if not res.ok or not isinstance(res.data, dict) or not res.data.get("观点列表"):
            out.error = "LLM 两次均未返回观点（接口偶发或该文档确无可抽内容）"
            return out

    for it in res.data.get("观点列表", []):
        if not isinstance(it, dict):
            continue
        # 提问句机械兜底。铁律 7 已在提示词里禁了，但提示词是**倾向**不是保证——
        # 这类规则判得准、代价为零，就不该只靠模型自觉（与 gauge 量纲那处同理）。
        if _is_question(it.get("原文")) or _is_question(it.get("观点")):
            out.滤除提问 += 1
            continue
        try:
            page = int(str(it.get("页码", 0)).strip() or 0)
        except ValueError:
            page = 0
        c = DocClaim(
            观点=str(it.get("观点", "")).strip(),
            方向=str(it.get("方向", "")).strip(),
            类别=str(it.get("类别", "")).strip(),
            原文=str(it.get("原文", "")).strip(),
            表格原文=_join_rows(it.get("表格原文")),
            页码=page,
            数值=[str(x).strip() for x in (it.get("数值") or [])],
            来源=out.来源,
            文档日期=out.文档日期,
            时效=out.时效,
        )
        out.claims.append(_verify(c, pages))

    _attach_charts(out, text, pages, client)
    out.ok = True
    return out


def _attach_charts(out: DocExtract, text: str, pages: list[str],
                   client: DeepSeekClient) -> None:
    """第二次调用：抽表格数列并挂到对应观点上。失败不影响已抽出的观点。"""
    ok_claims = [c for c in out.claims if c.ok]
    if not ok_claims:
        return
    res = client.chat_json(_SYSTEM_CHART,
                           _build_chart_prompt(text, ok_claims), temperature=0.2)
    out.tokens = client.total_tokens
    if not res.ok or not isinstance(res.data, dict):
        out.图表错误 = res.error or "数列调用返回非预期结构"
        return

    full = _norm("".join(pages))
    待挂: list[tuple[int, dict, str]] = []      # (归属序号, 合格图表规格, 表格原文)
    for it in res.data.get("数列列表", []):
        if not isinstance(it, dict):
            continue
        # 图表数列必须用**它自己的**表格原文校验，不能沿用观点的原文——
        # 两次调用引的是文档里不同的位置，混用等于没校验。
        rows = _join_rows(it.get("表格原文"))
        if not rows or not _rows_in_doc(rows, full):
            out.图表拦截 += 1
            out.拦截原因["表格原文对不上文档"] += 1
            continue
        spec, note = _verify_chart(it, {float(n) for n in _numbers(rows)})
        if spec is None:
            out.图表拦截 += 1
            out.拦截原因[note or "未知"] += 1
            continue
        try:
            idx = int(str(it.get("归属观点", 0)).strip() or 0)
        except ValueError:
            idx = 0
        待挂.append((idx if 1 <= idx <= len(ok_claims) else 0, spec, rows))

    # **只按模型给出的明确归属挂图，不做顺延填充。**
    # 曾经把归属不明的数列顺延给"下一条还没图的观点"，配图数从 1 张涨到 9 张，
    # 但其中 4 张明显文不对题（"会长增持致股价大涨 30%"配了 DRAM 营收序列）。
    # 一张配错的图比没有图更糟——它看着权威，读者却无法把它与论证对上，
    # 反而损害整份报告的可信度，与"每个数字可溯源"这条主线冲突。
    # 归属不明的不丢弃（数列本身已通过逐字校验，是真数据），存入备选供人工取用。
    for idx, spec, rows in 待挂:
        c = ok_claims[idx - 1] if idx else None
        if c is None or c.图表:         # 无归属，或该观点已有图
            out.备选数列.append(spec)
            continue
        c.图表 = spec
        c.表格原文 = (c.表格原文 + "\n" + rows).strip() if c.表格原文 else rows


def extract_all(directory: Path | None = None,
                client: DeepSeekClient | None = None,
                topic: str = "") -> list[DocExtract]:
    """抽取 sources/ 下全部 PDF（含子目录）。"""
    d = directory or SOURCES_DIR
    client = client or DeepSeekClient()
    return [extract(p, client, topic) for p in sorted(d.rglob("*.pdf"))]


def render(results: list[DocExtract]) -> str:
    """把抽取结果排成可读清单。"""
    lines: list[str] = []
    for r in results:
        lines.append("=" * 72)
        lines.append(f"{r.来源}   （{r.页数}页 · {r.时效}）")
        if not r.ok:
            lines.append(f"  ✗ {r.error}")
            continue
        n_chart = sum(1 for c in r.通过 if c.图表)
        extra = f"，配图 {n_chart} 张"
        if r.备选数列:
            extra += f"（另有 {len(r.备选数列)} 组备选）"
        if r.图表拦截:
            why = "、".join(f"{k}×{v}" for k, v in r.拦截原因.most_common())
            extra += f"（{r.图表拦截} 组数列未过校验：{why}）"
        if r.图表错误:
            extra += f"（数列调用失败：{r.图表错误}）"
        lines.append(f"  抽取 {len(r.claims)} 条，校验通过 {len(r.通过)} 条{extra}")
        for c in r.通过:
            lines.append(f"\n  ● [{c.类别}] {c.观点}　（{c.方向}）")
            lines.append(f"      {c.来源} p{c.页码} · {c.时效}")
            lines.append(f"      原文：{c.原文[:110]}")
            if c.表格原文:
                lines.append(f"      表格：{c.表格原文[:160]}")
            if c.图表:
                n = len(c.图表.get("数据点") or [])
                lines.append(f"      📊 可配图：{c.图表.get('类型')} · "
                             f"{c.图表.get('标题')} · {n} 个数据点")
            if c.校验 != "ok":
                lines.append(f"      ⓘ {c.校验}")
        for c in r.未通过:
            lines.append(f"\n  ✗ {c.观点[:50]}")
            lines.append(f"      校验不通过：{c.校验}")
    return "\n".join(lines)


if __name__ == "__main__":  # python -m core.docs
    rs = extract_all()
    print(render(rs))
    tot = sum(len(r.claims) for r in rs)
    ok = sum(len(r.通过) for r in rs)
    print(f"\n合计抽取 {tot} 条，校验通过 {ok} 条，"
          f"拦截 {tot - ok} 条（{'0%' if not tot else f'{(tot-ok)/tot*100:.0f}%'}）")
