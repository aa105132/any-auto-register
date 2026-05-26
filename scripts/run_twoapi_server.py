from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.twoapi import management_router, proxy_router, swarms_proxy_router
from services.twoapi.manager import get_twoapi_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = get_twoapi_manager()
    manager.start_keepalive(interval_seconds=300)
    try:
        yield
    finally:
        manager.stop_keepalive()


app = FastAPI(title="Any Auto Register 2API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(management_router, prefix="/api")
app.include_router(proxy_router)
app.include_router(swarms_proxy_router)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=6543, reload=False)


if __name__ == "__main__":
    main()
