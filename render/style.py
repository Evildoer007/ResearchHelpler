"""图表与一页通视觉风格（经 dataviz 方法论校验）。

配色锚定 0720《场外衍生品投资策略》模板（光大证券风格）：深红主色 + 中性灰，浅色底。
类别色板已用 dataviz 的 validate_palette.js 校验：色盲安全、对比达标、固定顺序不循环。

本地化要点：中国金融惯例【红涨绿跌】，覆盖 dataviz 默认的"绿=好"。
"""

from __future__ import annotations

import matplotlib as mpl

# ---- 品牌色 ----
PRIMARY = "#A32C2C"    # 光大深红：标题、核心数字、主标记
PRIMARY_D = "#7E2020"  # 深一档
INK = "#2B2B2B"        # 正文
MUTED = "#8A8A8A"      # 次要文字/坐标
GRID = "#E6E3E1"       # 网格、分隔线
SURFACE = "#FFFFFF"    # 画布底
CARD = "#F6F2F0"       # 卡片底（暖浅）

# ---- 涨跌语义（红涨绿跌）----
UP = "#C0392B"         # 涨 / 正
DOWN = "#2E8B57"       # 跌 / 负
FLAT = MUTED

# ---- 类别色板（validate_palette.js 全通过；固定顺序，勿循环）----
SERIES = ["#A32C2C", "#2C6E9E", "#B0791F", "#6B4A9E", "#2E8B57"]

FONT = "Microsoft YaHei"


def apply_style() -> None:
    """套用全局 matplotlib 风格（含中文字体）。绘图前调用一次。"""
    mpl.rcParams.update({
        "font.sans-serif": [FONT, "SimHei", "SimSun"],
        "font.size": 11,
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
