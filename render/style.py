"""图表与一页通视觉风格（经 dataviz 方法论校验）。

配色锚定 0720《场外衍生品投资策略》模板（光大证券风格）：深红主色 + 中性灰，浅色底。
类别色板已用 dataviz 的 validate_palette.js 校验：色盲安全、对比达标、固定顺序不循环。

本地化要点：中国金融惯例【红涨绿跌】，覆盖 dataviz 默认的"绿=好"。
"""

from __future__ import annotations

import matplotlib as mpl

# ---- 品牌色 ----
# #82 第一版把饱和度压得太狠（#7E3833），色相偏向砖褐，整页像**褪了色**。
# 现修正为：**保持红的彩度**（S≈63%），只把明度压深一档——
# 深而饱和 = 沉稳；浅而灰 = 褪色。两者完全不同，降饱和不等于高级。
PRIMARY = "#9B2226"    # 深红：标题、核心数字、主标记（红调明确，不发褐）
PRIMARY_D = "#6E1719"  # 深一档
PRIMARY_L = "#C55A55"  # 浅一档（次级元素、浅填充）
INK = "#2B2724"        # 正文（暖调墨色，比纯黑柔和）
MUTED = "#8A837C"      # 次要文字/坐标
GRID = "#EFE4E3"       # 网格、分隔线（跟着浅红底走暖）
SURFACE = "#FEFBFA"    # 画布底：极淡浅红，与一页通版面同色，图不再是白方块
CARD = "#FBF4F3"       # 卡片底（比画布深一档的浅红）

# ---- 涨跌语义（红涨绿跌）----
# 涨用主红同一色系，跌用足够深的绿：两者**明度差**保持够大，
# 确保色觉障碍读者靠深浅也能区分，不单靠红绿色相。
UP = "#A82C2A"         # 涨 / 正（红）
DOWN = "#2E7D5B"       # 跌 / 负（深绿）
FLAT = MUTED

# ---- 类别色板：红色系深浅阶梯 ----
# ⚠ 取舍写明：原色板是**多色相**（红/蓝/金/紫/绿），经 dataviz 的
# validate_palette.js 校验过色盲安全。改成单色相后色相不再承担区分作用，
# 故**改用明度阶梯**——五档亮度依次拉开，转成灰度仍可分辨，
# 这是单色系配色保证可读性的标准做法。同时图表一律直接标数值与标签，
# 不依赖"看颜色猜是哪条"，色板只负责视觉层次。
SERIES = ["#6E1719", "#9B2226", "#BE4B47", "#D98C86", "#A8A29B"]

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
