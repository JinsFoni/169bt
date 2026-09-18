"""门禁中间件：真正的安全边界（S-6 / ADR-12）。

需求 §8.13 问「访问密码仅前端门禁还是后端校验」。
**架构结论：必须后端校验**——静态资源任何人可下载，前端门禁不是边界。

## 放行规则

| 路径 | 门禁开启时 |
|---|---|
| ``/api/auth/*`` | **永远放行**（否则没人能登录进来） |
| ``/api/health`` | 放行（存活探针；且不泄漏内容） |
| ``/api/*`` 其他 | 必须已认证 |
| ``/img/*`` | 必须已认证（**用户内容**，不是「静态资源」） |
| 其他（HTML/JS/CSS） | 放行（登录页本身要能加载） |

★ ``/img`` 归到「必须认证」是刻意的：本地化图片是**用户内容**
（论坛帖子封面/详情图）。把它当普通静态资源放行，等于把整个图库公开
——门禁就白设了。

## CSRF

写操作（非 GET/HEAD/OPTIONS）校验 ``Origin``：存在且与 Host 不同源 →
403。配合 cookie 的 ``SameSite=Lax``（§9 两道防线）。

> 没有 ``Origin`` 头的请求放行：这是**非浏览器**客户端（curl / 本机
> CLI / 反向代理）的正常形态，而 CSRF 的威胁模型只涉及浏览器。
> 要求必须有 Origin 会误伤 CLI 且不增加任何安全性。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from bt169.api.auth import SESSION_COOKIE, is_gate_enabled, validate_session

__all__ = ["GateMiddleware"]

#: 免认证的路径前缀。顺序无关，逐个 ``startswith`` 判断。
_PUBLIC_PREFIXES = (
    "/api/auth/",       # 登录/登出/状态——否则门禁一开就锁死自己
    "/api/health",      # 存活探针
)

#: 必须认证的路径前缀（门禁开启时）。比 ``_PUBLIC_PREFIXES`` 优先判断。
_PROTECTED_PREFIXES = (
    "/api/",            # 除上面放行的以外全部
    "/img/",            # ★ 用户内容，不是静态资源
)

#: 写操作的 HTTP 方法（需要 Origin 校验）。
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class GateMiddleware(BaseHTTPMiddleware):
    """访问门禁 + CSRF。

    用法::

        app.add_middleware(GateMiddleware)

    从 ``request.app.state.db`` 取数据库（不引入全局单例）。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        # ---------- CSRF：写操作校验 Origin ----------
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and not _same_origin(origin, request):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "bad_origin",
                            "message": f"跨源写入被拒绝：{origin}",
                        }
                    },
                )

        # ---------- 门禁 ----------
        path = request.url.path
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        if not any(path.startswith(p) for p in _PROTECTED_PREFIXES):
            # 前端 HTML/JS/CSS：放行，否则登录页加载不出来
            return await call_next(request)

        db = getattr(request.app.state, "db", None)
        if db is None:
            return await call_next(request)

        from bt169.repo.settings import SettingsRepo

        sr = SettingsRepo(db, request.app.state.box)
        if not is_gate_enabled(sr):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if not validate_session(db, token):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "unauthorized",
                        "message": "需要访问密码",
                    }
                },
            )

        return await call_next(request)


def _same_origin(origin: str, request: Request) -> bool:
    """``Origin`` 是否与请求同源。

    ★ 比较 **Host**（含端口），不比较 scheme：本机 http、经 Lucky 反代
    时外部是 https，但 ``Host`` 仍是同一个。按 scheme 比会把正常请求
    判成跨源。
    """
    host = request.headers.get("host", "")
    if not host:
        return True
    # origin 形如 "http://127.0.0.1:8899"；取 "//" 之后的部分
    stripped = origin.split("//", 1)[-1].rstrip("/")
    return stripped.lower() == host.lower()
