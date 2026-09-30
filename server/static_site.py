"""前端托管 —— 新 SPA（web/dist）与旧静态页（static/）统一入口。

新前端是 React + Vite 的**单页应用**，和旧的 Multi-page 静态资源托管方式不同：
    * 资源在 `/assets/**`；
    * 任意未命中的路径都要回落到 `index.html`（前端自己做路由），
      否则刷新 `/sessions` 这种地址会直接 404。

旧的 `/static/**` 与 `/client` 一律保留：老页面还在用，桌面端打包也依赖它们。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web" / "dist"
STATIC_DIR = PROJECT_ROOT / "static"


def frontend_dir() -> Path:
    """优先新前端；没构建过就退回旧静态目录。"""
    if (WEB_DIR / "index.html").exists():
        return WEB_DIR
    return STATIC_DIR


def mount_frontend(app: FastAPI) -> None:
    """挂上前端资源。必须在所有 /api 路由之后调用，免得吞掉接口。"""
    # 旧静态资源与旧控制台页：无论有没有新前端都保留
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/client", include_in_schema=False)
        def _legacy_client():
            return FileResponse(str(STATIC_DIR / "client.html"))

    web = WEB_DIR / "index.html"
    if not web.exists():
        # 还没构建：沿用旧版 index.html
        @app.get("/", include_in_schema=False)
        def _legacy_index():
            return FileResponse(str(STATIC_DIR / "index.html"))

        return

    assets = WEB_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="web-assets")

    @app.get("/", include_in_schema=False)
    def _index():
        return FileResponse(str(web))

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str):
        # /api 下的未注册路径不能回落成 HTML——否则前端会拿到 200 + 一段 HTML，
        # 解析失败后只显示"网络错误"，真正的 404 线索反而没了
        if full_path.startswith("api/") or full_path == "api":
            return JSONResponse(status_code=404, content={"detail": f"未找到接口：{full_path}"})

        target = (WEB_DIR / full_path).resolve()
        root = WEB_DIR.resolve()
        if target.is_file() and str(target).startswith(str(root)):
            return FileResponse(str(target))

        resp: Response = FileResponse(str(web))
        resp.headers["Cache-Control"] = "no-cache"
        return resp
