"""Same-origin web administration authenticated by the local Octop admin API."""

import hmac
import json
import math
import secrets
import time
from urllib.parse import urlsplit

import httpx
from admin_i18n import locale_for, render_admin, translate_template
from identity_profile import validate_profile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

COOKIE = "octop_gateway_admin"


class AdminWeb:
    def __init__(self, registry, base, allowed_hosts, client_factory=httpx.AsyncClient):
        self.registry = registry
        self.base = base
        self.client_factory = client_factory
        self.hosts = {h.split(":")[0] for h in allowed_hosts if h}
        self.hosts.update({"localhost", "127.0.0.1", "::1"})
        self.sessions = {}
        self.failures = {}

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        try:
            response = await self.handle(request)
        except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
            response = JSONResponse(
                {"error": "输入格式不正确，请检查身份、专家和有效期。"}, status_code=400
            )
        except httpx.HTTPError:
            response = JSONResponse({"error": "Octop 暂时无法连接，请稍后重试。"}, status_code=503)
        if isinstance(response, JSONResponse) and response.status_code >= 400:
            body = json.loads(response.body)
            if isinstance(body.get("error"), str):
                body["error"] = translate_template(body["error"], locale_for(request))
                response = JSONResponse(body, status_code=response.status_code)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
        )
        await response(scope, receive, send)

    async def backend(self, method, path, token=None, body=None):
        async with self.client_factory(timeout=15) as client:
            return await client.request(
                method,
                self.base + path,
                headers={"Authorization": "Bearer " + token} if token else {},
                json=body,
            )

    async def handle(self, request):
        if request.url.hostname not in self.hosts:
            return JSONResponse({"error": "invalid_host"}, status_code=400)
        path = request.url.path
        if request.method == "GET" and path in ("/admin", "/admin/"):
            nonce = secrets.token_urlsafe(24)
            response = HTMLResponse(render_admin(locale_for(request), nonce))
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'nonce-"
                + nonce
                + "'; style-src 'nonce-"
                + nonce
                + "'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
            return response
        if not path.startswith("/admin/api/"):
            return JSONResponse({"error": "not_found"}, status_code=404)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin", "")
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("http", "https")
                or parsed.netloc != request.headers.get("host")
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return JSONResponse({"error": "请求来源不匹配，请刷新管理页面。"}, status_code=403)
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        if path == "/admin/api/login" and request.method == "POST":
            peer = request.client.host if request.client else "local"
            failures = [t for t in self.failures.get(peer, []) if t > now - 300]
            if len(failures) >= 8:
                return JSONResponse(
                    {"error": "登录失败次数过多，请 5 分钟后重试。"}, status_code=429
                )
            body = await request.json()
            if set(body) != {"username", "password"} or any(
                not isinstance(body[k], str) or not body[k] or len(body[k]) > 1024 for k in body
            ):
                raise ValueError()
            result = await self.backend("POST", "/api/auth/login", body=body)
            if result.status_code != 200:
                self.failures[peer] = failures + [now]
                return JSONResponse({"error": "账号或密码错误。"}, status_code=401)
            token = result.json()["access_token"]
            me = await self.backend("GET", "/api/auth/me", token)
            if me.status_code != 200 or me.json().get("role") != "admin":
                self.failures[peer] = failures + [now]
                return JSONResponse({"error": "需要 Octop 管理员账号。"}, status_code=403)
            self.failures.pop(peer, None)
            old = request.cookies.get(COOKIE)
            if old:
                self.sessions.pop(old, None)
            session_id = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self.sessions[session_id] = {
                "token": token,
                "csrf": csrf,
                "expires": now + 3600,
                "username": me.json().get("username", "admin"),
            }
            response = JSONResponse(
                {"ok": True, "csrf": csrf, "username": me.json().get("username", "admin")}
            )
            response.set_cookie(
                COOKIE,
                session_id,
                max_age=3600,
                httponly=True,
                samesite="strict",
                secure=request.url.hostname not in ("localhost", "127.0.0.1", "::1"),
                path="/",
            )
            return response
        session = self.sessions.get(request.cookies.get(COOKIE, ""))
        if not session:
            return JSONResponse({"error": "请登录管理员账号。"}, status_code=401)
        if request.method not in ("GET", "HEAD") and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), session["csrf"]
        ):
            return JSONResponse({"error": "会话校验失败，请刷新页面。"}, status_code=403)
        me = await self.backend("GET", "/api/auth/me", session["token"])
        if me.status_code != 200 or me.json().get("role") != "admin":
            self.sessions.pop(request.cookies.get(COOKIE, ""), None)
            return JSONResponse({"error": "管理员会话已失效，请重新登录。"}, status_code=401)
        if path == "/admin/api/session" and request.method == "GET":
            return JSONResponse(
                {"ok": True, "csrf": session["csrf"], "username": session["username"]}
            )
        if path == "/admin/api/logout" and request.method == "POST":
            self.sessions.pop(request.cookies.get(COOKIE, ""), None)
            response = JSONResponse({"ok": True})
            response.delete_cookie(COOKIE, path="/")
            return response
        if path == "/admin/api/experts" and request.method == "GET":
            result = await self.backend("GET", "/api/agents", session["token"])
            result.raise_for_status()
            rows = result.json()
            rows = rows if isinstance(rows, list) else rows["agents"]
            fid = self.registry.feedback_agent()
            experts = [
                {
                    "agent_id": r["agent_id"],
                    "name": r.get("name") or r["agent_id"],
                    "description": r.get("description") or "",
                }
                for r in rows
                if r.get("agent_id") in self.registry.allowed and r.get("agent_id") != fid
            ]
            return JSONResponse({"experts": experts, "feedback_agent_id": fid})
        if path == "/admin/api/tokens" and request.method == "GET":
            return JSONResponse({"tokens": self.registry.listing()})
        if path == "/admin/api/tokens" and request.method == "POST":
            data = self.validate(await request.json())
            value = self.registry.issue(**data)
            return JSONResponse(value, status_code=201)
        if path.startswith("/admin/api/tokens/"):
            tid = path.removeprefix("/admin/api/tokens/")
            if "/" in tid:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if request.method == "PATCH":
                data = self.validate(await request.json())
                self.registry.update_token(tid, **data)
                return JSONResponse({"ok": True})
            if request.method == "DELETE":
                return JSONResponse({"ok": self.registry.revoke(tid)})
        return JSONResponse({"error": "not_found"}, status_code=404)

    def validate(self, body):
        if not isinstance(body, dict) or set(body) != {
            "name",
            "agents",
            "feedback",
            "expires",
            "profile",
        }:
            raise ValueError()
        name = body["name"]
        agents = body["agents"]
        expires = body["expires"]
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError()
        if (
            not isinstance(agents, list)
            or any(not isinstance(a, str) for a in agents)
            or not set(agents).issubset(self.registry.allowed)
        ):
            raise ValueError()
        if self.registry.feedback_agent() in agents:
            raise ValueError()
        if not isinstance(body["feedback"], bool):
            raise ValueError()
        if body["feedback"] and not self.registry.feedback_agent():
            raise ValueError()
        if expires is not None and (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or expires <= time.time()
        ):
            raise ValueError()
        return {**body, "name": name.strip(), "profile": validate_profile(body["profile"])}
