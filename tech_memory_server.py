#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TechMemory MCP Server —— 机的独立工作室
一个独立于 OmbreBrain 的技术记忆库：按项目分类、关键词检索、真删除、进度追加。

为什么需要它：
    OmbreBrain 天生什么都记——情感、日常、技术、踩坑全混在一个库里，
    还会浮现、衰减、软删除（删了原文还在，搜索时会诈尸）。
    技术类的东西过期了删不掉、搜索时老蹦出来干扰判断、情感记忆被一堆
    API 参数稀释。这个项目就是给技术记忆一个独立的、能真删、按项目分类的家。

存储：
    SQLite 单文件，零依赖，量小，不占内存。

配置（全部通过环境变量，可选）：
    TECH_MEMORY_TOKEN   鉴权 token，不设置则不鉴权（仅本地调试用）
    TECH_MEMORY_PORT    监听端口，默认 8899
    TECH_MEMORY_DB      数据库文件路径，默认脚本同目录 tech_memory.db

向量检索（可选 feature，默认关闭）：
    不装 fastembed 就静默退回关键词模式，开箱即用零依赖。
    想开语义检索的人，自行 pip install fastembed，并可选设置：
      TECH_MEMORY_EMBED_MODEL      模型名，默认 BAAI/bge-small-zh-v1.5
      TECH_MEMORY_EMBED_THRESHOLD  相似度阈值 0~1，低于不返回，默认 0.3

启动：
    TECH_MEMORY_TOKEN=xxx python3 tech_memory_server.py
    或配合 systemd（见 tech-memory.service）。
"""

import sqlite3
import os
import json
from datetime import datetime

from mcp.server.fastmcp import FastMCP

# 尝试从 .env 文件加载环境变量（可选依赖 python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tech_memory.env"))
except ImportError:
    pass  # 没装 python-dotenv 也没关系，直接用系统环境变量

DB_PATH = os.environ.get(
    "TECH_MEMORY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tech_memory.db"),
)
AUTH_TOKEN = os.environ.get("TECH_MEMORY_TOKEN", "")
PORT = int(os.environ.get("TECH_MEMORY_PORT", "8899"))
# 搜索默认返回条数上限：宁少勿滥，省 token。可被 search 的 limit 参数覆盖。
DEFAULT_LIMIT = int(os.environ.get("TECH_MEMORY_SEARCH_LIMIT", "5"))

# ---- 向量检索（可选 feature）----
EMBED_MODEL = os.environ.get("TECH_MEMORY_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
EMBED_THRESHOLD = float(os.environ.get("TECH_MEMORY_EMBED_THRESHOLD", "0.3"))

_embed_model = None  # 惰性加载，避免启动就吃内存


def _get_embed_model():
    """惰性加载 fastembed 模型；没装 fastembed 返回 None（降级关键词模式）。"""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding(model_name=EMBED_MODEL)
        return _embed_model
    except Exception:
        _embed_model = None
        return None


def _embed(text):
    """把一段文本转成向量；返回 list[float] 或 None（降级）。"""
    model = _get_embed_model()
    if model is None:
        return None
    try:
        vecs = list(model.embed([text]))
        return [float(x) for x in vecs[0]]
    except Exception:
        return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# 注意：host/port 要传给 FastMCP 构造函数，而不是 uvicorn.run()。
# 原因见 README「踩坑记录」：uvicorn 0.5x 会强制校验 Host 头，
# 用 uvicorn.run() 裸跑会报 421 Misdirected Request；FastMCP 自己处理 Host，不校验。
mcp = FastMCP("tech-memory", host="0.0.0.0", port=PORT)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            desc TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    # 兼容老库：加 embedding 列（存 JSON 向量文本，简单直观）
    try:
        cur.execute("ALTER TABLE entries ADD COLUMN embedding TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@mcp.tool()
def project_create(name: str, desc: str = "") -> dict:
    """创建一个技术项目（分类）。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO projects (name, desc, created_at) VALUES (?, ?, ?)",
                    (name, desc, now_str()))
        conn.commit()
        return {"ok": True, "project_id": cur.lastrowid, "msg": f"项目「{name}」已创建"}
    except sqlite3.IntegrityError:
        return {"ok": False, "msg": f"项目「{name}」已存在"}
    finally:
        conn.close()


