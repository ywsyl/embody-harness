from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_navigation_service
from app.api.response import success
from app.model.models import (
    ApiResponse,
    NavigationGoPoseRequest,
    NavigationGoRequest,
    NavigationStatus,
    NavigationTask,
    Pose,
)
from app.service.navigation_service import NavigationService

router = APIRouter(prefix="/navigation", tags=["navigation"])


@router.post("/go", response_model=ApiResponse[NavigationTask])
def navigate_to_landmark(
    request: NavigationGoRequest,
    service: NavigationService = Depends(get_navigation_service),
) -> ApiResponse[NavigationTask]:
    return success(service.navigate_to(request.destination))


@router.post("/go_pose", response_model=ApiResponse[NavigationTask])
def navigate_to_pose(
    request: NavigationGoPoseRequest,
    service: NavigationService = Depends(get_navigation_service),
) -> ApiResponse[NavigationTask]:
    pose = Pose(x=request.x, y=request.y, yaw=request.yaw)
    return success(service.navigate_to_pose(pose))


@router.post("/cancel", response_model=ApiResponse[None], response_model_exclude_none=True)
def cancel_navigation(
    service: NavigationService = Depends(get_navigation_service),
) -> ApiResponse[None]:
    service.cancel()
    return success()


@router.get("/status", response_model=ApiResponse[NavigationStatus])
def get_navigation_status(
    service: NavigationService = Depends(get_navigation_service),
) -> ApiResponse[NavigationStatus]:
    return success(service.get_navigation_status())
