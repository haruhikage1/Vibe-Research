"""投资假设沙盒 —— 用户调整财务假设 → 重算估值 → AI 点评假设合理性。

纯前端可逆计算：不荐股、不预测，只把「如果…那么…」算清楚。
损益表简化模型：净利率 ≈ 毛利率 - 费用率（忽略税/其他损益，够沙盒用）。
"""

from __future__ import annotations

import logging
import re

import astock

logger = logging.getLogger("vibe-research.scenario")

# 情景模板：default=None 表示启动时用实际值填充
SCENARIO_TEMPLATE = {
    "revenue_growth": {"label": "营收增速", "unit": "%"},
    "gross_margin": {"label": "毛利率", "unit": "%"},
    "op_expense_ratio": {"label": "费用率", "unit": "%"},
    "target_pe": {"label": "目标 PE", "unit": "x"},
    "peg_threshold": {"label": "PEG 阈值", "unit": "x", "default": 1.5},
}


def _num(v) -> float | None:
    """同花顺摘要值 → float。'746.21亿'→7.4621e10，'15.25%'→15.25，'-'→None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "--"):
        return None
    m = re.match(r"^(-?[\d.]+)(万亿|亿|万)?", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        val *= 1e8
    elif unit == "万":
        val *= 1e4
    elif unit == "万亿":
        val *= 1e12
    return val


def _pct(v) -> float | None:
    """'15.25%' / 15.25 → 15.25（百分数值）。"""
    n = _num(v)
    return n


def get_scenario_defaults(code: str) -> dict:
    """拉当前财务 + 估值，给前端面板填基准值。"""
    base = astock.full_valuation(code)
    try:
        fin = astock.financials(code)
    except astock.DependencyMissing:
        fin = {}

    price = base.get("price")
    mcap_yi = base.get("mcap_yi")
    gross_margin = _pct(fin.get("gross_margin"))
    net_margin = _pct(fin.get("net_margin"))
    revenue = _num(fin.get("revenue"))
    net_profit = _num(fin.get("net_profit"))
    revenue_yoy = _pct(fin.get("revenue_yoy"))
    pe_ttm = base.get("pe_ttm")

    # 费用率基准 ≈ 毛利率 - 净利率（忽略税）；缺毛利率时用净利/营收反推净利率
    if gross_margin is None and net_margin is None and revenue and net_profit:
        net_margin = round(net_profit / revenue * 100, 2)
    op_expense_ratio = round(gross_margin - net_margin, 2) if (gross_margin is not None and net_margin is not None) else None

    # 总股本（亿股）= 总市值(亿) × 1e8 ÷ 现价 ÷ 1e8
    shares_yi = round(mcap_yi / price, 4) if (mcap_yi and price) else None

    return {
        "code": code,
        "name": base.get("name", code),
        "period": fin.get("period"),
        "price": price,
        "pe_ttm": pe_ttm,
        "mcap_yi": mcap_yi,
        "shares_yi": shares_yi,
        "revenue_yi": round(revenue / 1e8, 2) if revenue else None,
        "net_profit_yi": round(net_profit / 1e8, 2) if net_profit else None,
        "revenue_yoy": revenue_yoy,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "op_expense_ratio": op_expense_ratio,
        "analyst_count": base.get("analyst_count", 0),
        "defaults": {
            "revenue_growth": revenue_yoy,
            "gross_margin": gross_margin,
            "op_expense_ratio": op_expense_ratio,
            "target_pe": pe_ttm,
            "peg_threshold": SCENARIO_TEMPLATE["peg_threshold"]["default"],
        },
    }


def compute_scenario(code: str, adjustments: dict, llm_cfg: dict | None = None) -> dict:
    """按用户假设重算估值。

    adjustments 缺的键用实际值补齐（前端 defaults 已预填，此处兜底再取一次）。
    llm_cfg 提供时生成 AI 点评。
    """
    base = astock.full_valuation(code)
    try:
        fin = astock.financials(code)
    except astock.DependencyMissing:
        fin = {}

    price = base.get("price")
    mcap_yi = base.get("mcap_yi")
    revenue = _num(fin.get("revenue"))
    net_profit = _num(fin.get("net_profit"))
    gross_margin = _pct(fin.get("gross_margin"))
    net_margin = _pct(fin.get("net_margin"))
    revenue_yoy = _pct(fin.get("revenue_yoy"))
    pe_ttm = base.get("pe_ttm")

    if gross_margin is None and net_margin is None and revenue and net_profit:
        net_margin = round(net_profit / revenue * 100, 2)
    base_op_ratio = round(gross_margin - net_margin, 2) if (gross_margin is not None and net_margin is not None) else None

    # 补齐用户未填的假设
    adj = {
        "revenue_growth": adjustments.get("revenue_growth") if adjustments.get("revenue_growth") is not None else revenue_yoy,
        "gross_margin": adjustments.get("gross_margin") if adjustments.get("gross_margin") is not None else gross_margin,
        "op_expense_ratio": adjustments.get("op_expense_ratio") if adjustments.get("op_expense_ratio") is not None else base_op_ratio,
        "target_pe": adjustments.get("target_pe") if adjustments.get("target_pe") is not None else pe_ttm,
        "peg_threshold": adjustments.get("peg_threshold") if adjustments.get("peg_threshold") is not None else SCENARIO_TEMPLATE["peg_threshold"]["default"],
    }

    missing = [SCENARIO_TEMPLATE[k]["label"] for k, v in adj.items() if v is None]
    if missing:
        return {"error": f"缺少基准数据，无法计算：{'、'.join(missing)}（该股财务数据可能不全）"}
    if not price or not revenue:
        return {"error": "行情或营收数据缺失，无法计算"}
    if adj["target_pe"] is not None and adj["target_pe"] <= 0:
        return {"error": "目标 PE 必须大于 0"}

    # ---- 损益表推演 ----
    shares = mcap_yi * 1e8 / price if (mcap_yi and price) else None
    new_revenue = revenue * (1 + adj["revenue_growth"] / 100)
    net_margin_new = adj["gross_margin"] - adj["op_expense_ratio"]  # 简化：净利率≈毛利率-费用率
    new_net_profit = new_revenue * net_margin_new / 100
    new_eps = new_net_profit / shares if shares else None
    implied_price = new_eps * adj["target_pe"] if (new_eps is not None and adj["target_pe"]) else None
    upside = round((implied_price / price - 1) * 100, 1) if (implied_price and price) else None

    # 隐含利润增速（对本期净利）
    profit_growth = round((new_net_profit / net_profit - 1) * 100, 1) if (net_profit and net_profit > 0) else None
    scenario_peg = round(adj["target_pe"] / profit_growth, 2) if (profit_growth and profit_growth > 0 and adj["target_pe"]) else None

    result = {
        "code": code,
        "name": base.get("name", code),
        "scenario": adj,
        "base": {
            "price": price,
            "revenue_yi": round(revenue / 1e8, 2),
            "net_profit_yi": round(net_profit / 1e8, 2) if net_profit else None,
            "gross_margin": gross_margin,
            "net_margin": net_margin,
            "op_expense_ratio": base_op_ratio,
            "pe_ttm": pe_ttm,
        },
        "projected": {
            "revenue_yi": round(new_revenue / 1e8, 2),
            "net_profit_yi": round(new_net_profit / 1e8, 2),
            "net_margin": round(net_margin_new, 2),
            "eps": round(new_eps, 3) if new_eps is not None else None,
        },
        "valuation": {
            "implied_pe": adj["target_pe"],
            "implied_price": round(implied_price, 2) if implied_price else None,
            "upside_pct": upside,
            "profit_growth_pct": profit_growth,
            "scenario_peg": scenario_peg,
            "peg_threshold": adj["peg_threshold"],
        },
        "ai_commentary": "",
        "ai_commentary_status": "pending",
    }

    if llm_cfg:
        _ai_commentary(result, llm_cfg)

    return result


def _ai_commentary(result: dict, cfg: dict) -> None:
    """调 LLM 点评假设合理性（3-5 句），写回 result。合规：不荐股、不给买卖建议。"""
    from chat import run_chat

    b, p, v = result["base"], result["projected"], result["valuation"]
    s = result["scenario"]
    prompt = (
        f"用户在假设沙盒里对 {result['name']}（{result['code']}）做了如下调整，请点评假设的合理性。\n"
        f"实际基准：营收 {b['revenue_yi']} 亿、净利 {b['net_profit_yi']} 亿、毛利率 {b['gross_margin']}%、"
        f"费用率 {b['op_expense_ratio']}%、PE(TTM) {b['pe_ttm']}、现价 {b['price']}。\n"
        f"用户假设：营收增速 {s['revenue_growth']}%、毛利率 {s['gross_margin']}%、费用率 {s['op_expense_ratio']}%、目标 PE {s['target_pe']}x。\n"
        f"推演结果：营收 {p['revenue_yi']} 亿、净利 {p['net_profit_yi']} 亿（净利率 {p['net_margin']}%）、"
        f"隐含价格 {v['implied_price']} 元（较现价空间 {v['upside_pct']}%）、隐含利润增速 {v['profit_growth_pct']}%、情景 PEG {v['scenario_peg']}。\n\n"
        "请用中文写 3-5 句点评：各假设相对实际基准的偏离幅度是否在合理范围（如毛利率变化是否符合行业周期常识、"
        f"情景 PEG {v['scenario_peg']} 相对阈值 {v['peg_threshold']} 的含义），只做客观分析，不预测涨跌、不推荐买卖。"
    )
    try:
        out = run_chat(cfg, [{"role": "user", "content": prompt}], context="假设沙盒点评")
        result["ai_commentary"] = out.get("content", "")
        result["ai_commentary_status"] = "done"
    except Exception as e:
        logger.error("假设点评失败：%s", e)
        result["ai_commentary"] = ""
        result["ai_commentary_status"] = "error"
