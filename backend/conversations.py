"""对话记忆 —— 多轮对话持久化，存本地 ~/.vibe-research/conversations.json。

与 portfolio.py 同模式：JSON 文件 + 原子写入 + 线程锁。
合规：对话内容只存本地，不上传、不进仓库。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

CACHE_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
CONV_FILE = os.path.join(CACHE_DIR, "conversations.json")
_LOCK = threading.Lock()
MAX_CONVERSATIONS = 200


def _load() -> dict:
    try:
        with open(CONV_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"conversations": []}


def _save(d: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CONV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CONV_FILE)


def list_conversations(page: str | None = None, limit: int = 50) -> list[dict]:
    with _LOCK:
        d = _load()
    convs = d.get("conversations", [])
    if page:
        convs = [c for c in convs if c.get("page") == page]
    convs = sorted(convs, key=lambda c: c.get("updated_at", 0), reverse=True)
    result = []
    for c in convs[:limit]:
        msgs = c.get("messages", [])
        last = msgs[-1] if msgs else {}
        result.append({
            "id": c["id"],
            "title": c.get("title", ""),
            "page": c.get("page", ""),
            "context_summary": c.get("context_summary", ""),
            "message_count": len(msgs),
            "last_preview": (last.get("content", "") or "")[:100],
            "created_at": c.get("created_at", 0),
            "updated_at": c.get("updated_at", 0),
        })
    return result


def get_conversation(cid: str) -> dict | None:
    with _LOCK:
        d = _load()
    for c in d.get("conversations", []):
        if c.get("id") == cid:
            return c
    return None


def create_conversation(page: str, context: str, first_message: str) -> dict:
    now = int(time.time() * 1000)
    title = first_message[:24] + ("…" if len(first_message) > 24 else "")
    conv = {
        "id": uuid.uuid4().hex,
        "title": title,
        "page": page,
        "context_summary": (context or "")[:200],
        "messages": [{"role": "user", "content": first_message, "ts": now}],
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        d = _load()
        d.setdefault("conversations", []).insert(0, conv)
        d["conversations"] = d["conversations"][:MAX_CONVERSATIONS]
        _save(d)
    return conv


def append_message(cid: str, role: str, content: str, tools: list | None = None) -> dict | None:
    now = int(time.time() * 1000)
    with _LOCK:
        d = _load()
        for c in d.get("conversations", []):
            if c.get("id") == cid:
                msg = {"role": role, "content": content, "ts": now}
                if tools:
                    msg["tools"] = tools
                c.setdefault("messages", []).append(msg)
                c["updated_at"] = now
                _save(d)
                return c
        return None


def delete_conversation(cid: str) -> bool:
    with _LOCK:
        d = _load()
        before = len(d.get("conversations", []))
        d["conversations"] = [c for c in d.get("conversations", []) if c.get("id") != cid]
        _save(d)
        return len(d["conversations"]) < before


def clear_conversations(page: str | None = None) -> int:
    with _LOCK:
        d = _load()
        before = len(d.get("conversations", []))
        if page:
            d["conversations"] = [c for c in d.get("conversations", []) if c.get("page") != page]
        else:
            d["conversations"] = []
        deleted = before - len(d["conversations"])
        _save(d)
        return deleted
