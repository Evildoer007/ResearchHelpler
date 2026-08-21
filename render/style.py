"""图表与一页通视觉风格（经 dataviz 方法论校验）。

配色与 OptionHelper Designer 的 design_tokens.py 对齐：品牌红、蓝灰、风险金和纸白。
类别色板固定、不循环；图表与 HTML 报告共用同一套视觉语义。

本地化要点：上涨使用品牌红；下跌使用蓝灰，以保持金融语义与统一色系。
"""

from __future__ import annotations

import matplotlib as mpl

# ---- OptionHelper Designer tokens ----
# 与 option-helper/scripts/modules/designer/design_tokens.py 一一对应；
# 本地 matplotlib 无法消费 CSS 变量，故在此保留同值映射。
PRIMARY = "#C8102E"       # brand_red：标题、核心数字、主标记
PRIMARY_D = "#890D26"     # brand_red_deep：强调/深色轮廓
PRIMARY_L = "#E5C5CC"     # red_border_soft：次级元素、浅填充
INK = "#241D20"           # ink：正文
MUTED = "#6E5F63"         # muted：次要文字/坐标
GRID = "#E9DADC"          # rule：网格、分隔线
SURFACE = "#FFFDFB"       # paper：图表画布与报告纸面
CARD = "#FBF1F3"          # brand_red_soft：浅色卡片
BLUE_GRAY = "#49647D"     # blue_gray：中性对比/负向序列
RISK_GOLD = "#855E22"     # risk_gold：风险提示序列
CHART_GRAY = "#7E8A99"    # chart_gray：低优先级序列

# ---- 涨跌语义（红涨蓝灰跌）----
# 涨用主红，跌用 Designer 的蓝灰；两者明度与色相均有足够差异，
# 色觉障碍读者亦可通过深浅与图表标签区分。
UP = PRIMARY            # 涨 / 正（品牌红）
DOWN = BLUE_GRAY        # 跌 / 负（蓝灰；与 Designer 中性色语义一致）
FLAT = MUTED

# ---- 类别色板：Designer 固定顺序 ----
# 红、蓝灰、风险金、图表灰与深红均来自同一份 Designer token；
# 图表一律直接标数值与标签，不依赖"看颜色猜是哪条"。
SERIES = [PRIMARY, BLUE_GRAY, RISK_GOLD, CHART_GRAY, PRIMARY_D]

# ---- 字体：优先可商用 ----
# 成品要发给客户，字体授权是实打实的合规问题：
#   微软雅黑  方正授权给微软，**商用需另行授权** → 不用
#   黑体/宋体 中易·方正体系，商用同样不干净       → 仅作最后兜底
#   等线      微软自有委托设计，相对清白但仍是捆绑字体 → 次选兜底
# 真正可商用的（SIL OFL / 官方免费商用）是下面前四个，装了就自动生效、
# 不用改代码；一个都没装时回落到等线，**不会**回落到微软雅黑。
# matplotlib 与 CSS 用同一份优先级，避免图里与正文字体不一致。
FONT_STACK = [
    "Source Han Sans SC",   # 思源黑体（Adobe/Google，SIL OFL）
    "Noto Sans CJK SC",     # 同一字体的 Google 命名
    "Source Han Sans CN",
    "HarmonyOS Sans SC",    # 华为，官方免费商用
    "Alibaba PuHuiTi",      # 阿里巴巴普惠体，官方免费商用
    "DengXian",             # 等线：本机兜底
    "SimSun",               # 最后兜底，保证不出豆腐块
]
FONT = FONT_STACK[0]


def apply_style() -> None:
    """套用全局 matplotlib 风格（含中文字体）。绘图前调用一次。

    matplotlib 会按顺序取**第一个本机可用**的字体，列了没装的也不报错，
    所以把可商用字体排在前面是零成本的——装上即生效。
    """
    mpl.rcParams.update({
        "font.sans-serif": list(FONT_STACK),
        "font.size": 8.5,
        "axes.unicode_minus": False,           # 负号正常显示
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "text.color": INK,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def signed_color(v: float) -> str:
    """按红涨绿跌返回颜色。"""
    return UP if v >= 0 else DOWN


def series_color(i: int) -> str:
    """取第 i 个类别色（超出则回退中性灰，绝不循环生成新色）。"""
    return SERIES[i] if 0 <= i < len(SERIES) else MUTED
