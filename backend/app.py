"""Vibe-Research 后端 —— A股数据层 HTTP 接口（FastAPI）。

端点全部在 /api 下，前端 vite 代理 /api → localhost:8900。
只读、无状态、按用户传入代码返回客观数据。不预置标的、不建议。

启动：
    uvicorn app:app --host 127.0.0.1 --port 8900
"""

from __future__ import annotations

import json
import logging
import os
import traceback

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import astock
import chat as chat_layer
import cli_runtime
import conversations
import cross_market
import daily_brief
import gstock
import scenario
import screener
import newsradar
import portfolio as pf
import market
import myreports as mr
import rag

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vibe-research")

app = FastAPI(title="Vibe-Research API", version="0.1.3")

# 每半小时后台刷新持仓数据
pf.start_scheduler(1800)

# CORS：默认放开（本地自托管友好）；公网部署时用 VR_ALLOW_ORIGINS 收紧成白名单。
#   例：VR_ALLOW_ORIGINS="https://myhost"  （逗号分隔多个）
_ORIGINS = [o.strip() for o in os.environ.get("VR_ALLOW_ORIGINS", "*").split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 可选鉴权：设了 VR_API_KEY 就要求所有 /api/* 带 `Authorization: Bearer <key>`
#   （本地自托管不设=开放；公网部署务必设，否则别人能读你的持仓/调你的后端）。
_API_KEY = os.environ.get("VR_API_KEY", "").strip()


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    if (
        _API_KEY
        and request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/health"
    ):
        if request.headers.get("authorization", "") != f"Bearer {_API_KEY}":
            return JSONResponse({"detail": "未授权：缺少或错误的 API Key（VR_API_KEY）"}, status_code=401)
    return await call_next(request)

_CODE_RE = r"^\d{6}$"


def _validate(code: str) -> str:
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "代码必须是 6 位数字")
    return code


@app.get("/api/health")
def health():
    return {"ok": True, "service": "vibe-research-api", "version": "0.1.3"}


class LLMConfig(BaseModel):
    provider: str = ""       # cli-* = 订阅接入（调本机 CLI）；其余 = API 接入
    baseURL: str = ""        # 订阅接入时留空
    apiKey: str = ""         # 订阅接入时留空
    model: str


class ChatReq(BaseModel):
    messages: list[dict]
    context: str = ""
    llm: LLMConfig


@app.post("/api/chat")
def chat(req: ChatReq):
    """系统 AI 对话，**流式** NDJSON（每行一个事件 {type: tool|delta|done|error}）。

    - API 接入：OpenAI 兼容 function-calling，边流答案边推工具调用事件。
    - 订阅接入（provider=cli-*）：调本机已登录的 CLI，stdout 边出边流（数据靠 context）。
    配置错误（缺 key / 未装 CLI）走 HTTP 400；运行时错误走流内 error 事件。用户配置随请求传入，后端不持久化。
    """
    if not req.messages:
        raise HTTPException(400, "messages 不能为空")
    if not req.llm.model:
        raise HTTPException(400, "缺少模型配置，请先在「接入 AI」里选择")

    is_cli = req.llm.provider.startswith("cli-")
    if is_cli:
        kind = req.llm.provider[4:]
        if not cli_runtime.detect_cli(kind):
            raise HTTPException(400, f"未检测到「{kind}」对应的本机命令。请先安装并登录该 CLI，或改用「API 接入」。")
    elif not req.llm.apiKey or not req.llm.baseURL:
        raise HTTPException(400, "缺少 Base URL 或 API Key，请先在「接入 AI」里填写")

    cfg = req.llm.model_dump() if hasattr(req.llm, "model_dump") else req.llm.dict()

    def gen():
        try:
            events = (chat_layer.run_chat_cli_stream if is_cli else chat_layer.run_chat_stream)(cfg, req.messages, req.context)
            for ev in events:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 — 运行时错误以流内事件上报，不中断连接
            logger.error("对话失败：%s\n%s", e, traceback.format_exc())
            yield json.dumps({"type": "error", "message": f"对话失败：{e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class CommitteeReq(BaseModel):
    messages: list[dict]
    context: str = ""
    llm: LLMConfig
    agents: list[str] = []


@app.post("/api/chat/committee")
def chat_committee(req: CommitteeReq):
    """多 Agent 投研委员会：并行运行 N 个不同风格的 AI agent，流式 NDJSON。

    每个事件带 `agent` 字段标识来源，前端可据此分列渲染。
    各 agent 共享同一份 context，工具调用结果带缓存避免重复拉数据。
    """
    if not req.messages:
        raise HTTPException(400, "messages 不能为空")
    if not req.llm.model:
        raise HTTPException(400, "缺少模型配置，请先在「接入 AI」里选择")
    if not req.agents:
        req.agents = chat_layer.DEFAULT_COMMITTEE
    if not req.llm.apiKey or not req.llm.baseURL:
        raise HTTPException(400, "缺少 Base URL 或 API Key，请先在「接入 AI」里填写")

    cfg = req.llm.model_dump() if hasattr(req.llm, "model_dump") else req.llm.dict()

    def gen():
        try:
            events = chat_layer.run_chat_committee_stream(cfg, req.messages, req.context, req.agents)
            for ev in events:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001
            logger.error("委员会对话失败：%s\n%s", e, traceback.format_exc())
            yield json.dumps({"type": "error", "message": f"委员会对话失败：{e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class DailyBriefReq(BaseModel):
    watch_codes: list[str] = []
    llm: LLMConfig | None = None


class DailyBriefCommentaryReq(BaseModel):
    llm: LLMConfig


@app.post("/api/daily-brief/generate")
def daily_brief_generate(req: DailyBriefReq):
    """生成今日投资复盘录：聚合市场、持仓、自选、资讯数据，可选生成 AI 简评。

    返回完整的复盘录 JSON。如果有 llm 配置，会自动调用 AI 生成简评。
    """
    try:
        log = daily_brief.generate_brief(watch_codes=req.watch_codes)
        if req.llm and req.llm.model and req.llm.apiKey and req.llm.baseURL:
            cfg = req.llm.model_dump() if hasattr(req.llm, "model_dump") else req.llm.dict()
            daily_brief.generate_ai_commentary(log, cfg)
        return {"data": log}
    except Exception as e:
        logger.error("生成复盘录失败：%s\n%s", e, traceback.format_exc())
        raise HTTPException(502, f"生成复盘录失败：{e}") from e


@app.post("/api/daily-brief/commentary")
def daily_brief_commentary(req: DailyBriefCommentaryReq):
    """为今日复盘录生成/刷新 AI 简评（需 LLM 配置）。"""
    if not req.llm.model or not req.llm.apiKey or not req.llm.baseURL:
        raise HTTPException(400, "缺少 AI 配置")
    try:
        log = daily_brief.get_brief()
        if not log:
            raise HTTPException(404, "今天还没有生成复盘录，请先生成")
        cfg = req.llm.model_dump() if hasattr(req.llm, "model_dump") else req.llm.dict()
        daily_brief.generate_ai_commentary(log, cfg)
        return {"data": daily_brief.get_brief()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI 简评生成失败：%s\n%s", e, traceback.format_exc())
        raise HTTPException(502, f"AI 简评生成失败：{e}") from e


@app.get("/api/daily-brief")
def daily_brief_get(date: str = Query(None)):
    """取指定日期的复盘录。date=YYYY-MM-DD，默认今天。"""
    log = daily_brief.get_brief(date=date)
    if not log:
        raise HTTPException(404, f"未找到{'今天' if not date else date}的复盘录")
    return {"data": log}


@app.get("/api/daily-brief/history")
def daily_brief_history():
    """历史复盘录列表（倒序）。"""
    return {"data": daily_brief.list_briefs()}


class ScreenerReq(BaseModel):
    query: str
    llm: LLMConfig


@app.post("/api/screener")
def screener_run(req: ScreenerReq):
    """自然语言筛选器：NL → 结构化条件 → 逐层过滤 → 结果表格。

    请求体包含用户的自然语言查询和 LLM 配置（用于解析条件）。
    返回筛选结果 + 筛选链路日志（前端展示每一步做了什么）。
    """
    if not req.query.strip():
        raise HTTPException(400, "筛选条件不能为空")
    if not req.llm.model or not req.llm.apiKey or not req.llm.baseURL:
        raise HTTPException(400, "缺少 AI 配置，条件解析需要 LLM")
    cfg = req.llm.model_dump() if hasattr(req.llm, "model_dump") else req.llm.dict()
    try:
        result = screener.screening_pipeline(req.query, cfg)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return {"data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("筛选失败：%s\n%s", e, traceback.format_exc())
        raise HTTPException(502, f"筛选失败：{e}") from e


@app.get("/api/cross-market/signals")
def cross_market_signals():
    """跨境信号映射：检测美港股异动 → 匹配关联 A 股板块/个股行情。

    返回当前所有监控标的的行情 + 关联板块涨跌 + 代表个股行情。
    |涨跌幅| >= 2% 标记为活跃信号。
    """
    try:
        return {"data": cross_market.get_active_signals()}
    except Exception as e:  # noqa: BLE001
        logger.error("跨境信号异常：%s\n%s", e, traceback.format_exc())
        raise HTTPException(502, f"跨境信号异常：{e}") from e


@app.get("/api/cross-market/correlation")
def cross_market_correlation():
    """联动强度摘要：美股/港股 → A 股板块联动关系列表。"""
    try:
        return {"data": cross_market.get_correlation_summary()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"联动强度异常：{e}") from e


@app.get("/api/scenario/defaults")
def scenario_defaults(code: str = Query(...)):
    """假设沙盒基准值：当前财务 + 估值，供前端面板预填。"""
    code = _validate(code)
    try:
        return {"data": scenario.get_scenario_defaults(code)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"假设基准值异常：{e}") from e


class ScenarioReq(BaseModel):
    code: str
    revenue_growth: float | None = None
    gross_margin: float | None = None
    op_expense_ratio: float | None = None
    target_pe: float | None = None
    peg_threshold: float | None = None
    llm: LLMConfig | None = None


@app.post("/api/scenario")
def scenario_compute(req: ScenarioReq):
    """假设沙盒：按用户假设重算估值，可选生成 AI 点评。"""
    code = _validate(req.code)
    cfg = None
    if req.llm and req.llm.model and req.llm.apiKey and req.llm.baseURL:
        cfg = req.llm.model_dump() if hasattr(req.llm, "model_dump") else req.llm.dict()
    try:
        adjustments = {
            "revenue_growth": req.revenue_growth,
            "gross_margin": req.gross_margin,
            "op_expense_ratio": req.op_expense_ratio,
            "target_pe": req.target_pe,
            "peg_threshold": req.peg_threshold,
        }
        result = scenario.compute_scenario(code, adjustments, llm_cfg=cfg)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return {"data": result}
    except HTTPException:
        raise
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("假设计算异常：%s\n%s", e, traceback.format_exc())
        raise HTTPException(502, f"假设计算异常：{e}") from e


class HoldingIn(BaseModel):
    code: str
    shares: float
    cost: float


@app.get("/api/portfolio")
def portfolio_get():
    """持仓 + 实时盈亏（浮动盈亏红涨绿跌）。"""
    try:
        return {"data": pf.get_portfolio()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"持仓读取异常：{e}") from e


@app.post("/api/portfolio/holding")
def portfolio_add(h: HoldingIn):
    """加一笔持仓（同代码按加权平均成本合并）。存本地，不上传。"""
    code = (h.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "代码必须是 6 位数字")
    if h.shares <= 0:
        raise HTTPException(400, "数量必须大于 0")
    # 成本价不限正负：融券 / 返息 / 摊薄后为负成本等情形按结果计算，用户想怎么输就怎么输。
    return {"data": pf.add_holding(code, h.shares, h.cost)}


@app.delete("/api/portfolio/holding")
def portfolio_remove(code: str = Query(...)):
    return {"data": pf.remove_holding(code.strip())}


# ---- 我的研报（用户上传自己的研报，存本地、不上传、不进开源仓库）----

class ReportIn(BaseModel):
    name: str
    content_b64: str


@app.get("/api/myreports")
def myreports_list():
    return {"data": mr.list_reports()}


@app.post("/api/myreports")
def myreports_upload(r: ReportIn):
    """上传一份研报（base64）→ 存本地 + 按文件名自动打行业标签。"""
    try:
        return {"data": mr.save_report(r.name, r.content_b64)}
    except mr.ReportError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/myreports/file/{rid}")
def myreports_file(rid: str):
    """下载/预览某份研报原文件。"""
    hit = mr.report_path(rid)
    if not hit:
        raise HTTPException(404, "研报不存在")
    path, name = hit
    return FileResponse(str(path), filename=name)


@app.delete("/api/myreports/{rid}")
def myreports_delete(rid: str):
    return {"data": {"ok": mr.delete_report(rid)}}


class CloseIn(BaseModel):
    code: str
    date: str
    price: float
    shares: float
    cost: float


@app.post("/api/portfolio/close")
def portfolio_close(c: CloseIn):
    """记一笔已清仓（已实现盈亏）。存本地。"""
    code = (c.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "代码必须是 6 位数字")
    if c.price <= 0 or c.shares <= 0:
        raise HTTPException(400, "清仓价与股数必须大于 0")
    # 买入成本不限正负（同持仓录入）：按 (清仓价 - 成本) × 股数 的结果计算已实现盈亏。
    date = (c.date or "").strip()
    if not date:
        raise HTTPException(400, "请填清仓日期")
    from datetime import datetime
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "清仓日期格式应为 YYYY-MM-DD") from None
    return {"data": pf.close_position(code, date, c.price, c.shares, c.cost)}


@app.delete("/api/portfolio/close")
def portfolio_close_remove(index: int = Query(...)):
    return {"data": pf.remove_closed(index)}


@app.post("/api/portfolio/refresh")
def portfolio_refresh():
    """手动刷新：立即重拉行情算盈亏。"""
    try:
        return {"data": pf.get_portfolio()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"刷新失败：{e}") from e


@app.get("/api/radar")
def radar():
    """资讯雷达：12 赛道公开 RSS 资讯（读缓存，无缓存返回赛道骨架）。"""
    try:
        return {"data": newsradar.get_radar(force=False)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资讯雷达异常：{e}") from e


@app.post("/api/radar/refresh")
def radar_refresh():
    """强制重抓全部 RSS 源（耗时约 20-40s），更新缓存。"""
    try:
        return {"data": newsradar.fetch_radar()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资讯雷达刷新失败：{e}") from e


@app.get("/api/market/overview")
def market_overview():
    """市场情绪 + 板块资金流（板块/大盘级，全站共享缓存 5 分钟）。"""
    try:
        return {"data": market.get_overview()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"市场总览异常：{e}") from e


@app.get("/api/market/emotion")
def market_emotion():
    """短线情绪：连板梯队 / 最高连板 / 炸板率 / 封板率 / 晋级率 / 涨跌停家数。

    含连板梯队个股清单（code/name/连板数等）——2026-07-05 起如实展示客观公开榜单（东财同款），
    只呈现事实，不附推荐/评分/预测/买卖时机。全站共享缓存 5 分钟。
    """
    try:
        return {"data": market.get_short_term_emotion()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"短线情绪异常：{e}") from e


@app.get("/api/market/turnover-top")
def market_turnover_top():
    """全市场成交额榜 Top20（客观公开榜单数据，非推荐/非预测/不评分）。全站共享缓存 5 分钟。"""
    try:
        return {"data": market.get_turnover_top()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"成交额榜异常：{e}") from e


@app.get("/api/global/indices")
def global_indices():
    """全球指数快照（道指 / 标普500 / 纳斯达克 / 恒生 / 恒生科技）—— A 股看隔夜外围脸色。缓存 5 分钟。"""
    try:
        return {"data": market.get_global_indices()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"全球指数异常：{e}") from e


@app.get("/api/global/stock")
def global_stock(symbol: str = Query(..., min_length=1, max_length=16)):
    """美股 / 港股个股聚合：行情 + 关键财务指标（东财域内源）。symbol 如 AAPL / BABA / 00700。"""
    try:
        data = gstock.us_hk_stock(symbol.strip())
        if not data:
            raise HTTPException(404, f"未找到美股/港股代码「{symbol}」")
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"美港股查询异常：{e}") from e


@app.get("/api/indices")
def indices():
    """A股大盘指数实时行情（上证/深证成指/创业板指/沪深300）。仅标准库。"""
    try:
        return {"data": astock.index_quote()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"指数行情异常：{e}") from e


@app.get("/api/quote")
def quote(codes: str = Query(..., description="逗号分隔的 6 位代码")):
    """实时行情：现价/涨跌/PE/PB/市值/换手/涨跌停。仅标准库，永远可用。"""
    lst = [c.strip() for c in codes.split(",") if c.strip()]
    if not lst or any(not c.isdigit() or len(c) != 6 for c in lst):
        raise HTTPException(400, "codes 必须是逗号分隔的 6 位数字")
    try:
        return {"data": astock.tencent_quote(lst)}
    except Exception as e:  # noqa: BLE001 — 边界统一兜底
        raise HTTPException(502, f"行情源异常：{e}") from e


import time as _time
_PCT_CACHE: dict = {}


@app.get("/api/valuation/percentile")
def valuation_percentile(code: str = Query(...)):
    """PE-TTM / PB 历史分位（近5年）。全站缓存 30 分钟/代码（历史序列日频、变化慢）。"""
    code = _validate(code)
    hit = _PCT_CACHE.get(code)
    if hit and _time.time() - hit[0] < 1800:
        return {"data": hit[1]}
    try:
        data = astock.valuation_percentile(code)
        _PCT_CACHE[code] = (_time.time(), data)
        return {"data": data}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"估值分位异常：{e}") from e


_ANN_CACHE: dict = {}


@app.get("/api/announcements")
def announcements(code: str = Query(...)):
    """个股近期公告（东财，仅 requests）。缓存 15 分钟/代码。"""
    code = _validate(code)
    hit = _ANN_CACHE.get(code)
    if hit and _time.time() - hit[0] < 900:
        return {"data": hit[1]}
    try:
        data = astock.announcements(code)
        _ANN_CACHE[code] = (_time.time(), data)
        return {"data": data}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"公告源异常：{e}") from e


_FIN_CACHE: dict = {}


@app.get("/api/financials")
def financials(code: str = Query(...)):
    """财务关键指标（同花顺财务摘要，最新报告期）。缓存 30 分钟/代码。"""
    code = _validate(code)
    hit = _FIN_CACHE.get(code)
    if hit and _time.time() - hit[0] < 1800:
        return {"data": hit[1]}
    try:
        data = astock.financials(code)
        _FIN_CACHE[code] = (_time.time(), data)
        return {"data": data}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"财务摘要异常：{e}") from e


@app.get("/api/valuation")
def valuation(code: str = Query(...)):
    """完整估值：行情 + 一致预期 + 前向PE/PEG/消化年数。"""
    code = _validate(code)
    try:
        return {"data": astock.full_valuation(code)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"估值计算异常：{e}") from e


@app.get("/api/reports")
def reports(code: str = Query(...), pages: int = Query(2, ge=1, le=5)):
    """个股研报列表（东财，含 PDF 链接）。仅需 requests。"""
    code = _validate(code)
    try:
        rows = astock.eastmoney_reports(code, max_pages=pages)
        for r in rows:
            r["pdfUrl"] = astock.pdf_url(r.get("infoCode", "")) if r.get("infoCode") else None
        return {"data": rows}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"研报源异常：{e}") from e


@app.get("/api/news")
def news(code: str = Query(...), limit: int = Query(20, ge=1, le=50)):
    """个股新闻（东财，需 akshare）。"""
    code = _validate(code)
    try:
        return {"data": astock.stock_news(code, limit=limit)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"新闻源异常：{e}") from e


@app.get("/api/info")
def info(code: str = Query(...)):
    """个股基本面：行业/股本/上市时间（需 akshare）。"""
    code = _validate(code)
    try:
        return {"data": astock.individual_info(code)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基本面源异常：{e}") from e


@app.get("/api/disclosure")
def disclosure(code: str = Query(...)):
    """巨潮公告列表（需 akshare）。"""
    code = _validate(code)
    try:
        return {"data": astock.disclosure(code)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"公告源异常：{e}") from e


@app.get("/api/kline")
def kline(code: str = Query(...), category: int = Query(4), offset: int = Query(60, ge=1, le=800)):
    """K线（需 mootdx）。category 4=日 5=周 6=月 11=60分钟。"""
    code = _validate(code)
    try:
        return {"data": astock.kline(code, category=category, offset=offset)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"K线源异常：{e}") from e


@app.get("/api/finance")
def finance(code: str = Query(...)):
    """季报财务快照（需 mootdx）。"""
    code = _validate(code)
    try:
        return {"data": astock.finance(code)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"财务源异常：{e}") from e


# ---------------------------------------------------------------------------
# 资金面 / 筹码 / 信号（东财数据中心，v3.3 并入）—— 均为「用户查的那只股」的公开数据。
# 东财有 1s 限流，这些多为日/季级静态数据，统一走 30 分钟缓存，进一步降低被封风险。
# ---------------------------------------------------------------------------

_DC_CACHE: dict = {}  # key=(endpoint, code) -> (ts, data)


def _cached(endpoint: str, code: str, ttl: int, fetch):
    key = (endpoint, code)
    hit = _DC_CACHE.get(key)
    if hit and _time.time() - hit[0] < ttl:
        return hit[1]
    data = fetch()
    _DC_CACHE[key] = (_time.time(), data)
    return data


@app.get("/api/margin")
def margin(code: str = Query(...)):
    """融资融券明细（东财，日级）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("margin", code, 1800, lambda: astock.margin_trading(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"融资融券异常：{e}") from e


@app.get("/api/block-trade")
def block_trade(code: str = Query(...)):
    """大宗交易（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("block", code, 1800, lambda: astock.block_trade(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"大宗交易异常：{e}") from e


@app.get("/api/holders")
def holders(code: str = Query(...)):
    """股东户数变化（东财，季度级）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("holders", code, 1800, lambda: astock.holder_num_change(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"股东户数异常：{e}") from e


@app.get("/api/dividend")
def dividend(code: str = Query(...)):
    """分红送转历史（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("dividend", code, 1800, lambda: astock.dividend_history(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"分红送转异常：{e}") from e


@app.get("/api/fund-flow")
def fund_flow(code: str = Query(...)):
    """个股资金流（东财 push2his，120 日主力净流入）。缓存 15 分钟。
    注：push2his 对部分大陆住宅 IP 有间歇风控，可能返回空（非代码问题）。"""
    code = _validate(code)
    try:
        return {"data": _cached("fundflow", code, 900, lambda: astock.stock_fund_flow_120d(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资金流异常：{e}") from e


@app.get("/api/dragon-tiger")
def dragon_tiger(code: str = Query(...)):
    """龙虎榜：该股近期上榜记录 + 买卖席位 + 机构净买（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("dt", code, 1800, lambda: astock.dragon_tiger_board(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"龙虎榜异常：{e}") from e


@app.get("/api/lockup")
def lockup(code: str = Query(...)):
    """限售解禁日历：历史解禁 + 未来 90 天待解禁（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("lockup", code, 1800, lambda: astock.lockup_expiry(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"解禁日历异常：{e}") from e


@app.get("/api/blocks")
def blocks(code: str = Query(...)):
    """个股所属板块/概念归属（东财 slist）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("blocks", code, 1800, lambda: astock.concept_blocks(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"板块归属异常：{e}") from e


@app.get("/api/hot-concepts")
def hot_concepts(code: str = Query(...)):
    """个股当下被市场归到哪些概念在炒（东财热门概念命中）。缓存 15 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("hotcon", code, 900, lambda: astock.hot_concepts(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"热门概念异常：{e}") from e


@app.get("/api/investor-qa")
def investor_qa(code: str = Query(...)):
    """互动易问答（巨潮）：投资者提问 + 公司回复。缓存 15 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("irm", code, 900, lambda: astock.investor_qa(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"互动易异常：{e}") from e


@app.get("/api/industry")
def industry(top: int = Query(20, ge=5, le=50)):
    """全行业涨跌幅排名（东财行业板块，板块级、零个股名单）。缓存 5 分钟。"""
    key = ("industry", str(top))
    hit = _DC_CACHE.get(key)
    if hit and _time.time() - hit[0] < 300:
        return {"data": hit[1]}
    try:
        data = astock.industry_comparison(top_n=top)
        _DC_CACHE[key] = (_time.time(), data)
        return {"data": data}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"行业排名异常：{e}") from e


# ---------------------------------------------------------------------------
# 研报 RAG（检索用户上传的研报内容，存本地 ~/.vibe-research/rag/）
# ---------------------------------------------------------------------------

@app.get("/api/rag/status")
def rag_status():
    """RAG 索引状态：模式 / 已索引块数 / 已索引研报数 / 缺失依赖。"""
    try:
        return {"data": rag.status()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"RAG 状态异常：{e}") from e


@app.post("/api/rag/reindex")
def rag_reindex():
    """重建全部研报索引（切换依赖 / 修复解析失败后用）。"""
    try:
        items = mr.list_reports()
        return {"data": rag.reindex_all(items)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"重建索引失败：{e}") from e


@app.post("/api/rag/index/{rid}")
def rag_index_one(rid: str):
    """手动索引单份研报（首次索引失败后重试）。"""
    try:
        for r in mr.list_reports():
            if r.get("id") == rid:
                fp = mr.REPORTS_DIR / f"{rid}{r.get('ext', '')}"
                if not fp.exists():
                    raise HTTPException(404, "研报文件不存在")
                return {"data": rag.index_report(rid, fp, r.get("name", rid), r.get("industry", ""))}
        raise HTTPException(404, "研报不存在")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"索引失败：{e}") from e


# ---------------------------------------------------------------------------
# 对话记忆（多轮对话持久化，存本地 ~/.vibe-research/conversations.json）
# ---------------------------------------------------------------------------

class ConvCreate(BaseModel):
    page: str
    context: str
    first_message: str


class ConvAppend(BaseModel):
    role: str
    content: str
    tools: list | None = None


@app.get("/api/conversations")
def conversations_list(page: str = Query(None)):
    """对话列表（元数据，不含 messages 正文）。按更新时间倒序。"""
    try:
        return {"data": conversations.list_conversations(page=page)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"对话列表异常：{e}") from e


@app.get("/api/conversations/{cid}")
def conversations_get(cid: str):
    """取完整对话含 messages。"""
    c = conversations.get_conversation(cid)
    if not c:
        raise HTTPException(404, "对话不存在")
    return {"data": c}


@app.post("/api/conversations")
def conversations_create(req: ConvCreate):
    """新建对话（首条用户消息一并存入）。"""
    try:
        return {"data": conversations.create_conversation(req.page, req.context, req.first_message)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"创建对话失败：{e}") from e


@app.post("/api/conversations/{cid}/messages")
def conversations_append(cid: str, req: ConvAppend):
    """追加一条消息（用户追问 / AI 回复）。"""
    c = conversations.append_message(cid, req.role, req.content, req.tools)
    if not c:
        raise HTTPException(404, "对话不存在")
    return {"data": c}


@app.delete("/api/conversations/{cid}")
def conversations_delete(cid: str):
    return {"data": {"ok": conversations.delete_conversation(cid)}}


@app.delete("/api/conversations")
def conversations_clear(page: str = Query(None)):
    """清空对话（可按 page 过滤）。"""
    try:
        return {"data": {"deleted": conversations.clear_conversations(page)}}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"清空对话失败：{e}") from e
