"""兼容入口：支持在仓库根目录直接运行 `python main.py` 或 `uvicorn main:app`。

主应用定义在 :mod:`app.main`，Docker 使用的是 ``app.main:app``。
"""

from __future__ import annotations

import os

from app.main import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "0") == "1",
    )
