"""应用入口：挂载版本化记录存储的 REST API。

存储为进程内单例；可用环境变量 ``VRS_STORE_ID`` 固定实例 ID
（便于本地复现），不设置时随机生成。
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from versioned_record_store import VersionedRecordStore
from versioned_record_store.api import create_router, error_handlers_app

app = FastAPI(title="versioned-record-store")

store = VersionedRecordStore(store_id=os.getenv("VRS_STORE_ID"))
app.include_router(create_router(store))
error_handlers_app(app)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"store_id": store.store_id, "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
