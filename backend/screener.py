"""自然语言筛选器 —— NL → 结构化条件 → 逐层筛选 → 结果表格。

流程：
  1. parse_criteria() 用 LLM 把自然语言拆成结构化筛选条件
  2. resolve_universe() 确定筛选范围（行业/全市场）
  3. execute_screen() 从宽到窄逐条件过滤，每步记录日志
  4. screening_pipeline() 编排全流程
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import astock

logger = logging.getLogger("vibe-research.screener")

# ---- 条件解析：NL → 结构化条件（调 LLM） ----

CRITERIA_PARSE_PROMPT = """你是一个 A 股选股条件解析器。请将用户的自然语言筛选需求解析为结构化 JSON。

可用的筛选字段（all optional）：
- industry: str — 行业/板块名称（如"半导体""新能源车"）
- concept: str — 概念主题（如"AI算力""机器人"）
- price_min / price_max: float — 股价范围
- pe_min / pe_max: float — PE(TTM) 范围
- pb_min / pb_max: float — PB 范围
- mcap_min / mcap_max: float — 总市值范围（亿）
- turnover_min / turnover_max: float — 换手率范围（%）
- revenue_growth_min / revenue_growth_max: float — 营收增速范围（%）
- profit_growth_min / profit_growth_max: float — 净利润增速范围（%）
- roe_min / roe_max: float — ROE 范围（%）
- gross_margin_min / gross_margin_max: float — 毛利率范围（%）
- analyst_count_min: int — 最少机构覆盖数
- rating_upgrade: bool — 最近是否有研报上调评级
- holder_decrease: bool — 股东户数是否在减少
- fund_flow_positive: bool — 主力资金是否净流入
- limit_up_today: bool — 今日是否涨停
- consecutive_limit_up_min: int — 最少连板数

只返回 JSON，不要任何解释。JSON 格式：
{"industry": "...", "concept": "...", "filters": [{"field": "pe_max", "value": 30}, ...]}

