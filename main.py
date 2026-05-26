import os
import sys

# Windows GBK 终端无法编码部分 Unicode 字符（如 [FAIL][OK][OK][FAIL]），
# 强制 stdout/stderr 用 UTF-8 并忽略不可编码字符，避免 UnicodeEncodeError 导致崩溃。
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    import io as _io
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        try:
            if _stream is None:
                continue
            if hasattr(_stream, "reconfigure"):
                try:
                    _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
                    continue
                except Exception:
                    pass
            buf = getattr(_stream, "buffer", None)
            if buf is not None:
                wrapped = _io.TextIOWrapper(buf, encoding="utf-8", errors="replace", line_buffering=True)
                setattr(sys, _stream_name, wrapped)
        except Exception:
            pass

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.account_checks import router as account_checks_router
from api.accounts import router as accounts_router
from api.actions import router as actions_router
from api.config import router as config_router
from api.credit_card_pool import router as credit_card_pool_router
from api.health import router as health_router
from api.google_account_pool import router as google_account_pool_router
from api.mailbox_inventory import router as mailbox_inventory_router
from api.platform_capabilities import router as platform_capabilities_router
from api.platforms import router as platforms_router
from api.provider_definitions import router as provider_definitions_router
from api.provider_settings import router as provider_settings_router
from api.proxies import router as proxies_router
from api.subscription_proxy import router as subscription_proxy_router
from api.system import router as system_router
from api.task_commands import router as task_commands_router
from api.task_logs import router as task_logs_router
from api.twoapi import (
    management_router as twoapi_management_router,
    proxy_router as twoapi_proxy_router,
    swarms_proxy_router as swarms_twoapi_proxy_router,
)
from api.tasks import router as tasks_router
from core.db import init_db
from core.registry import load_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_all()
    print("[OK] 数据库初始化完成")
    from core.registry import list_platforms
    print(f"[OK] 已加载平台: {[p['name'] for p in list_platforms()]}")
    from core.scheduler import scheduler
    scheduler.start()
    from services.task_runtime import task_runtime
    task_runtime.start()
    from services.solver_manager import start_async
    start_async()
    from services.twoapi.manager import get_twoapi_manager
    from services.twoapi.server_runtime import twoapi_server_runtime
    server_state = twoapi_server_runtime.ensure_running(timeout_seconds=10)
    if server_state.get("running"):
        print(f"[OK] 2API 服务已就绪: {server_state.get('listen')}")
    else:
        print(f"[WARN] 2API 服务未就绪: {server_state.get('error') or 'unknown'}")
    get_twoapi_manager().start_keepalive(interval_seconds=300)
    yield
    from core.scheduler import scheduler as _scheduler
    _scheduler.stop()
    from services.task_runtime import task_runtime as _task_runtime
    _task_runtime.stop()
    from services.solver_manager import stop
    stop()
    from services.twoapi.manager import get_twoapi_manager as _get_twoapi_manager
    _get_twoapi_manager().stop_keepalive()
    from services.twoapi.server_runtime import twoapi_server_runtime as _twoapi_server_runtime
    _twoapi_server_runtime.stop_owned()
    from core.subscription_proxy import subscription_proxy_manager
    subscription_proxy_manager.stop()


app = FastAPI(title="Account Manager", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts_router, prefix="/api")
app.include_router(account_checks_router, prefix="/api")
app.include_router(actions_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(credit_card_pool_router, prefix="/api")
app.include_router(health_router, prefix="/api")
app.include_router(google_account_pool_router, prefix="/api")
app.include_router(mailbox_inventory_router, prefix="/api")
app.include_router(platforms_router, prefix="/api")
app.include_router(platform_capabilities_router, prefix="/api")
app.include_router(provider_definitions_router, prefix="/api")
app.include_router(provider_settings_router, prefix="/api")
app.include_router(proxies_router, prefix="/api")
app.include_router(subscription_proxy_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(task_commands_router, prefix="/api")
app.include_router(task_logs_router, prefix="/api")
app.include_router(twoapi_management_router, prefix="/api")
app.include_router(twoapi_proxy_router)
app.include_router(swarms_twoapi_proxy_router)
app.include_router(system_router, prefix="/api")


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        return FileResponse(os.path.join(_static_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
