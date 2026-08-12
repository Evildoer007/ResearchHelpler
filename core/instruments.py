"""挂钩标的库：可作为场外衍生品挂钩标的的指数与 ETF（人工维护）。

为什么要有它（发散性的基础设施）：
  模板从"长鑫科技IPO"推导出"挂钩中证500"，这一步是**发散**——
  事件 → 影响机制 → 影响范围 → **候选表达工具** → 多维比较 → 最优挂钩标的。
  其中"候选表达工具"需要一个候选池；没有池子，LLM 只能凭记忆编代码
  （实测已两次踩坑：思源电气写成中国石化的代码、证券ETF 代码给错）。

维护原则（同论点库）：**人工维护、代码经 iFinD 实测校验**，不让模型自行扩充。
标签用于让 planner/writer 从"影响范围"匹配到候选标的（如科技暴露、市值风格）。

全部代码已用 THS_BasicData 校验：返回的官方名见 `官方名` 字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dfield

# 标签词表（供筛选与 LLM 匹配；勿随意新造，保持可控）
# 市值风格
T_LARGE = "大盘"
T_MID = "中盘"
T_SMALL = "小盘"
# 风格属性
T_GROWTH = "成长"
T_VALUE = "价值"
T_BALANCED = "均衡"
T_DIVIDEND = "红利"
# 行业主题
T_TECH = "科技"
T_SEMI = "半导体"
T_AI = "人工智能"
T_FINANCE = "金融"
T_BANK = "银行"
T_CONSUME = "消费"
T_MEDICAL = "医药"
T_NEWENERGY = "新能源"
T_CYCLE = "周期"
T_MILITARY = "军工"
T_ROBOT = "机器人"
T_REALESTATE = "地产"
# 地域
T_OVERSEA = "跨境"


@dataclass
class Instrument:
    代码: str
    简称: str            # 俗称（研报里常用）
    官方名: str          # iFinD 返回的全称（已校验）
    类型: str            # 宽基指数 / 宽基ETF / 行业ETF
    标签: list[str] = dfield(default_factory=list)
    说明: str = ""
    跟踪指数: str = ""   # ETF 的估值取自其跟踪指数（两者估值本质相同）


INSTRUMENTS: list[Instrument] = [
    # ---- 宽基指数（雪球等结构最常挂钩）----
    Instrument("000300.SH", "沪深300", "沪深300", "宽基指数",
               [T_LARGE, T_VALUE, T_BALANCED], "大盘蓝筹代表，金融权重高"),
    Instrument("000905.SH", "中证500", "中证500", "宽基指数",
               [T_MID, T_GROWTH, T_TECH], "中盘成长，泛科技权重高、估值适中、流动性充裕"),
    Instrument("000852.SH", "中证1000", "中证1000", "宽基指数",
               [T_SMALL, T_GROWTH], "小盘成长，弹性大、波动高"),
    Instrument("399006.SZ", "创业板指", "创业板指", "宽基指数",
               [T_GROWTH, T_TECH], "成长科技代表，新能源与医药权重高"),
    Instrument("000688.SH", "科创50", "科创50", "宽基指数",
               [T_TECH, T_SEMI, T_GROWTH], "硬科技，半导体权重最高，弹性最强"),
    Instrument("000016.SH", "上证50", "上证50", "宽基指数",
               [T_LARGE, T_VALUE, T_FINANCE], "超大盘价值，金融与消费为主"),

    # ---- 宽基 ETF ----
    Instrument("510300.SH", "沪深300ETF", "华泰柏瑞沪深300ETF", "宽基ETF",
               [T_LARGE, T_VALUE, T_BALANCED], "沪深300 的 ETF 表达", "000300.SH"),
    Instrument("510500.SH", "中证500ETF", "南方中证500ETF", "宽基ETF",
               [T_MID, T_GROWTH, T_TECH], "中证500 的 ETF 表达", "000905.SH"),
    Instrument("512100.SH", "中证1000ETF", "南方中证1000ETF", "宽基ETF",
               [T_SMALL, T_GROWTH], "中证1000 的 ETF 表达", "000852.SH"),
    Instrument("588000.SH", "科创50ETF", "华夏上证科创板50成份ETF", "宽基ETF",
               [T_TECH, T_SEMI, T_GROWTH], "集中硬科技暴露（模板曾用）", "000688.SH"),
    Instrument("588330.SH", "双创50ETF", "华宝中证科创创业50ETF", "宽基ETF",
               [T_TECH, T_GROWTH, T_BALANCED], "科创板+创业板跨板均衡（模板曾用）"),

    # ---- 宽基（补充）----
    Instrument("000906.SH", "中证800", "中证800", "宽基指数",
               [T_LARGE, T_MID, T_BALANCED], "沪深300+中证500，覆盖大中盘"),
    Instrument("399303.SZ", "国证2000", "国证2000", "宽基指数",
               [T_SMALL, T_GROWTH], "小微盘代表，弹性最大"),
    Instrument("510050.SH", "上证50ETF", "华夏上证50ETF", "宽基ETF",
               [T_LARGE, T_VALUE, T_FINANCE], "超大盘价值，期权品种最活跃", "000016.SH"),
    Instrument("159915.SZ", "创业板ETF", "易方达创业板ETF", "宽基ETF",
               [T_GROWTH, T_TECH], "创业板指的 ETF 表达", "399006.SZ"),
    Instrument("563800.SH", "A500ETF", "广发中证A500ETF", "宽基ETF",
               [T_LARGE, T_BALANCED], "中证A500，行业均衡的新一代核心宽基"),

    # ---- 行业/主题 ETF · 金融 ----
    Instrument("512000.SH", "券商ETF", "华宝中证全指证券公司ETF", "行业ETF",
               [T_FINANCE], "覆盖头部上市券商（模板曾用）"),
    Instrument("512880.SH", "证券ETF", "国泰中证全指证券公司ETF", "行业ETF",
               [T_FINANCE], "券商板块另一主流表达，流动性好"),
    Instrument("512800.SH", "银行ETF", "华宝中证银行ETF", "行业ETF",
               [T_BANK, T_FINANCE, T_VALUE, T_DIVIDEND], "高股息低估值，防御属性"),

    # ---- 科技 ----
    Instrument("512480.SH", "半导体ETF", "国联安中证全指半导体产品与设备ETF", "行业ETF",
               [T_SEMI, T_TECH, T_GROWTH], "半导体产业链集中表达"),
    Instrument("159995.SZ", "芯片ETF", "华夏国证半导体芯片ETF", "行业ETF",
               [T_SEMI, T_TECH, T_GROWTH], "芯片设计制造，弹性高"),
    Instrument("515980.SH", "人工智能ETF", "华富中证人工智能产业ETF", "行业ETF",
               [T_AI, T_TECH, T_GROWTH], "AI 产业链表达"),
    Instrument("159819.SZ", "AI主题ETF", "易方达中证人工智能主题ETF", "行业ETF",
               [T_AI, T_TECH, T_GROWTH], "AI 主题另一表达"),
    Instrument("512720.SH", "计算机ETF", "国泰中证计算机主题ETF", "行业ETF",
               [T_TECH, T_GROWTH], "软件与信息技术"),

    # ---- 消费 / 医药 ----
    Instrument("159928.SZ", "消费ETF", "汇添富中证主要消费ETF", "行业ETF",
               [T_CONSUME, T_VALUE], "主要消费，防御性较强"),
    Instrument("512690.SH", "酒ETF", "鹏华中证酒ETF", "行业ETF",
               [T_CONSUME], "白酒为主，高集中度"),
    Instrument("515170.SH", "食品饮料ETF", "华夏中证细分食品饮料产业主题ETF", "行业ETF",
               [T_CONSUME], "食品饮料细分"),
    Instrument("512010.SH", "医药ETF", "易方达沪深300医药卫生ETF", "行业ETF",
               [T_MEDICAL], "沪深300 医药权重股"),
    Instrument("159992.SZ", "创新药ETF", "银华中证创新药产业ETF", "行业ETF",
               [T_MEDICAL, T_GROWTH], "创新药产业链，弹性大"),
    Instrument("512170.SH", "医疗ETF", "华宝中证医疗ETF", "行业ETF",
               [T_MEDICAL, T_GROWTH], "医疗服务与器械"),

    # ---- 新能源 / 周期 / 军工 ----
    Instrument("515030.SH", "新能源车ETF", "华夏中证新能源汽车ETF", "行业ETF",
               [T_NEWENERGY, T_GROWTH], "新能源汽车产业链"),
    Instrument("515790.SH", "光伏ETF", "华泰柏瑞中证光伏产业ETF", "行业ETF",
               [T_NEWENERGY, T_GROWTH], "光伏产业链，周期成长"),
    Instrument("159755.SZ", "电池ETF", "广发国证新能源车电池ETF", "行业ETF",
               [T_NEWENERGY, T_GROWTH], "锂电产业链"),
    Instrument("512400.SH", "有色ETF", "南方中证申万有色金属ETF", "行业ETF",
               [T_CYCLE], "有色金属，与商品价格联动"),
    Instrument("515220.SH", "煤炭ETF", "国泰中证煤炭ETF", "行业ETF",
               [T_CYCLE, T_VALUE, T_DIVIDEND], "高股息周期"),
    Instrument("515210.SH", "钢铁ETF", "国泰中证钢铁ETF", "行业ETF",
               [T_CYCLE], "钢铁，顺周期"),
    Instrument("159870.SZ", "化工ETF", "鹏华中证细分化工产业主题ETF", "行业ETF",
               [T_CYCLE], "化工产业链"),
    Instrument("516780.SH", "稀土ETF", "华泰柏瑞中证稀土产业ETF", "行业ETF",
               [T_CYCLE, T_GROWTH], "稀土，资源+成长双属性"),
    Instrument("512660.SH", "军工ETF", "国泰中证军工ETF", "行业ETF",
               [T_MILITARY, T_GROWTH], "国防军工，事件驱动特征明显"),

    # ---- 机器人 / 智能制造 ----
    # 同类共 7 只（均已校验代码），按近20日日均成交额取流动性最好的两只入池：
    # 562500 日均 7.41亿、159770 日均 1.82亿，其余 4 只均低于 0.4亿——
    # 挂钩标的最怕的就是流动性不足时做市方不接，故不把长尾那几只列进来当候选。
    Instrument("562500.SH", "机器人ETF", "华夏中证机器人ETF", "行业ETF",
               [T_ROBOT, T_TECH, T_GROWTH], "机器人产业链，同类中流动性最好"),
    Instrument("159770.SZ", "机器人ETF(天弘)", "天弘中证机器人ETF", "行业ETF",
               [T_ROBOT, T_TECH, T_GROWTH], "机器人产业链另一表达"),

    # ---- 红利 / 地产 / 家电 ----
    Instrument("510880.SH", "红利ETF", "华泰上证红利ETF", "行业ETF",
               [T_DIVIDEND, T_VALUE], "高股息，防御与安全垫"),
    Instrument("515180.SH", "中证红利ETF", "易方达中证红利ETF", "行业ETF",
               [T_DIVIDEND, T_VALUE], "红利另一表达，覆盖更广"),
    Instrument("512200.SH", "地产ETF", "南方中证全指房地产ETF", "行业ETF",
               [T_REALESTATE, T_CYCLE], "房地产产业链"),
    Instrument("159996.SZ", "家电ETF", "国泰中证全指家用电器ETF", "行业ETF",
               [T_CONSUME, T_VALUE], "家电，消费+出海"),

    # ---- 跨境（QDII）----
    Instrument("513130.SH", "恒生科技ETF", "华泰柏瑞南方东英恒生科技指数(QDII-ETF)", "跨境ETF",
               [T_OVERSEA, T_TECH, T_GROWTH], "港股科技龙头，与A股科技联动"),
    Instrument("513050.SH", "中概互联ETF", "易方达中证海外中国互联网50(QDII-ETF)", "跨境ETF",
               [T_OVERSEA, T_TECH], "中概互联网龙头"),
    Instrument("159941.SZ", "纳指ETF", "广发纳斯达克100(QDII-ETF)", "跨境ETF",
               [T_OVERSEA, T_TECH, T_GROWTH], "美股科技，海外映射标的"),
    Instrument("513500.SH", "标普500ETF", "博时标普500(QDII-ETF)", "跨境ETF",
               [T_OVERSEA, T_BALANCED], "美股宽基"),
]

_BY_CODE = {i.代码: i for i in INSTRUMENTS}


def get(code: str) -> Instrument | None:
    return _BY_CODE.get(code)


def find(keyword: str) -> list[Instrument]:
    """按简称/官方名模糊查找。"""
    k = (keyword or "").strip()
    if not k:
        return []
    return [i for i in INSTRUMENTS if k in i.简称 or k in i.官方名 or k == i.代码]


def by_tags(tags: list[str], *, match_all: bool = False) -> list[Instrument]:
    """按标签筛候选（发散推理用：影响范围 → 候选表达工具）。"""
    tset = set(tags or [])
    if not tset:
        return list(INSTRUMENTS)
    out = []
    for i in INSTRUMENTS:
        s = set(i.标签)
        if (tset <= s) if match_all else (tset & s):
            out.append(i)
    return out


# 板块 → 该板块的**默认挂钩标的**。人工维护，理由同本模块开头：不让模型自行匹配。
#
# 为什么需要它：报告分析的是**整个板块**，观点包交给 OptionHelper 时，挂钩标的就该是
# 能表达这个板块的可交易工具，而不是数据阶段用的那只代表个股——
# 把板块观点绑到贵州茅台身上，做出来的结构承担的是茅台的个股风险，不是消费板块的风险。
# 数据锚点（代表标的）与可交易标的是两个角色，这里负责后者。
#
# ⚠ ETF 跟踪的指数与我们的板块口径**并不完全一致**（消费ETF 跟踪中证主要消费，
# 而我们的"消费"是六个一级行业合并），这是真实存在的基差。故 `underlying_for`
# 会把这层不一致如实说出来，由定价方决定怎么处理，而不是假装两者等同。
_SECTOR_UNDERLYING: dict[str, str] = {
    # 宽口径大类
    "消费": "159928.SZ", "大消费": "159928.SZ", "必选消费": "159928.SZ",
    # 一级行业
    "食品饮料": "515170.SH", "白酒": "512690.SH", "家用电器": "159996.SZ",
    "医药生物": "512010.SH", "半导体": "159995.SZ", "证券": "512880.SH",
    "银行": "512800.SH", "计算机": "512720.SH", "软件开发": "512720.SH",
    "有色金属": "512400.SH", "煤炭": "515220.SH", "钢铁": "515210.SH",
    "基础化工": "159870.SZ", "房地产": "512200.SH", "国防军工": "512660.SH",
    # 机器人需求最终落到"自动化设备"这个一级行业上（见 #72 的口径解析），
    # 故两个名字都映射到机器人ETF——分析口径是行业，可交易表达是主题ETF，
    # 两者不完全等同，`underlying_for` 会把这层基差说出来。
    "自动化设备": "562500.SH", "机械设备": "562500.SH", "机器人": "562500.SH",
}


def underlying_for(sector: str) -> tuple[Instrument | None, str]:
    """板块 → (默认挂钩标的, 口径说明)。没有对应标的时返回 (None, 原因)。

    宁可返回 None 也不硬凑一个近似标的——挂错标的的代价由定价和对冲承担，
    而"这个板块暂无合适挂钩工具"本身就是有用的结论。
    """
    s = (sector or "").strip()
    code = _SECTOR_UNDERLYING.get(s)
    if not code:
        return None, f"挂钩标的池中暂无对应「{s}」的可交易标的，需人工指定"
    inst = _BY_CODE.get(code)
    if inst is None:
        return None, f"映射到 {code} 但标的池中查无此码（映射表与池不同步）"
    note = f"{inst.官方名}"
    if inst.跟踪指数:
        note += f"，跟踪{inst.跟踪指数}"
    return inst, note


def catalog(for_llm: bool = True) -> list[dict]:
    """候选池清单，喂给 LLM 做择优（只给真实存在的标的，杜绝编造代码）。"""
    return [
        {"代码": i.代码, "简称": i.简称, "类型": i.类型,
         "标签": i.标签, "说明": i.说明}
        for i in INSTRUMENTS
    ]


if __name__ == "__main__":  # python -m core.instruments
    print(f"挂钩标的库共 {len(INSTRUMENTS)} 个")
    for t in ("宽基指数", "宽基ETF", "行业ETF"):
        items = [i for i in INSTRUMENTS if i.类型 == t]
        print(f"  [{t}] {len(items)}个: {', '.join(i.简称 for i in items)}")
    print("\n按标签筛选示例：")
    print("  科技+成长 →", [i.简称 for i in by_tags([T_TECH, T_GROWTH])])
    print("  金融     →", [i.简称 for i in by_tags([T_FINANCE])])
