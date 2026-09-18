"""访问门禁路由（S-6）。

``POST /api/auth/login`` / ``POST /api/auth/logout`` / ``GET /api/auth/me``。

★ 这三个端点**永远免认证**（见 :mod:`bt169.api.gate`）——否则门禁一开
就没人能登录进来，只能删库自救。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from bt169.api.auth import (
    SESSION_COOKIE,
    SESSION_DAYS,
    cookie_kwargs,
    create_session,
    is_gate_enabled,
    is_https,
    revoke_session,
    validate_session,
    verify_access_password,
)
from bt169.api.deps import get_settings
from bt169.db import Database
from bt169.repo.settings import SettingsRepo

router = APIRouter(tags=["auth"])

#: 会话 cookie 的 max-age（秒）。与 ``SESSION_DAYS`` 保持一致。
_MAX_AGE = SESSION_DAYS * 24 * 3600


class LoginBody(BaseModel):
    """登录请求。"""

    password: str = ""


def _get_db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


@router.get("/api/auth/me")
def me(request: Request, sr: SettingsRepo = Depends(get_settings)) -> dict[str, bool]:
    """当前认证状态。

    前端用它决定「显示登录页还是主界面」。

    ★ 门禁关闭时 ``authenticated`` 恒为 ``True``：没有密码就没有边界，
    把用户挡在一个不存在的门外只会让人困惑。
    """
    enabled = is_gate_enabled(sr)
    if not enabled:
        return {"authenticated": True, "gate_enabled": False}
    token = request.cookies.get(SESSION_COOKIE)
    return {
        "authenticated": validate_session(_get_db(request), token),
        "gate_enabled": True,
    }


@router.post("/api/auth/login")
def login(
    request: Request,
    response: Response,
    body: LoginBody,
    sr: SettingsRepo = Depends(get_settings),
) -> dict[str, bool]:
    """校验访问密码，成功则下发会话 cookie。"""
    # 门禁关闭时直接放行：前端可能无脑调一次登录，不该报错。
    if not is_gate_enabled(sr):
        return {"authenticated": True, "gate_enabled": False}

    if not verify_access_password(sr, body.password):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=401,
            detail={"code": "bad_password", "message": "访问密码错误"},
        )

    token = create_session(_get_db(request))
    # ★ cookie_kwargs 已含 max_age；不能再单独传一次，否则
    #   TypeError: got multiple values for keyword argument 'max_age'。
    response.set_cookie(
        SESSION_COOKIE,
        token,
        **cookie_kwargs(request, max_age=_MAX_AGE),  # type: ignore[arg-type]
    )
    return {"authenticated": True, "gate_enabled": True}


@router.post("/api/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    """登出：撤销当前会话并清除 cookie。

    ★ 必须用**同一个** Response 对象既设状态码又删 cookie。
    之前写成「改注入的 response、再 return 一个新建的 Response(204)」
    ——注入对象的改动被丢掉了，浏览器里的 cookie 一直留着。
    """
    revoke_session(_get_db(request), request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )
    return response
