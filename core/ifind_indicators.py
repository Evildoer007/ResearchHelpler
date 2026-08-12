"""schema 业务字段 → iFinD 指标代码 的映射（iFinD 适配表）。

分工：
  schema.py         定义业务字段是什么（含义/单位/取数方式）
  ifind_indicators  把字段翻译成 iFinD 的源生指标代码 + 参数要求（本文件）
  fetcher.py        运行时按标的+日期，用 provider 调 iFinD 取数

指标代码经茅台(600519.SH)实测确认。参数因指标而异：
  param_type = "none"          无需参数
             = "trade_date"    需传交易日（如换手率）
             = "report_period" 需传报告期（如 ROE 年报）
fetcher 运行时按 param_type 填入实际日期。

status:
  verified  已实测命中，值合理
  uncertain 能返回值但口径/参数含义待确认
  pending   指标名未探测到，需在 iFinD"查询函数"面板核对
"""

from __future__ import annotations

VERIFIED = "verified"
UNCERTAIN = "uncertain"
PENDING = "pending"

# 键 = schema 业务字段名（与 schema.py 对齐）或通用基础字段
IFIND_MAP: dict[str, dict] = {
    # ---- 已实测命中 ----
    "PB": {
        "indicator": "ths_pb_latest_stock", "param_type": "none",
        "status": VERIFIED, "note": "茅台6.83；另有 ths_pb_mrq_stock(最近季度)",
    },
    "ROE": {
        "indicator": "ths_roe_ttm_stock", "param_type": "none",
        "status": VERIFIED, "note": "TTM口径，茅台30.53%；年报口径用 ths_roe_stock+报告期",
    },
    "换手率": {
        "indicator": "ths_turnover_ratio_stock", "param_type": "trade_date",
        "status": VERIFIED, "note": "需传交易日，茅台0.286%",
    },
    # ---- 通用基础字段（非 schema 主字段，但常用/供 DERIVED 组合）----
    "股票简称": {
        "indicator": "ths_stock_short_name_stock", "param_type": "none", "status": VERIFIED,
    },
    "最新价": {
        "indicator": "ths_close_price_stock", "param_type": "none", "status": VERIFIED,
    },
    "PE_TTM": {
        "indicator": "ths_pe_ttm_stock", "param_type": "none", "status": VERIFIED,
    },
    "总市值": {
        "indicator": "ths_market_value_stock", "param_type": "none",
        "status": VERIFIED, "note": "单位元，茅台1.61万亿",
    },

    "A股股息率": {
        "indicator": "ths_dividend_yield_ttm_ex_sd_stock", "param_type": "trade_date",
        "status": VERIFIED,
        "note": "TTM不含特别分红(标准口径)，参数=截止日期；茅台4.01% 中信2.50%",
    },
    "归母净利同比": {
        "indicator": "ths_np_atsopc_yoy_stock", "param_type": "report_period",
        "status": VERIFIED,
        "note": "累计口径(研报用)，报告期填季度末如2026-03-31；单季度版 ths_sq_np_atsopc_yoy_stock。茅台26Q1=+1.47%",
    },
    "现金分红总额": {
        "indicator": "ths_unit_total_cash_dividend_stock", "param_type": "date_range",
        "status": VERIFIED,
        "note": "单只口径，参数='起始日,截止日,币种(CNY)'；板块级需对成分股求和(DERIVED)。茅台2025=646.7亿",
    },

    # ---- 待确认 / 待面板 ----
    "分红率": {
        "indicator": None, "param_type": "report_period",
        "status": UNCERTAIN,
        "note": "可 DERIVED=现金分红总额/归母净利润；直取指标 ths_dividend_ratio_stock(传100=42.56)口径待确认",
    },
    "H股股息率": {
        "indicator": None, "param_type": "trade_date",
        "status": PENDING, "note": "港股股息率，待面板（原 schema 标 MANUAL，iFinD 或可取）",
    },
}


def get_mapping(field: str) -> dict | None:
    return IFIND_MAP.get(field)


def fields_by_status(status: str) -> list[str]:
    return [k for k, v in IFIND_MAP.items() if v["status"] == status]


if __name__ == "__main__":  # python -m core.ifind_indicators
    for s in (VERIFIED, UNCERTAIN, PENDING):
        print(f"[{s}] {fields_by_status(s)}")
