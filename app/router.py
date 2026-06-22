"""Application configuration - root APIRouter.

Defines all FastAPI application endpoints.

Resources:
    1. https://fastapi.tiangolo.com/tutorial/bigger-applications

"""

from fastapi import APIRouter

from app.controllers.v1 import llm, reel, video

root_api_router = APIRouter()
# v1
root_api_router.include_router(video.router)
root_api_router.include_router(llm.router)
# 唤星 reel fork 新增端点（/api/v1/reel/*，doc19 §4.4）+ 健康检查（/ping、/health，token 豁免）
root_api_router.include_router(reel.router)
root_api_router.include_router(reel.health_router)
