"""应用入口：FastAPI + SQLite + SQLAlchemy。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from . import __version__
from .config import settings
from .database import Base, engine
from .routers import anchors, devices, readings, replay

DESCRIPTION = """文物展柜环境变点分析 API。

* **上报**：客户端按设备序号提交带时间戳的温湿度读数；同设备同时间戳的重复上报自动合并（幂等）。
* **分段**：对每个展柜的时间线执行带惩罚项的确定性 PELT 变点分段，输出各段起止、均值、相对上一段的变化幅度与置信标记。
* **重放**：迟到读数补入或锚点变更时，在最近锚点之后的受影响时间线内重算分段（同一事务内替换），并留下审计记录。
* **锚点校准**：锚点强制分段边界，用于人工标定的环境事件（如开关柜、设备维护）。
* **预览**：`/api/v1/replay-preview` 试算假设读数补入后的分段，**绝不写库**。

所有时间戳均为 UTC；分段参数（惩罚项等）见 `APP_*` 环境变量。
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="展柜环境变点分析 API",
    version=__version__,
    description=DESCRIPTION,
    lifespan=lifespan,
)

app.include_router(readings.router)
app.include_router(devices.router)
app.include_router(anchors.router)
app.include_router(replay.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"], summary="健康检查")
def health():
    return {
        "status": "ok",
        "version": __version__,
        "penalty": settings.penalty,
        "min_size": settings.min_size,
    }
