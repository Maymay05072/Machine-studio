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

启动：
    TECH_MEMORY_TOKEN=xxx python3 tech_memory_server.py
    或配合 systemd（见 tech-memory.service）。
"""

import sqlite3
import os
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
    """往某个项目追加一条进度/结论。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE name = ?", (project,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "msg": f"项目「{project}」不存在，请先 project_create"}
    pid = row["id"]
    cur.execute("INSERT INTO entries (project_id, content, tags, created_at) VALUES (?, ?, ?, ?)",
                (pid, content, tags, now_str()))
    conn.commit()
    conn.close()
    return {"ok": True, "entry_id": cur.lastrowid, "msg": "已保存"}


@mcp.tool()
def search(query: str) -> dict:
    """按关键词搜索项目名、正文、标签。"""
    conn = get_conn()
    cur = conn.cursor()
    like = f"%{query}%"
    cur.execute("""
        SELECT e.id, p.name AS project, e.content, e.tags, e.created_at
        FROM entries e JOIN projects p ON e.project_id = p.id
        WHERE p.name LIKE ? OR e.content LIKE ? OR e.tags LIKE ?
        ORDER BY e.created_at DESC
    """, (like, like, like))
    rows = cur.fetchall()
    conn.close()
    results = [{"id": r["id"], "project": r["project"], "content": r["content"],
                "tags": r["tags"], "created_at": r["created_at"]} for r in rows]
    return {"ok": True, "count": len(results), "results": results}


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
