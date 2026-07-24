"""研报 RAG —— 用户上传的研报解析、分块、TF-IDF 检索。

惰性依赖：pypdf / python-docx / jieba 缺失时降级（跳过该格式 / 退化为字符 bigram）。
存储：SQLite（stdlib），~/.vibe-research/rag/rag.db。
合规：只做客观文本检索，返回原文片段，不含建议。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

_DATA_DIR = Path(os.environ.get("VR_DATA_DIR") or Path.home() / ".vibe-research")
RAG_DIR = _DATA_DIR / "rag"
DB_PATH = RAG_DIR / "rag.db"
_LOCK = threading.Lock()

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
MAX_CHUNKS_PER_REPORT = 200


class DependencyMissing(RuntimeError):
    """惰性依赖未安装时抛出，前端据此提示 pip install。"""


def _ensure_db() -> sqlite3.Connection:
    RAG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            report_id TEXT,
            report_name TEXT,
            industry TEXT,
            chunk_index INTEGER,
            text TEXT,
            ts INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report ON chunks(report_id)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 文档解析（惰性导入，缺失时跳过该格式）
# ---------------------------------------------------------------------------

def _extract_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(file_path)
        if ext in (".docx",):
            return _extract_docx(file_path)
        if ext in (".txt", ".md", ".markdown"):
            return file_path.read_text("utf-8", errors="replace")
        if ext == ".csv":
            return _extract_csv(file_path)
        if ext in (".xlsx", ".xls"):
            return _extract_xlsx(file_path)
    except DependencyMissing:
        raise
    except Exception:
        return ""
    return ""


def _extract_pdf(path: Path) -> str:
    try:
        import pypdf
    except ImportError:
        raise DependencyMissing("pypdf（pip install pypdf）")
    reader = pypdf.PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(parts)


def _extract_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        raise DependencyMissing("python-docx（pip install python-docx）")
    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_csv(path: Path) -> str:
    import csv
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    return "\n".join(",".join(r) for r in rows)


def _extract_xlsx(path: Path) -> str:
    try:
        import pandas as pd
    except ImportError:
        raise DependencyMissing("pandas（pip install pandas）")
    df = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    parts = []
    for name, sheet in df.items():
        parts.append(f"[{name}]\n{sheet.to_string(index=False)}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 分块（段落感知，避免切断语义）
# ---------------------------------------------------------------------------

def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}" if buf else para
            continue
        if buf:
            chunks.append(buf)
        if len(para) <= size:
            buf = para
        else:
            for i in range(0, len(para), size - overlap):
                chunks.append(para[i:i + size])
                if i + size >= len(para):
                    break
            buf = ""
    if buf:
        chunks.append(buf)
    if len(chunks) > MAX_CHUNKS_PER_REPORT:
        chunks = chunks[:MAX_CHUNKS_PER_REPORT]
    return chunks


# ---------------------------------------------------------------------------
# TF-IDF 检索
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    try:
        import jieba
        return [w for w in jieba.lcut(text) if len(w.strip()) >= 2]
    except ImportError:
        return _bigrams(text)


def _bigrams(text: str) -> list[str]:
    chars = [c for c in text if c.strip() and not c.isspace()]
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


def _tfidf_search(query: str, chunks: list[dict], top_k: int, industry: str | None) -> list[dict]:
    if not chunks:
        return []
    if industry:
        chunks = [c for c in chunks if c.get("industry") == industry]
    if not chunks:
        return []
    df: dict[str, int] = defaultdict(int)
    tokenized: list[tuple[dict, Counter]] = []
    for c in chunks:
        tokens = _tokenize(c["text"])
        tf = Counter(tokens)
        for term in tf:
            df[term] += 1
        tokenized.append((c, tf))
    total = len(tokenized)
    q_tokens = _tokenize(query)
    q_tf = Counter(q_tokens)
    scores: list[tuple[float, dict]] = []
    for c, tf in tokenized:
        score = 0.0
        for term, q_count in q_tf.items():
            if term in tf and df[term] > 0:
                idf = math.log(total / df[term])
                score += q_count * tf[term] * idf * idf
        if score > 0:
            norm = max(sum(tf.values()), 1)
            scores.append((score / norm, c))
    scores.sort(key=lambda x: -x[0])
    return [
        {
            "report_name": c["report_name"],
            "industry": c.get("industry", ""),
            "text": c["text"],
            "score": round(s, 4),
            "chunk_index": c.get("chunk_index", 0),
        }
        for s, c in scores[:top_k]
    ]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def index_report(report_id: str, file_path: Path, name: str, industry: str) -> dict:
    try:
        text = _extract_text(Path(file_path))
    except DependencyMissing as e:
        return {"report_id": report_id, "chunks": 0, "skipped": str(e)}
    if not text.strip():
        return {"report_id": report_id, "chunks": 0, "skipped": "无法提取文本（可能是扫描件/图片）"}
    chunks = _chunk(text)
    if not chunks:
        return {"report_id": report_id, "chunks": 0, "skipped": "分块为空"}
    now = int(time.time() * 1000)
    rows = [
        (uuid.uuid4().hex, report_id, name, industry, i, chunk, now)
        for i, chunk in enumerate(chunks)
    ]
    with _LOCK:
        conn = _ensure_db()
        conn.execute("DELETE FROM chunks WHERE report_id = ?", (report_id,))
        conn.executemany(
            "INSERT INTO chunks (id, report_id, report_name, industry, chunk_index, text, ts) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
    return {"report_id": report_id, "chunks": len(chunks), "skipped": None}


def remove_report(report_id: str) -> bool:
    with _LOCK:
        conn = _ensure_db()
        cur = conn.execute("DELETE FROM chunks WHERE report_id = ?", (report_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
    return deleted


def search(query: str, top_k: int = 5, industry: str | None = None) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    with _LOCK:
        conn = _ensure_db()
        rows = conn.execute(
            "SELECT report_name, industry, text, chunk_index FROM chunks"
        ).fetchall()
        conn.close()
    if not rows:
        return []
    chunks = [
        {"report_name": r[0], "industry": r[1], "text": r[2], "chunk_index": r[3]}
        for r in rows
    ]
    return _tfidf_search(q, chunks, top_k, industry)


def reindex_all(report_items: list[dict]) -> dict:
    results = []
    for item in report_items:
        rid = item.get("id", "")
        name = item.get("name", "")
        industry = item.get("industry", "")
        ext = item.get("ext", "")
        fp = RAG_DIR.parent / "myreports" / f"{rid}{ext}"
        if not fp.exists():
            continue
        results.append(index_report(rid, fp, name, industry))
    return {"reindexed": len(results), "details": results}


def status() -> dict:
    deps_missing = []
    for mod, pkg in [("pypdf", "pypdf"), ("docx", "python-docx"), ("jieba", "jieba")]:
        try:
            __import__(mod)
        except ImportError:
            deps_missing.append(pkg)
    with _LOCK:
        conn = _ensure_db()
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        report_count = conn.execute(
            "SELECT COUNT(DISTINCT report_id) FROM chunks"
        ).fetchone()[0]
        conn.close()
    return {
        "mode": "tfidf",
        "indexed_chunks": chunk_count,
        "indexed_reports": report_count,
        "deps_missing": deps_missing,
    }
