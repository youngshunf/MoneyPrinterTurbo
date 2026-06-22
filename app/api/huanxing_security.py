"""唤星 reel 安全加固（fork patch，集中一处便于 upstream rebase，照 VideoClaw/PRES-P2-e 范式）。

全部 env-gated —— env 未设时回落开放（本地 dev 直跑不受影响）；打包/生产由 daemon 注入：
- REEL_ALLOWED_ORIGINS：CORS 白名单（逗号分隔）。未设 = `*`。
- REEL_ALLOWED_HOSTS：Host 头白名单（逗号分隔）。未设默认 `127.0.0.1,localhost`（sidecar 只绑 loopback）。
- REEL_SIDECAR_TOKEN：sidecar 访问令牌。设了则非豁免路径必须带 `X-Reel-Token`，
  由 daemon 反代时附加（其它来源直连 sidecar 被拒，缺/错即 403）。未设 = 不鉴权。

放行策略（探活/反代健康需无 token 可达）：
- `/ping`（MPT 原生健康检查，app/controllers/ping.py），`/docs` / `/openapi.json` / `/redoc`，根 `/`。
- **所有业务端点过闸**：上游 `/api/v1/*`（videos/scripts/terms/tasks/stream/download/musics/video_materials…）
  与 fork 新增 `/api/v1/reel/*`（materials/search、tasks/{id}/events）一律要求 token。
"""

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

logger = logging.getLogger(__name__)

# 鉴权豁免前缀：健康检查 + API 文档（探活/反代健康必须无 token 可达）。
_TOKEN_EXEMPT_PREFIXES = ("/ping", "/health", "/docs", "/openapi.json", "/redoc")
_TOKEN_HEADER = "X-Reel-Token"

# Host 闸默认值：sidecar 只应被 daemon 经 loopback 反代访问。
_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


class SidecarTokenMiddleware(BaseHTTPMiddleware):
    """要求非豁免请求带正确的 sidecar token（由 daemon 反代附加）。token 未配置则不装该中间件。"""

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path == "/" or any(
            path == prefix or path.startswith(prefix + "/") or path.startswith(prefix)
            for prefix in _TOKEN_EXEMPT_PREFIXES
        ):
            return await call_next(request)
        if request.headers.get(_TOKEN_HEADER) != self.token:
            return JSONResponse(
                status_code=403,
                content={"detail": "missing or invalid sidecar token"},
            )
        return await call_next(request)


def apply_huanxing_hardening(app: FastAPI) -> None:
    """装配 CORS 收敛 + Host 闸 + sidecar token 闸（env-gated）。替代上游裸 `allow_origins=['*']`。

    注意 Starlette 中间件后注册者最先执行（LIFO）。这里按 token → Host → CORS 顺序注册，
    使运行时执行顺序为 CORS → Host → token，与 VideoClaw/Presenton 一致。
    """
    token = os.environ.get("REEL_SIDECAR_TOKEN", "").strip()
    if token:
        app.add_middleware(SidecarTokenMiddleware, token=token)
        logger.info("Sidecar token gate enabled")

    hosts = _csv_env("REEL_ALLOWED_HOSTS") or list(_DEFAULT_ALLOWED_HOSTS)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    logger.info("Host gate enabled: %s", hosts)

    origins = _csv_env("REEL_ALLOWED_ORIGINS") or ["*"]
    allow_credentials = origins != ["*"]  # 通配来源不能带凭据（CORS 规范）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("CORS origins: %s (credentials=%s)", origins, allow_credentials)
