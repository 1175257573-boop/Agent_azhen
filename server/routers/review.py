"""评审任务 Controller —— 提交工程评审、轮询状态、取回报告。

和 `/api/chat` 的流式思路不同：评审**不流式**。
一次评审在大仓库上要几分钟，SSE 挂着连接不如让前端轮询状态——
连接断了不影响后台任务，重新拉一次状态就回来了。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_kit import review_jobs
from agent_kit.logging_conf import get_logger

router = APIRouter(prefix="/api/review", tags=["review"])

log = get_logger("web.review")


class ReviewSubmit(BaseModel):
    """提交一次评审。"""

    target: str = Field(..., description="仓库 URL 或本地路径", min_length=1, max_length=500)
    with_modules: bool = Field(default=False, description="是否做模块职责分析（额外几次模型调用）")
    module_top_n: int = Field(default=9, ge=1, le=30, description="分析多少个核心模块")
    module_batch: int = Field(default=3, ge=1, le=8, description="每批几个模块")
    module_code_lines: int = Field(
        default=0, ge=0, le=200,
        description="每个模块附多少行真实代码，0 = 只给结构摘要",
    )
    no_judge: bool = Field(default=False, description="强制降级：只出证据清单，不调模型")


@router.post("", status_code=202)
def submit_review(body: ReviewSubmit):
    """提交评审，**立即返回** job_id（202 Accepted）。

    不等结果：几分钟的同步请求会让连接一直占着，网关一断就前功尽弃。
    """
    target = body.target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="target 不能为空")
    # 路径穿越：本地路径必须是真的存在的目录，别让调用方拿它去读任意文件
    if not target.startswith(("http://", "https://", "git@")):
        from pathlib import Path

        if not Path(target).expanduser().is_dir():
            raise HTTPException(status_code=400, detail=f"本地目录不存在：{target}")

    job = review_jobs.submit(
        target,
        with_modules=body.with_modules,
        module_top_n=body.module_top_n,
        module_batch=body.module_batch,
        module_code_lines=body.module_code_lines,
        no_judge=body.no_judge,
    )
    log.info("已受理评审任务 %s：%s", job.id, target)
    return job.to_dict(include_result=False)


@router.get("")
def list_reviews(limit: int = 20):
    """最近的评审任务列表（不含正文，轮询用）。"""
    return {"jobs": review_jobs.list_jobs(limit=max(1, min(limit, 100)))}


@router.get("/{job_id}")
def get_review(job_id: str):
    """查任务状态；已完成时带 `markdown` 报告正文。"""
    job = review_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在或已过期：{job_id}")
    return job.to_dict(include_result=True)


@router.delete("/{job_id}")
def cancel_review(job_id: str):
    """取消任务。

    只对**排队中**的任务有效：Python 无法安全终止运行中的线程，
    与其假装能取消，不如明确告知。
    """
    if review_jobs.get(job_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")
    ok = review_jobs.cancel(job_id)
    return {
        "cancelled": ok,
        "reason": "" if ok else "任务已在运行，无法安全终止；等它跑完即可",
    }