"""csrf —— 双提交 cookie（double-submit cookie）中间件。

参考本体：backend/app/gateway/csrf_middleware.py
        （CSRF_COOKIE_NAME="csrf_token"、CSRF_HEADER_NAME="X-CSRF-Token"、
         只查 POST/PUT/DELETE/PATCH，GET/HEAD/OPTIONS 豁免）
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
_UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


class CSRFMiddleware(BaseHTTPMiddleware):
    """浏览器会自动带上 cookie，但不会自动带上自定义头——两者不一致即拒绝。

    攻击者能诱导浏览器发 POST（自动带 cookie），但读不到 cookie 内容，
    伪造不了 X-CSRF-Token 头。这就是"双提交"要两个各走一条通道的原因。

    简化（相对本体）：仅当请求已持有 csrf cookie 时才强制头校验——
    从没领过 cookie 的纯 API-key 脚本客户端不构成 CSRF 威胁面，直接放行，
    由鉴权层处置（见 README【与本体差异】）。
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _UNSAFE_METHODS:
            cookie = request.cookies.get(CSRF_COOKIE_NAME)
            if cookie and cookie != request.headers.get(CSRF_HEADER_NAME):
                return JSONResponse({"detail": "CSRF token missing or mismatched"}, status_code=403)
        response: Response = await call_next(request)
        # 只在安全方法上签发（等价本体"登录流程首次签发"）：
        # 纯 API-key 脚本客户端只发 POST，永远不会被动领到 cookie，
        # 从而不会被卷入浏览器 CSRF 语义。
        if request.method not in _UNSAFE_METHODS and CSRF_COOKIE_NAME not in request.cookies:
            response.set_cookie(CSRF_COOKIE_NAME, secrets.token_urlsafe(32), samesite="lax")
        return response
