"""前端托管回归：新 SPA（web/dist）与旧静态页（static/）的挂载行为。

用 TestClient 直接打 ASGI 应用，不走真实端口——本机有透明代理，
走 HTTP 的话 127.0.0.1 也会被劫持成 502，测出来的不是应用行为。

WEB_DIR 被 monkeypatch 到 tmp 目录，所以「构建过 / 没构建过」两种状态都能测到，
不依赖仓库里是否真的执行过 npm run build。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from server import static_site

SPA_INDEX = '<div id="root"></div>'


def _build(tmp_path) -> None:
    """造一个最小的 Vite 产物：index.html + assets/app.js。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "index.html").write_text(
        f'<!doctype html><html><body>{SPA_INDEX}<script src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('atlas')", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")


@pytest.fixture()
def spa(tmp_path, monkeypatch):
    """已构建：web/dist 存在。"""
    web = tmp_path / "dist"
    _build(web)
    monkeypatch.setattr(static_site, "WEB_DIR", web)
    from server.app import create_app

    return TestClient(create_app())


@pytest.fixture()
def unbuilt(tmp_path, monkeypatch):
    """没构建过：web/dist/index.html 不存在。"""
    web = tmp_path / "empty-dist"
    web.mkdir()
    monkeypatch.setattr(static_site, "WEB_DIR", web)
    from server.app import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# 1. 已构建时托管新 SPA
# ---------------------------------------------------------------------------
def test_root_serves_spa_shell(spa):
    resp = spa.get("/")
    assert resp.status_code == 200
    assert SPA_INDEX in resp.text
    assert "/assets/app.js" in resp.text


def test_client_side_routes_fall_back_to_index(spa):
    """前端自己做路由，刷新 /sessions 这种地址不能 404。"""
    for path in ("/sessions", "/queue", "/memory", "/settings", "/approvals"):
        resp = spa.get(path)
        assert resp.status_code == 200, path
        assert SPA_INDEX in resp.text, path


def test_assets_are_served_with_correct_type(spa):
    resp = spa.get("/assets/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "atlas" in resp.text


def test_root_level_files_are_served(spa):
    resp = spa.get("/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_unknown_api_path_is_404_json_not_html(spa):
    """/api 下未注册的路径必须 404 JSON。

    回落成 HTML 的话前端 fetch 会拿到 200 + 一段 HTML，
    JSON 解析失败后只显示「网络错误」，真正的 404 线索反而没了。
    """
    resp = spa.get("/api/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert "未找到接口" in resp.text


def test_path_traversal_is_not_served(spa, tmp_path):
    """SPA 回落只允许 web/dist 内的文件。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")

    for path in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt"):
        resp = spa.get(path)
        assert "top-secret" not in resp.text, path


def test_api_routes_still_win(spa):
    """SPA 兜底不能把接口吞掉。"""
    assert spa.get("/api/modes").status_code == 200
    assert spa.get("/api/info").status_code == 200


# ---------------------------------------------------------------------------
# 2. 没构建过时退回旧静态页
# ---------------------------------------------------------------------------
def test_legacy_index_when_not_built(unbuilt):
    resp = unbuilt.get("/")
    assert resp.status_code == 200
    assert SPA_INDEX not in resp.text  # 旧版 static/index.html


def test_legacy_pages_always_available(spa, unbuilt):
    """旧资源与旧控制台页无论如何都要在，桌面端打包依赖它们。"""
    for client in (spa, unbuilt):
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/client").status_code == 200


def test_real_build_artifacts_are_served():
    """真的构建过（web/dist 存在）时，产物必须能被完整取到。

    前面的用例都用 tmp 目录模拟；这条打真实产物，用来兜住
    「index.html 引用了 assets/xxx.js，但那个文件没被挂出来」这类只会在真机上炸的问题。
    """
    if not (static_site.WEB_DIR / "index.html").exists():
        pytest.skip("web/dist 未构建，跳过（cd web && npm install && npm run build）")

    from server.app import create_app

    client = TestClient(create_app())
    html = client.get("/").text
    assert '<div id="root"></div>' in html, "首页不是构建产物，可能构建坏了"

    assets = re.findall(r'/(assets/[^"\']+\.(?:js|css))', html)
    assert assets, f"构建产物里没找到资源引用：{html[:200]}"
    for asset in assets:
        resp = client.get(f"/{asset}")
        assert resp.status_code == 200, f"{asset} 取不到"


# ---------------------------------------------------------------------------
# 3. 网关同样托管前端（三进程部署时它是唯一入口）
# ---------------------------------------------------------------------------
def test_gateway_also_serves_spa(tmp_path, monkeypatch):
    web = tmp_path / "dist"
    _build(web)
    monkeypatch.setattr(static_site, "WEB_DIR", web)

    from server.gateway import create_gateway_app

    client = TestClient(create_gateway_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert SPA_INDEX in resp.text
    assert client.get("/sessions").status_code == 200
    assert client.get("/api/nope").status_code == 404
