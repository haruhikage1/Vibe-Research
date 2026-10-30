"""投资复盘录 —— 每日收盘后自动聚合市场、持仓、资讯、AI 分析。

纯组装型模块：所有数据来自已有模块（market / portfolio / newsradar / astock），
本模块只负责聚合、存储、检索。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import market
import newsradar
import portfolio as pf

logger = logging.getLogger("vibe-research.daily-brief")

BEIJING = timezone(timedelta(hours=8))
CACHE_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
LOGS_DIR = os.path.join(CACHE_DIR, "daily-logs")


def _ensure_dir() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)


def _today() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")


def _yi(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1e8:.2f}亿"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def generate_brief(watch_codes: list[str] | None = None) -> dict:
    """聚合当天所有数据，生成结构化投资日志。

    watch_codes: 用户的自选股代码列表（前端传，因存 localStorage）。
    返回完整的 log dict。
    """
    # 1. 大盘指数
    try:
        import astock as _astock
        indices = _astock.index_quote()
    except Exception:
        indices = []

    # 2. 全球指数
    try:
        global_idx = market.get_global_indices()
    except Exception:
        global_idx = []

    # 3. 市场情绪 + 板块资金
    try:
        ov = market.get_overview()
        sentiment = ov.get("sentiment", {})
        sectors = ov.get("sectors", [])
    except Exception:
        sentiment = {}
        sectors = []

    # 4. 短线情绪
    try:
        emotion = market.get_short_term_emotion()
    except Exception:
        emotion = {}

    # 5. 成交额 Top
    try:
        turnover = market.get_turnover_top()
    except Exception:
        turnover = {}

    # 6. 持仓盈亏
    try:
        portfolio = pf.get_portfolio()
    except Exception:
        portfolio = {"holdings": [], "totals": {}, "closed": [], "realized_pnl": 0}

    # 7. 自选股行情（前端传代码列表）
    watch_quotes = {}
    if watch_codes:
        try:
            import astock as _astock
            watch_quotes = _astock.tencent_quote(watch_codes)
        except Exception:
            pass

    # 8. 相关资讯（取当天前 3 条/赛道）
    try:
        radar = newsradar.get_radar(force=False)
        radar_items = []
        for ind in (radar.get("industries") or []):
            for item in (ind.get("items") or [])[:3]:
                radar_items.append({
                    "industry": ind.get("name", ""),
                    "title": item.get("title", ""),
                    "source": item.get("source", ""),
                    "time": item.get("time", ""),
                })
    except Exception:
        radar_items = []

    log = {
        "date": _today(),
        "generated_at": _now(),
        "indices": [
            {"name": i.get("name", ""), "price": i.get("price", 0), "change_pct": i.get("change_pct", 0)}
            for i in indices
        ],
        "global_indices": [
            {"name": g.get("name", ""), "region": g.get("region", ""),
             "price": g.get("price"), "change_pct": g.get("change_pct")}
            for g in global_idx
        ],
        "sentiment": {
            "up": sentiment.get("up", 0),
            "down": sentiment.get("down", 0),
            "zt": sentiment.get("zt", 0),
            "dt": sentiment.get("dt", 0),
            "breadth": sentiment.get("breadth", ""),
            "speculation": sentiment.get("speculation", ""),
        } if sentiment else {},
        "emotion": {
            "zt_count": emotion.get("zt_count", 0),
            "dt_count": emotion.get("dt_count", 0),
            "max_boards": emotion.get("max_boards", 0),
            "lianban_count": emotion.get("lianban_count", 0),
            "seal_rate": emotion.get("seal_rate"),
            "break_rate": emotion.get("break_rate"),
        } if emotion else {},
        "sectors_top": [
            {"name": s.get("name", ""), "pct": s.get("pct", 0), "net": s.get("net", 0)}
            for s in (sectors or [])[:8]
        ],
        "turnover_top": [
            {"name": s.get("name", ""), "pct": s.get("pct"), "amount": s.get("amount")}
            for s in ((turnover or {}).get("stocks") or [])[:10]
        ],
        "portfolio": {
            "holdings": [
                {"code": h.get("code", ""), "name": h.get("name", ""),
                 "pnl": h.get("pnl", 0), "pnl_pct": h.get("pnl_pct", 0)}
                for h in portfolio.get("holdings", [])
            ],
            "totals": portfolio.get("totals", {}),
            "realized_pnl": portfolio.get("realized_pnl", 0),
        },
        "watchlist": [
            {"code": c, "name": watch_quotes.get(c, {}).get("name", c),
             "price": watch_quotes.get(c, {}).get("price", 0),
             "change_pct": watch_quotes.get(c, {}).get("change_pct", 0)}
            for c in (watch_codes or [])
        ],
        "news_highlights": radar_items[:15],
        "ai_commentary": "",
        "ai_commentary_status": "pending",  # pending | done | error
    }

    # 存储
    _ensure_dir()
    fp = os.path.join(LOGS_DIR, f"{log['date']}.json")
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存复盘录失败：%s", e)

    return log


def generate_ai_commentary(log: dict, cfg: dict) -> str:
    """调用 LLM 生成 AI 简评，更新 log 并持久化。"""
    from chat import run_chat

    # 组装 prompt
    lines = [f"今天是 {log['date']}。以下是今天的市场数据：\n"]
    if log["indices"]:
        lines.append("【大盘指数】")
        for i in log["indices"]:
            lines.append(f"  {i['name']} {i['price']}（{_pct(i['change_pct'])}）")
    if log["global_indices"]:
        lines.append("【全球市场】")
        for g in log["global_indices"]:
            lines.append(f"  {g['name']} {g['price']}（{_pct(g['change_pct'])}）")
    if log["sentiment"]:
        s = log["sentiment"]
        lines.append(f"【市场情绪】上涨 {s['up']} / 下跌 {s['down']} / 涨停 {s['zt']} / 跌停 {s['dt']}，大盘宽度「{s['breadth']}」，题材投机「{s['speculation']}」")
    if log["emotion"]:
        e = log["emotion"]
        seal = f"{e['seal_rate']*100:.1f}%" if e.get("seal_rate") is not None else "—"
        brk = f"{e['break_rate']*100:.1f}%" if e.get("break_rate") is not None else "—"
        lines.append(f"【短线情绪】涨停 {e['zt_count']} / 跌停 {e['dt_count']}，最高连板 {e['max_boards']} 板，封板率 {seal}，炸板率 {brk}")
    if log["portfolio"]["holdings"]:
        t = log["portfolio"]["totals"]
        lines.append(f"【持仓】总市值 {_yi(t.get('market_value', 0))}，总盈亏 {_yi(t.get('pnl', 0))}（{_pct(t.get('pnl_pct', 0))}）")
        for h in log["portfolio"]["holdings"]:
            lines.append(f"  {h['name']}({h['code']}) 盈亏 {_yi(h['pnl'])}（{_pct(h['pnl_pct'])}）")
    if log["watchlist"]:
        lines.append("【自选股动态】")
        for w in log["watchlist"]:
            lines.append(f"  {w['name']}({w['code']}) {w['price']}（{_pct(w['change_pct'])}）")
    if log["sectors_top"]:
        lines.append("【板块资金 Top】")
        for s in log["sectors_top"]:
            lines.append(f"  {s['name']} {_pct(s['pct'])} 净流入 {s['net']} 亿")
    if log["news_highlights"]:
        lines.append("【今日重要资讯】")
        for n in log["news_highlights"][:8]:
            lines.append(f"  [{n['industry']}] {n['title']}")

    lines.append("")
    lines.append("请用中文写一段 3-5 句的当日投资日志总结，涵盖大盘表现、你的持仓/自选动态、市场情绪和值得关注的板块。语言简洁，只做客观陈述，不预测涨跌、不推荐标的。")

    prompt_text = "\n".join(lines)

    try:
        result = run_chat(cfg, [{"role": "user", "content": prompt_text}], context="每日复盘数据聚合")
        commentary = result.get("content", "")
    except Exception as e:
        logger.error("AI 简评生成失败：%s", e)
        commentary = ""
        log["ai_commentary_status"] = "error"
    else:
        log["ai_commentary"] = commentary
        log["ai_commentary_status"] = "done"

    # 持久化
    _ensure_dir()
    fp = os.path.join(LOGS_DIR, f"{log['date']}.json")
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("保存 AI 简评失败：%s", e)

    return commentary


def get_brief(date: str | None = None) -> dict | None:
    """取指定日期的复盘录。date 格式 YYYY-MM-DD，默认今天。"""
    target = date or _today()
    fp = os.path.join(LOGS_DIR, f"{target}.json")
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def list_briefs() -> list[dict]:
    """列出所有复盘录日期（倒序）。"""
    _ensure_dir()
    try:
        files = sorted(
            (f for f in os.listdir(LOGS_DIR) if f.endswith(".json")),
            reverse=True,
        )
        result = []
        for f in files:
            fp = os.path.join(LOGS_DIR, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    data = json.load(fh)
                    result.append({
                        "date": data.get("date", f[:-5]),
                        "generated_at": data.get("generated_at", ""),
                        "has_commentary": bool(data.get("ai_commentary")),
                        "indices_count": len(data.get("indices", [])),
                        "holdings_count": len(data.get("portfolio", {}).get("holdings", [])),
                    })
            except Exception:
                result.append({"date": f[:-5], "generated_at": "", "has_commentary": False})
        return result
    except OSError:
        return []
