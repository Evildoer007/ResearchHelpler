"""图表与一页通视觉风格。

图表与 HTML 报告使用同一份浅色酒红—浅金色板。红、蓝、绿各有相同的
明度阶梯：红用于核心结论，蓝用于对照/负向，绿用于验证/改善；浅色只作
面积、置信区间和卡片底色，不靠颜色替代数字或标签。
"""

from __future__ import annotations

import matplotlib as mpl

# ---- 报告视觉 token（matplotlib 无法消费 CSS 变量，故在此保留同值映射） ----
# 红/蓝/绿三列采用相同的明度阶梯；数值与直接标签始终是主识别方式。
PRIMARY_D = "#7D0A0A"     # 深酒红：主标题、风险重点、关键标记
PRIMARY = "#BF3131"       # 砖红：默认核心数据线
PRIMARY_L = "#D96B6B"     # 浅红：次级红色序列
PRIMARY_FILL = "#F0D1D1"  # 浅红底：面积、区间、卡片

BLUE_D = "#0A377D"        # 深蓝：强对照
BLUE = "#316FBF"          # 标准蓝：基准、负向或第二系列
BLUE_L = "#6B9FD9"        # 浅蓝：次级对照
BLUE_FILL = "#D1E0F0"     # 浅蓝底

GREEN_D = "#0A7D35"       # 深绿：强验证
GREEN = "#31BF73"         # 标准绿：改善、验证或第三系列
GREEN_L = "#6BD99F"       # 浅绿：次级验证
GREEN_FILL = "#D1F0E0"    # 浅绿底

RISK_GOLD = "#EAD196"     # 浅金：提示、非数据装饰与低强调底色
INK = "#2D2525"           # 正文
MUTED = "#6F6464"         # 次要文字/坐标
GRID = "#EEEEEE"          # 网格、分隔线
SURFACE = "#FFFDFB"       # 暖白纸面与图表画布
CARD = PRIMARY_FILL
DATA_CARD_BG = "#FCF8F8" # 数据卡专用近白浅粉底，不与面积图的浅红填充混用
DATA_CARD_BORDER = "#EADDDD"
CHART_GRAY = "#9B9292"    # 低优先级辅助序列

# ---- 涨跌语义（红涨蓝跌）----
# 绿不承担“涨”或“跌”，只表示验证/改善，避免同一颜色在不同图中换语义。
UP = PRIMARY            # 涨 / 正（品牌红）
DOWN = BLUE             # 跌 / 负（数据蓝）
FLAT = MUTED

# ---- 类别色板：同明度红/蓝/绿优先，超过三组不循环 ----
SERIES = [PRIMARY, BLUE, GREEN, PRIMARY_L, BLUE_L, GREEN_L]

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
