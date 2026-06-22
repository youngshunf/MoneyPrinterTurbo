"""唤星 reel fork 新增 REST 端点（doc19 §4.4），统一挂 `/api/v1/reel/` 前缀，与上游 `/api/v1/*` 区分。

1. POST /api/v1/reel/materials/search —— 同步库存素材搜索（包 material.py 搜索函数，多 key 轮换），
   返回候选片段 [{provider,url,duration}]，**不下载、不合成**（给主人/分身挑片）。doc19 §4.4 / N15。
2. POST /api/v1/reel/tasks/{task_id}/events —— JSONL 流式端点（MPT 原生无 SSE，只能轮询）。
   内部周期轮询任务 state/progress，逐行吐 JSON；daemon 复用 film `post_sse` 消费器。doc19 §4.4 / N14。

这些是 fork 新增、隔离在独立 controller，便于随上游 rebase。
"""

import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Path, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from app.models import const
from app.models.schema import VideoAspect
from app.services import material
from app.services import state as sm

router = APIRouter()
router.tags = ["Reel"]
router.prefix = "/api/v1/reel"

# 健康检查（无前缀，token 闸豁免 —— 见 huanxing_security._TOKEN_EXEMPT_PREFIXES）。
# 上游 app/controllers/ping.py 实际未挂进 root_api_router（dead code），daemon supervisor
# 探活需要一个稳定的无鉴权 liveness 端点（doc19 §4.3），故 fork 在此显式提供 /ping 与 /health。
health_router = APIRouter()


@health_router.get("/ping", tags=["Health Check"], summary="Liveness probe")
def ping() -> dict:
    return {"status": "ok"}


@health_router.get("/health", tags=["Health Check"], summary="Liveness probe (alias)")
def health() -> dict:
    return {"status": "ok"}

# JSONL 事件流参数。
_EVENTS_POLL_INTERVAL_SECONDS = 1.0
_EVENTS_MAX_DURATION_SECONDS = 30 * 60  # 长任务上限（30min），到点收尾防句柄泄漏。


class MaterialSearchRequest(BaseModel):
    search_terms: List[str] = Field(..., min_length=1)
    video_source: str = "pexels"
    minimum_duration: Optional[int] = 5
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait.value


class MaterialCandidate(BaseModel):
    provider: str
    url: str
    duration: int


def _search_fn(video_source: str):
    source = (video_source or "pexels").strip().lower()
    if source == "pixabay":
        return material.search_videos_pixabay
    if source == "coverr":
        return material.search_videos_coverr
    return material.search_videos_pexels


@router.post(
    "/materials/search",
    response_model=List[MaterialCandidate],
    summary="Search stock video candidates (no download, no compose)",
)
def search_materials(request: Request, body: MaterialSearchRequest) -> List[MaterialCandidate]:
    search_videos = _search_fn(body.video_source)
    minimum_duration = body.minimum_duration if body.minimum_duration is not None else 5
    seen_urls: set[str] = set()
    candidates: List[MaterialCandidate] = []
    for term in body.search_terms:
        term = (term or "").strip()
        if not term:
            continue
        items = search_videos(
            search_term=term,
            minimum_duration=minimum_duration,
            video_aspect=body.video_aspect,
        )
        for item in items:
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            candidates.append(
                MaterialCandidate(
                    provider=item.provider, url=item.url, duration=int(item.duration)
                )
            )
    logger.info(
        f"reel material search: terms={body.search_terms} source={body.video_source} "
        f"candidates={len(candidates)}"
    )
    return candidates


def _task_progress(task: dict) -> int:
    try:
        return int(task.get("progress", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _task_state(task: dict) -> int:
    try:
        return int(task.get("state", const.TASK_STATE_PROCESSING))
    except (TypeError, ValueError):
        return const.TASK_STATE_PROCESSING


def _stage_complete_payload(task: dict) -> dict:
    """完成时回传产物载荷（与 GET /tasks/{id} 同口径，daemon `stage_complete` 取此 payload）。"""
    keys = (
        "videos",
        "combined_videos",
        "script",
        "terms",
        "audio_file",
        "subtitle_path",
        "materials",
    )
    return {k: task[k] for k in keys if k in task}


async def _event_stream(task_id: str):
    """周期轮询任务状态，逐行吐 JSON：heartbeat / progress / stage_complete / error。

    流断且无终止事件时，消费方（daemon film post_sse）按错处理（零 fake）。
    """
    elapsed = 0.0
    last_progress = -1
    while elapsed < _EVENTS_MAX_DURATION_SECONDS:
        task = sm.state.get_task(task_id)
        if not task:
            yield json.dumps({"type": "error", "message": f"task not found: {task_id}"}) + "\n"
            return

        state = _task_state(task)
        progress = _task_progress(task)

        if state == const.TASK_STATE_FAILED:
            yield json.dumps(
                {"type": "error", "message": task.get("error") or "task failed"}
            ) + "\n"
            return

        if progress != last_progress:
            last_progress = progress
            yield json.dumps({"type": "progress", "progress": progress}) + "\n"
        else:
            yield json.dumps({"type": "heartbeat"}) + "\n"

        if state == const.TASK_STATE_COMPLETE:
            yield json.dumps(
                {"type": "stage_complete", "payload": _stage_complete_payload(task)}
            ) + "\n"
            return

        await asyncio.sleep(_EVENTS_POLL_INTERVAL_SECONDS)
        elapsed += _EVENTS_POLL_INTERVAL_SECONDS

    # 到达上限仍未终止：明确报错，不静默截断（零 fake）。
    yield json.dumps(
        {"type": "error", "message": f"event stream timed out after {_EVENTS_MAX_DURATION_SECONDS}s"}
    ) + "\n"


@router.post(
    "/tasks/{task_id}/events",
    summary="Stream task progress as JSONL (heartbeat/progress/stage_complete/error)",
)
async def stream_task_events(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    return StreamingResponse(
        _event_stream(task_id),
        media_type="application/x-ndjson",
    )
