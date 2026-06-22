# 唤星 reel sidecar 入口（daemon 启动契约）。
#
# daemon `domains/reel/{locator,supervisor}.rs` 以 `python -m uvicorn api.app:app`
# 启动 sidecar（cwd=fork 根 / 包内 backend），硬要求 `<backend_dir>/api/app.py` 存在并
# 模块级暴露 `app`。本文件仅再导出 MPT 原生 `app/asgi.py` 里已被唤星安全加固
# （`apply_huanxing_hardening`：CORS 收敛 + Host 闸 + X-Reel-Token 闸）的 FastAPI 实例——
# import 时即触发 app/asgi.py 的 app 创建 + 加固副作用，故模块式启动保留全部安全闸。
#
# `app/` 包相对 cwd（fork 根 / 包内 backend）原生可解析，MPT config 亦相对 cwd 读取，
# 与 `uvicorn app.asgi:app`（冒烟已证）等价。MPT 的 `app/` 包与加固一律不在此改动。
from app.asgi import app  # noqa: F401
