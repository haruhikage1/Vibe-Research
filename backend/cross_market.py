"""跨境信号映射 —— A 股 ↔ 美股/港股联动分析。

核心功能：
  1. 预定义联动映射（NVDA→光模块/GPU算力/HBM存储 等）
  2. get_active_signals() 检测美港股异动 → 匹配关联 A 股板块/个股
  3. 联动强度估算（基于历史方向同步率）
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import astock
import gstock

logger = logging.getLogger("vibe-research.cross-market")

BEIJING = timezone(timedelta(hours=8))
CACHE_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
CROSS_CACHE_FILE = os.path.join(CACHE_DIR, "cross_market_cache.json")

# 预定义联动映射：美股/港股标的 → 关联 A 股板块（东财行业板块名称）+ 代表个股
# 硬编码知识层，后续可通过历史数据验证/AI 补充。
CROSS_MAP: list[dict[str, Any]] = [
    {
        "symbol": "NVDA", "name": "英伟达",
        "market": "US", "lag": "t+1",
        "sectors": [
            {"name": "光模块CPO", "stocks": [{"code": "300308", "name": "中际旭创"}, {"code": "300394", "name": "天孚通信"}]},
            {"name": "GPU算力", "stocks": [{"code": "002916", "name": "沪电股份"}, {"code": "601138", "name": "工业富联"}]},
            {"name": "HBM存储", "stocks": [{"code": "002409", "name": "雅克科技"}, {"code": "300655", "name": "晶瑞电材"}]},
        ],
    },
    {
        "symbol": "TSLA", "name": "特斯拉",
        "market": "US", "lag": "t+1",
        "sectors": [
            {"name": "锂电", "stocks": [{"code": "300750", "name": "宁德时代"}, {"code": "002074", "name": "国轩高科"}]},
            {"name": "汽车零部件", "stocks": [{"code": "600741", "name": "华域汽车"}, {"code": "002126", "name": "银轮股份"}]},
            {"name": "自动驾驶", "stocks": [{"code": "002920", "name": "德赛西威"}, {"code": "601689", "name": "拓普集团"}]},
        ],
    },
    {
        "symbol": "AAPL", "name": "苹果",
        "market": "US", "lag": "t+1",
        "sectors": [
            {"name": "消费电子", "stocks": [{"code": "002475", "name": "立讯精密"}, {"code": "601138", "name": "工业富联"}]},
            {"name": "果链", "stocks": [{"code": "002475", "name": "立讯精密"}, {"code": "300433", "name": "蓝思科技"}]},
        ],
    },
    {
        "symbol": "MSFT", "name": "微软",
        "market": "US", "lag": "t+1",
        "sectors": [
            {"name": "AI算力", "stocks": [{"code": "688041", "name": "海光信息"}, {"code": "603019", "name": "中科曙光"}]},
            {"name": "软件服务", "stocks": [{"code": "688111", "name": "金山办公"}, {"code": "300033", "name": "同花顺"}]},
        ],
    },
    {
        "symbol": "AMZN", "name": "亚马逊",
        "market": "US", "lag": "t+1",
        "sectors": [
            {"name": "AI算力", "stocks": [{"code": "688041", "name": "海光信息"}, {"code": "300394", "name": "天孚通信"}]},
            {"name": "云计算", "stocks": [{"code": "600588", "name": "用友网络"}, {"code": "300454", "name": "深信服"}]},
        ],
    },
    {
        "symbol": "00700", "name": "腾讯控股",
        "market": "HK", "lag": "同天",
        "sectors": [
            {"name": "互联网", "stocks": [{"code": "300059", "name": "东方财富"}, {"code": "002517", "name": "恺英网络"}]},
            {"name": "游戏", "stocks": [{"code": "002555", "name": "三七互娱"}, {"code": "300315", "name": "掌趣科技"}]},
        ],
    },
    {
        "symbol": "DJIA", "name": "道琼斯",
        "market": "index", "lag": "t+1",
        "sectors": [{"name": "证券", "stocks": [{"code": "600030", "name": "中信证券"}, {"code": "601688", "name": "华泰证券"}]}],
    },
    {
        "symbol": "NDX", "name": "纳斯达克",
        "market": "index", "lag": "t+1",
        "sectors": [
            {"name": "AI算力", "stocks": [{"code": "688041", "name": "海光信息"}, {"code": "300308", "name": "中际旭创"}]},
        ],
    },
]

SIGNAL_THRESHOLD = 2.0  # 涨跌幅超过此值视为异动


def _resolve_quote(symbol: str, market: str) -> dict | None:
    """取美港股/指数行情。返回 {price, change_pct, prev_close} 或 None。"""
    if market == "index":
        # 全球指数：从 market.py 获取
        import market
        try:
            indices = market.get_global_indices()
            key_map = {"DJIA": "dji", "NDX": "ndx", "SPX": "spx", "HSI": "hsi", "HSTECH": "hstech"}
            key = key_map.get(symbol)
            for idx in indices:
                if idx.get("key") == key:
                    return {"price": idx.get("price"), "change_pct": idx.get("change_pct")}
        except Exception:
            pass
        return None

    try:
        data = gstock.us_hk_stock(symbol)
        if not data or not data.get("quote"):
            return None
        q = data["quote"]
        return {
            "price": q.get("price"),
            "change_pct": q.get("change_pct"),
            "prev_close": q.get("prev_close"),
        }
    except Exception:
        return None


def _resolve_sector_pct(sector_name: str) -> float | None:
    """从行业排名中查某板块的实时涨跌幅。"""
    try:
        comp = astock.industry_comparison(top_n=100)
        all_sectors = comp.get("top", []) + comp.get("bottom", [])
        for s in all_sectors:
            if s.get("name") == sector_name:
                return s.get("change_pct")
    except Exception:
        pass
    return None


def _sync_rate_score(days: int = 30) -> dict[str, float]:
    """读取本地缓存的联动方向同步率（涨跌方向一致的天数比例）。
    纯占位——后续可扩展为真实历史相关系数计算。
    """
    try:
        with open(CROSS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("sync_rates", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_active_signals() -> dict:
    """检测当前活跃的跨境联动信号。

    遍历 CROSS_MAP，查每个美港股标的的实时行情，
    若 |涨跌幅| > SIGNAL_THRESHOLD 则产出信号。
    """
    sync_rates = _sync_rate_score()
    signals: list[dict] = []
    updated = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")

    for item in CROSS_MAP:
        symbol = item["symbol"]
        market = item.get("market", "US")
        quote = _resolve_quote(symbol, market)
        if not quote or quote.get("change_pct") is None:
            continue

        change_pct = quote["change_pct"]
        is_active = abs(change_pct) >= SIGNAL_THRESHOLD
        direction = "up" if change_pct > 0 else "down"

        # 取关联板块实时涨跌幅
        related_sectors = []
        for sec in item.get("sectors", []):
            sec_name = sec["name"]
            sec_pct = _resolve_sector_pct(sec_name)
            stock_codes = [s["code"] for s in sec.get("stocks", [])]
            stock_quotes = {}
            if stock_codes:
                try:
                    stock_quotes = astock.tencent_quote(stock_codes)
                except Exception:
                    pass

            related_sectors.append({
                "name": sec_name,
                "change_pct": sec_pct,
                "stocks": [
                    {
                        "code": s["code"],
                        "name": s["name"],
                        "price": stock_quotes.get(s["code"], {}).get("price"),
                        "change_pct": stock_quotes.get(s["code"], {}).get("change_pct"),
                    }
                    for s in sec.get("stocks", [])
                ],
            })

        signal = {
            "symbol": symbol,
            "name": item["name"],
            "market": market,
            "price": quote.get("price"),
            "change_pct": change_pct,
            "direction": direction,
            "is_active": is_active,
            "lag": item.get("lag", "t+1"),
            "sync_rate": sync_rates.get(symbol),
            "sectors": related_sectors,
        }
        signals.append(signal)

    # 活跃信号排前
    signals.sort(key=lambda x: (not x["is_active"], -abs(x["change_pct"])))

    return {
        "signals": signals,
        "total": len(signals),
        "active_count": sum(1 for s in signals if s["is_active"]),
        "updated": updated,
    }


def get_correlation_summary() -> list[dict]:
    """联动强度摘要：返回每对标的一级联动板块及参考强度。"""
    sync_rates = _sync_rate_score()
    result = []
    for item in CROSS_MAP:
        for sec in item.get("sectors", []):
            result.append({
                "us_symbol": item["symbol"],
                "us_name": item["name"],
                "sector": sec["name"],
                "lag": item.get("lag", "t+1"),
                "sync_rate": sync_rates.get(f"{item['symbol']}:{sec['name']}"),
            })
    return result
