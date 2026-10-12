"""评审任务与 API 的测试。

三层分别盯：
    · `review_jobs` —— 状态机是否可靠（失败不留脏结果、淘汰不误伤运行中）
    · router —— 参数校验与状态码
    · 注入假 run —— 端到端走一遍，但**不打真实模型**
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_kit import review_jobs
from server.app import create_app


@pytest.fixture(autouse=True)
def clean_jobs():
    review_jobs._jobs.clear()
    yield
    review_jobs._jobs.clear()


def _ok_run(target, **opts):
    return f"# 工程健康度评审报告\n\n仓库 {target}\n选项 {opts}", 2, False


def _boom(target, **opts):
    raise RuntimeError("模型不可用")


# ------------------------------------------------------------ 任务状态机


def test_submit_returns_immediately():
    """提交必须立刻返回——同步等几分钟会把连接占死。"""
    job = review_jobs.submit("x", run=_ok_run)
    assert job.id
    assert job.state in ("queued", "running", "done")


def test_successful_job_carries_result():
    job = review_jobs.wait(review_jobs.submit("x", run=_ok_run, with_modules=True).id, timeout=10)
    assert job is not None and job.state == "done"
    assert job.module_count == 2
    assert "评审报告" in job.markdown
    assert job.error == ""


def test_options_passed_through_untouched():
    job = review_jobs.wait(
        review_jobs.submit("x", run=_ok_run, with_modules=True, module_code_lines=40).id, timeout=10)
    assert job.options["with_modules"] is True
    assert job.options["module_code_lines"] == 40


def test_failed_job_has_no_markdown():
    """失败不该留下半截报告——否则前端会渲染出一堆空章节。"""
    job = review_jobs.wait(review_jobs.submit("x", run=_boom).id, timeout=10)
    assert job is not None and job.state == "failed"
    assert "RuntimeError" in job.error
    assert "markdown" not in job.to_dict(), "失败任务的响应里不该有 markdown 键"


def test_elapsed_is_reported():
    job = review_jobs.wait(review_jobs.submit("x", run=_ok_run).id, timeout=10)
    assert job.to_dict()["elapsed_s"] >= 0


def test_list_sorted_newest_first():
    a = review_jobs.wait(review_jobs.submit("a", run=_ok_run).id, timeout=10)
    time.sleep(0.01)
    b = review_jobs.wait(review_jobs.submit("b", run=_ok_run).id, timeout=10)
    ids = [j["id"] for j in review_jobs.list_jobs()]
    assert ids.index(b.id) < ids.index(a.id)


def test_cancel_only_queued_jobs():
    """运行中的任务不能"取消"——Python 没法安全杀线程，别假装能。"""
    job = review_jobs.submit("x", run=_ok_run)
    assert review_jobs.cancel(job.id) in (True, False)   # 取决于线程是否已开跑
    done = review_jobs.wait(job.id, timeout=10)
    if done.state == "done":
        assert review_jobs.cancel(job.id) is False, "已完成的任务不该被取消"


def test_unknown_job_id():
    assert review_jobs.get("不存在") is None


def test_eviction_keeps_running_jobs():
    """超上限时只能丢已完成的——丢掉运行中的会让那个线程没人管。"""
    review_jobs.MAX_JOBS = 3
    try:
        running = review_jobs.ReviewJob(id="running-1", target="x", state="running",
                                        created_at="2020-01-01T00:00:00+00:00")
        review_jobs._jobs[running.id] = running
        for i in range(6):
            review_jobs._jobs[f"done-{i}"] = review_jobs.ReviewJob(
                id=f"done-{i}", target="x", state="done", created_at=f"2020-01-0{i + 1}T00:00:00+00:00")
        review_jobs._evict_old_jobs()
        assert running.id in review_jobs._jobs, "运行中的任务被误删"
        assert len(review_jobs._jobs) <= 3
    finally:
        review_jobs.MAX_JOBS = 50


# ------------------------------------------------------------ API


@pytest.fixture
def client():
    return TestClient(create_app())


def test_submit_and_poll(client, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(review_jobs, "_default_run", _ok_run)
    r = client.post("/api/review", json={"target": str(repo)})
    assert r.status_code == 202
    job = r.json()
    assert "markdown" not in job, "提交响应不该带报告正文"

    for _ in range(50):
        s = client.get(f"/api/review/{job['id']}").json()
        if s["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert s["state"] == "done"
    assert "评审报告" in s["markdown"]


def test_rejects_missing_local_dir(client):
    r = client.post("/api/review", json={"target": "Z:/绝对不存在"})
    assert r.status_code == 400


def test_rejects_empty_target(client):
    assert client.post("/api/review", json={"target": ""}).status_code == 422


def test_unknown_job_returns_404(client):
    assert client.get("/api/review/nope").status_code == 404
    assert client.delete("/api/review/nope").status_code == 404


def test_list_endpoint(client):
    r = client.get("/api/review?limit=5")
    assert r.status_code == 200
    assert "jobs" in r.json()