@mcp.tool()
def save(project: str, content: str, tags: str = "") -> dict:
    """往某个项目追加一条进度/结论。装了 fastembed 会自动算向量。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE name = ?", (project,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "msg": f"项目「{project}」不存在，请先 project_create"}
    pid = row["id"]
    emb = _embed(content + " " + tags)
    emb_json = json.dumps(emb) if emb is not None else ""
    cur.execute("INSERT INTO entries (project_id, content, tags, created_at, embedding) VALUES (?, ?, ?, ?, ?)",
                (pid, content, tags, now_str(), emb_json))
    conn.commit()
    conn.close()
    return {"ok": True, "entry_id": cur.lastrowid, "msg": "已保存"}


@mcp.tool()
def search(query: str, limit: int = 0) -> dict:
    """搜索。装了 fastembed 走「向量+关键词」混合召回，没装退回纯关键词。limit 控制返回条数，0 用默认上限。"""
    conn = get_conn()
    cur = conn.cursor()
    n = limit if limit and limit > 0 else DEFAULT_LIMIT

    # 1) 关键词召回（永远生效）
    like = f"%{query}%"
    cur.execute("""
        SELECT e.id, p.name AS project, e.content, e.tags, e.created_at, e.embedding
        FROM entries e JOIN projects p ON e.project_id = p.id
        WHERE p.name LIKE ? OR e.content LIKE ? OR e.tags LIKE ?
        ORDER BY e.created_at DESC
    """, (like, like, like))
    kw_rows = cur.fetchall()

    # 2) 向量召回（装了 fastembed 才走）
    qvec = _embed(query)
    scored = {}  # id -> (score, row)
    for r in kw_rows:
        scored[r["id"]] = (1.0, r)  # 关键词命中给基础分 1.0
    if qvec is not None:
        cur.execute("SELECT e.id, p.name AS project, e.content, e.tags, e.created_at, e.embedding "
                    "FROM entries e JOIN projects p ON e.project_id = p.id")
        all_rows = cur.fetchall()
        for r in all_rows:
            if not r["embedding"]:
                continue
            try:
                v = json.loads(r["embedding"])
            except Exception:
                continue
            sim = _cosine(qvec, v)
            if sim >= EMBED_THRESHOLD:
                if r["id"] in scored:
                    scored[r["id"]] = (max(scored[r["id"]][0], 1.0 + sim), r)
                else:
                    scored[r["id"]] = (sim, r)

    # 3) 排序 + 截断 top N
    ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)[:n]
    conn.close()
    results = [{"id": r["id"], "project": r["project"], "content": r["content"],
                "tags": r["tags"], "created_at": r["created_at"],
                "score": round(s, 4)} for s, r in ranked]
    return {"ok": True, "count": len(results), "limit": n,
            "vector": qvec is not None, "results": results}


@mcp.tool()
def rebuild_index() -> dict:
    """给老数据一次性补向量。没装 fastembed 会返回提示。"""
    model = _get_embed_model()
    if model is None:
        return {"ok": False, "msg": "未安装 fastembed，无法重建向量索引（当前为关键词模式）"}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, content, tags FROM entries")
    rows = cur.fetchall()
    done = 0
    for r in rows:
        emb = _embed(r["content"] + " " + r["tags"])
        if emb is not None:
            cur.execute("UPDATE entries SET embedding = ? WHERE id = ?",
                        (json.dumps(emb), r["id"]))
            done += 1
    conn.commit()
    conn.close()
    return {"ok": True, "msg": f"已为 {done}/{len(rows)} 条重建向量"}


@mcp.tool()
def project_log(project: str) -> dict:
    """查看某个项目的完整时间线。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, desc, created_at FROM projects WHERE name = ?", (project,))
    p = cur.fetchone()
    if p is None:
        conn.close()
        return {"ok": False, "msg": f"项目「{project}」不存在"}
    cur.execute("SELECT id, content, tags, created_at FROM entries WHERE project_id = ? ORDER BY created_at ASC",
                (p["id"],))
    rows = cur.fetchall()
    conn.close()
    entries = [{"id": r["id"], "content": r["content"], "tags": r["tags"],
                "created_at": r["created_at"]} for r in rows]
    return {"ok": True, "project": project, "desc": p["desc"],
            "created_at": p["created_at"], "entries": entries}


@mcp.tool()
def delete(entry_id: int) -> dict:
    """真删除某条技术记忆（不可恢复）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        return {"ok": True, "msg": f"条目 {entry_id} 已真删"}
    return {"ok": False, "msg": f"条目 {entry_id} 不存在"}


@mcp.tool()
def project_delete(name: str) -> dict:
    """删除整个技术项目及其所有条目（不可恢复）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE name = ?", (name,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "msg": f"项目「{name}」不存在"}
    pid = row["id"]
    cur.execute("DELETE FROM entries WHERE project_id = ?", (pid,))
    cur.execute("DELETE FROM projects WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True, "msg": f"项目「{name}」及其所有条目已真删"}


@mcp.tool()
def list_projects() -> dict:
    """列出所有技术项目。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name, p.desc, p.created_at,
               (SELECT COUNT(*) FROM entries e WHERE e.project_id = p.id) AS cnt
        FROM projects p ORDER BY p.created_at ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return {"ok": True, "projects": [dict(r) for r in rows]}


if __name__ == "__main__":
    import uvicorn
    from starlette.responses import JSONResponse

    init_db()
    app = mcp.streamable_http_app()

    # Bearer 鉴权中间件
    async def auth_middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        if AUTH_TOKEN and auth != f"Bearer {AUTH_TOKEN}":
            response = JSONResponse({"error": "unauthorized"}, status_code=403)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)

    config = uvicorn.Config(auth_middleware, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    import asyncio
    asyncio.run(server.serve())