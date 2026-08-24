"""HTTP dashboard and JSON API for Tech Memory.

The web shell is public so a browser can display the login screen. Database
routes require a short-lived, HttpOnly session cookie. MCP keeps its existing
Bearer-token authentication.
"""

import asyncio
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

SESSION_COOKIE = "tech_memory_session"
SESSION_TTL = int(os.environ.get("TECH_MEMORY_SESSION_HOURS", "12")) * 3600
COOKIE_SECURE = os.environ.get("TECH_MEMORY_COOKIE_SECURE", "0") == "1"
WEB_DIR = Path(__file__).with_name("web")


def build_app(*, mcp_app, mcp_lifespan, auth_token, get_conn, project_create, save, update,
              search, project_log, delete, project_delete, list_projects):
    sessions = {}
    failed_logins = defaultdict(deque)

    def json_error(message, status=400):
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    def cleanup_sessions():
        now = time.time()
        for sid, expires in list(sessions.items()):
            if expires <= now:
                sessions.pop(sid, None)

    def has_session(request):
        cleanup_sessions()
        sid = request.cookies.get(SESSION_COOKIE, "")
        return bool(sid and sessions.get(sid, 0) > time.time())

    async def payload(request):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def home(_request):
        return FileResponse(WEB_DIR / "index.html")

    async def api_session(request):
        return JSONResponse({"ok": True, "authenticated": has_session(request),
                             "password_configured": bool(auth_token)})

    async def api_login(request):
        client = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = failed_logins[client]
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if len(attempts) >= 8:
            return json_error("尝试次数过多，请五分钟后再试", 429)
        data = await payload(request)
        supplied = str(data.get("password", ""))
        if auth_token and not hmac.compare_digest(supplied, auth_token):
            attempts.append(now)
            await asyncio.sleep(0.35)
            return json_error("访问密码不正确", 403)
        attempts.clear()
        sid = secrets.token_urlsafe(32)
        sessions[sid] = now + SESSION_TTL
        response = JSONResponse({"ok": True})
        response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL,
                            httponly=True, secure=COOKIE_SECURE,
                            samesite="strict", path="/")
        return response

    async def api_logout(request):
        sid = request.cookies.get(SESSION_COOKIE, "")
        sessions.pop(sid, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def api_projects(_request):
        return JSONResponse(list_projects())

    async def api_project_create(request):
        data = await payload(request)
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 120:
            return json_error("项目名称须为 1–120 个字符")
        return JSONResponse(project_create(name, str(data.get("desc", "")).strip()))

    async def api_project_update(request):
        old_name = request.path_params["name"]
        data = await payload(request)
        new_name = str(data.get("name", "")).strip()
        desc = str(data.get("desc", "")).strip()
        if not new_name or len(new_name) > 120:
            return json_error("项目名称须为 1–120 个字符")
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE projects SET name = ?, desc = ? WHERE name = ?",
                        (new_name, desc, old_name))
            if not cur.rowcount:
                return json_error("项目不存在", 404)
            conn.commit()
            return JSONResponse({"ok": True, "msg": "项目已更新"})
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                return json_error("同名项目已存在", 409)
            raise
        finally:
            conn.close()

    async def api_project_delete(request):
        return JSONResponse(project_delete(request.path_params["name"]))

    async def api_project_log(request):
        return JSONResponse(project_log(request.path_params["name"]))

    async def api_search(request):
        query = request.query_params.get("q", "").strip()
        if not query:
            return JSONResponse({"ok": True, "count": 0, "results": []})
        return JSONResponse(search(query, 50))

    async def api_entry_create(request):
        data = await payload(request)
        project = str(data.get("project", "")).strip()
        content = str(data.get("content", "")).strip()
        tags = str(data.get("tags", "")).strip()
        if not project or not content:
            return json_error("项目和正文不能为空")
        if len(content) > 100000 or len(tags) > 2000:
            return json_error("记录内容过长", 413)
        return JSONResponse(save(project, content, tags))

    async def api_entry_update(request):
        data = await payload(request)
        try:
            entry_id = int(request.path_params["entry_id"])
        except ValueError:
            return json_error("无效的记录编号")
        content = str(data.get("content", "")).strip()
        tags = str(data.get("tags", "")).strip()
        project = str(data.get("project", "")).strip()
        if not content:
            return json_error("正文不能为空")
        return JSONResponse(update(entry_id, content, tags, project))

    async def api_entry_delete(request):
        try:
            entry_id = int(request.path_params["entry_id"])
        except ValueError:
            return json_error("无效的记录编号")
        return JSONResponse(delete(entry_id))

    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if path.startswith("/api/") and path not in ("/api/login", "/api/session"):
                if not has_session(request):
                    return json_error("请先登录", 401)
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if request.headers.get("x-tech-memory") != "1":
                        return json_error("请求校验失败", 403)
                    origin = request.headers.get("origin")
                    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                        return json_error("跨站请求已拒绝", 403)
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
            return response

    routes = [
        Route("/", home),
        Mount("/static", StaticFiles(directory=WEB_DIR), name="static"),
        Route("/api/session", api_session), Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
        Route("/api/projects", api_projects), Route("/api/projects", api_project_create, methods=["POST"]),
        Route("/api/projects/{name:str}", api_project_update, methods=["PUT"]),
        Route("/api/projects/{name:str}", api_project_delete, methods=["DELETE"]),
        Route("/api/projects/{name:str}/entries", api_project_log),
        Route("/api/search", api_search), Route("/api/entries", api_entry_create, methods=["POST"]),
        Route("/api/entries/{entry_id:str}", api_entry_update, methods=["PUT"]),
        Route("/api/entries/{entry_id:str}", api_entry_delete, methods=["DELETE"]),
        Mount("/", app=mcp_app),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityMiddleware)],
                     lifespan=mcp_lifespan)


def mcp_bearer_guard(app, auth_token):
    """Keep Bearer auth on MCP while leaving the login shell reachable."""
    async def guarded(scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp") and auth_token:
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(supplied, f"Bearer {auth_token}"):
                response = JSONResponse({"error": "unauthorized"}, status_code=403)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)
    return guarded