如果用户未指定某个条件，不要包含它。如果无法解析，返回 {"error": "无法理解筛选条件"}。"""


def parse_criteria(nl_query: str, chat_cfg: dict) -> dict:
    """用 LLM 把自然语言拆成结构化筛选条件。"""
    from chat import run_chat

    result = run_chat(chat_cfg, [{"role": "user", "content": f"用户需求：{nl_query}"}], context=CRITERIA_PARSE_PROMPT)
    raw = (result.get("content") or "").strip()
    # 尝试提取 JSON（模型可能输出 markdown 包围）
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        raw = json_match.group(1)
    try:
        criteria = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": f"无法解析 LLM 输出：{raw[:200]}"}
    if not isinstance(criteria, dict):
        return {"error": "解析结果格式错误"}
    if "error" in criteria:
        return criteria
    # 规范化
    criteria.setdefault("filters", [])
    # 把顶层字段也转成 filters 格式
    for key in ("industry", "concept"):
        if criteria.get(key):
            criteria["filters"].insert(0, {"field": key, "value": criteria[key]})
    return criteria


# ---- 股票池解析 ----

_INDUSTRY_STOCK_CACHE: dict[str, list[dict]] = {}


def _get_industry_stocks(industry_name: str) -> list[dict]:
    """获取某行业的所有个股（通过 akshare 行业板块成分股）。"""
    cache_key = f"ind:{industry_name}"
    hit = _INDUSTRY_STOCK_CACHE.get(cache_key)
    if hit is not None:
        return hit

    try:
        ak = astock._akshare()
        # 获取所有行业板块列表
        df = ak.stock_board_industry_name_em()
        match = df[df["board_name"].str.contains(industry_name, case=False, na=False)]
        if match.empty:
            return []
        board_code = match.iloc[0]["board_code"]
        cons = ak.stock_board_industry_cons_em(symbol=board_code)
        stocks = [{"code": str(row["code"]).zfill(6), "name": str(row["name"])}
                  for _, row in cons.iterrows()]
        _INDUSTRY_STOCK_CACHE[cache_key] = stocks
        return stocks
    except Exception as e:
        logger.error("获取行业 %s 成分股失败：%s", industry_name, e)
        return []


def resolve_universe(criteria: dict) -> list[str]:
    """确定筛选范围：从行业/概念出发，或返回空（由上层决定兜底策略）。"""
    codes: list[str] = []
    industry = criteria.get("industry", "")
    if industry:
        stocks = _get_industry_stocks(industry)
        codes = [s["code"] for s in stocks]
    # 如果没有行业限制，返回空列表，由上层用全市场 TOP N 兜底
    return codes


# ---- 筛选执行 ----

def _batch_quote_filter(codes: list[str], filters: list[dict]) -> tuple[list[str], list[dict]]:
    """用 batch quote 做第一轮过滤（PE/PB/市值/换手率等行情字段）。
    返回 (通过列表, 筛选步骤日志)。
    """
    if not codes:
        return [], []

    quotes = astock.tencent_quote(codes)
    step_log = {"step": "行情过滤", "input_count": len(codes), "conditions": [], "output_count": 0}
    passed = []

    for code in codes:
        q = quotes.get(code)
        if not q:
            continue
        ok = True
        matched_conds = []
        for f in filters:
            field = f.get("field", "")
            val = f.get("value")
            if val is None:
                continue
            qv = {
                "pe_max": q.get("pe_ttm"), "pe_min": q.get("pe_ttm"),
                "pb_max": q.get("pb"), "pb_min": q.get("pb"),
                "price_max": q.get("price"), "price_min": q.get("price"),
                "mcap_max": q.get("mcap_yi"), "mcap_min": q.get("mcap_yi"),
                "turnover_max": q.get("turnover_pct"), "turnover_min": q.get("turnover_pct"),
            }.get(field)
            if qv is None:
                continue
            op = f.get("field", "").split("_")[-1]
            if op == "max" and qv is not None and qv > val:
                ok = False
            elif op == "min" and qv is not None and qv < val:
                ok = False
            if ok:
                matched_conds.append(field)
        if ok:
            passed.append(code)

    step_log["output_count"] = len(passed)
    step_log["conditions"] = [f.get("field", "") for f in filters if f.get("field") in (
        "pe_max", "pe_min", "pb_max", "pb_min", "price_max", "price_min",
        "mcap_max", "mcap_min", "turnover_max", "turnover_min",
    )]
    return passed, [step_log]


def _single_stock_filter(code: str, filters: list[dict], quotes: dict) -> bool:
    """对单只个股执行非行情类过滤条件。

    filters: 需要财务/研报/股东数据的条件
    """
    ok = True
    for f in filters:
        field = f.get("field", "")
        val = f.get("value")
        if field in ("revenue_growth_min", "revenue_growth_max"):
            fin = astock.financials(code)
            rg = fin.get("revenue_yoy")
            if rg is None:
                ok = False
                break
            op = "max" if "max" in field else "min"
            if op == "max" and rg > val:
                ok = False; break
            if op == "min" and rg < val:
                ok = False; break

        elif field in ("profit_growth_min", "profit_growth_max"):
            fin = astock.financials(code)
            pg = fin.get("net_profit_yoy")
            if pg is None:
                ok = False; break
            op = "max" if "max" in field else "min"
            if op == "max" and pg > val:
                ok = False; break
            if op == "min" and pg < val:
                ok = False; break

        elif field in ("roe_min", "roe_max"):
            fin = astock.financials(code)
            rv = fin.get("roe")
            if rv is None:
                ok = False; break
            op = "max" if "max" in field else "min"
            if op == "max" and rv > val:
                ok = False; break
            if op == "min" and rv < val:
                ok = False; break

        elif field in ("gross_margin_min", "gross_margin_max"):
            fin = astock.financials(code)
            gm = fin.get("gross_margin")
            if gm is None:
                ok = False; break
            op = "max" if "max" in field else "min"
            if op == "max" and gm > val:
                ok = False; break
            if op == "min" and gm < val:
                ok = False; break

        elif field == "analyst_count_min":
            val_info = astock.full_valuation(code)
            ac = val_info.get("analyst_count", 0)
            if ac is None or ac < val:
                ok = False; break

        elif field == "rating_upgrade" and val:
            reports = astock.eastmoney_reports(code, max_pages=1)
            if not reports:
                ok = False; break
            # Check if any report has upgrade rating
            has_up = any(
                r.get("emRatingName") in ("买入", "增持", "强烈推荐")
                for r in reports[:5]
            )
            if not has_up:
                ok = False; break

        elif field == "holder_decrease" and val:
            holders = astock.holder_num_change(code, page_size=3)
            if not holders:
                ok = False; break
            latest = holders[0].get("change_ratio", 0)
            if latest is None or latest >= 0:
                ok = False; break

        elif field == "fund_flow_positive" and val:
            flows = astock.stock_fund_flow_120d(code)
            if not flows:
                ok = False; break
            recent = sum(f.get("main_net", 0) or 0 for f in flows[:5])
            if recent <= 0:
                ok = False; break

    return ok


def execute_screen(criteria: dict) -> dict:
    """执行筛选流水线。

    步骤：
      1. 行业/概念 → 确定初始股票池
      2. 行情批量过滤（PE/PB/市值/换手率）
      3. 逐只过滤（财务/研报/股东/资金流）
      4. 组装结果
    """
    filters = criteria.get("filters", [])
    pipeline: list[dict] = []

    # ---- Step 1: 解析行业/概念，获取股票池 ----
    universe = resolve_universe(criteria)
    if not universe:
        # 兜底：用成交额 TOP 200 作为默认池
        try:
            top_stocks = astock.market_turnover_rank(200)
            universe = [s["code"] for s in top_stocks]
        except Exception:
            return {"results": [], "total": 0, "pipeline": [{"step": "获取股票池", "error": "无法获取初始股票池"}]}
    pipeline.append({
        "step": "确定筛选范围",
        "industry": criteria.get("industry", "全市场"),
        "count": len(universe),
    })

    # ---- Step 2: 行情字段批量过滤 ----
    quote_fields = {"pe_max", "pe_min", "pb_max", "pb_min", "price_max", "price_min",
                    "mcap_max", "mcap_min", "turnover_max", "turnover_min"}
    quote_filters = [f for f in filters if f.get("field") in quote_fields]
    deep_filters = [f for f in filters if f.get("field") not in quote_fields
                    and f.get("field") not in ("industry", "concept")]

    if quote_filters:
        universe, quote_logs = _batch_quote_filter(universe, quote_filters)
        pipeline.extend(quote_logs)
    else:
        pipeline.append({"step": "行情过滤", "input_count": len(universe), "conditions": [], "output_count": len(universe)})

    if not universe:
        pipeline.append({"step": "过滤结果", "count": 0})
        return {"results": [], "total": 0, "pipeline": pipeline}

    # ---- Step 3: 深层次过滤（财务/研报/股东等） ----
    if deep_filters:
        results: list[dict] = []
        quotes = astock.tencent_quote(universe) if universe else {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            fut_map = {pool.submit(_single_stock_filter, c, deep_filters, quotes): c for c in universe}
            for fut in as_completed(fut_map):
                code = fut_map[fut]
                try:
                    if fut.result():
                        q = quotes.get(code, {})
                        results.append({
                            "code": code,
                            "name": q.get("name", code),
                            "price": q.get("price", 0),
                            "change_pct": q.get("change_pct", 0),
                            "pe_ttm": q.get("pe_ttm"),
                            "pb": q.get("pb"),
                            "mcap_yi": q.get("mcap_yi"),
                            "turnover_pct": q.get("turnover_pct"),
                        })
                except Exception:
                    pass
        pipeline.append({
            "step": "深度过滤",
            "conditions": [f.get("field") for f in deep_filters],
            "input_count": len(universe),
            "output_count": len(results),
        })
    else:
        quotes = astock.tencent_quote(universe)
        results = [{
            "code": c, "name": q.get("name", c),
            "price": q.get("price", 0), "change_pct": q.get("change_pct", 0),
            "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
            "mcap_yi": q.get("mcap_yi"), "turnover_pct": q.get("turnover_pct"),
        } for c, q in quotes.items()]

    results.sort(key=lambda x: -(x.get("mcap_yi") or 0))
    pipeline.append({"step": "完成", "total": len(results)})
    return {"results": results, "total": len(results), "pipeline": pipeline}


def screening_pipeline(nl_query: str, chat_cfg: dict) -> dict:
    """NL → 解析 → 筛选 全流程。"""
    criteria = parse_criteria(nl_query, chat_cfg)
    if "error" in criteria:
        return {"error": criteria["error"], "criteria": criteria, "results": [], "total": 0, "pipeline": []}
    result = execute_screen(criteria)
    result["criteria"] = criteria
    return result
