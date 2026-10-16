from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_localization_service
from app.api.response import success
from app.model.models import ApiResponse, LocalizationStatus, NavigationTask, Pose
from app.service.localization_service import LocalizationService

router = APIRouter(prefix="/localization", tags=["localization"])


@router.get("/status", response_model=ApiResponse[LocalizationStatus])
def get_status(
    service: LocalizationService = Depends(get_localization_service),
) -> ApiResponse[LocalizationStatus]:
    return success(service.get_status())


@router.get("/pose", response_model=ApiResponse[Pose])
def get_pose(
    service: LocalizationService = Depends(get_localization_service),
) -> ApiResponse[Pose]:
    return success(service.get_pose())


@router.post("/relocalize", response_model=ApiResponse[NavigationTask])
def relocalize(
    service: LocalizationService = Depends(get_localization_service),
) -> ApiResponse[NavigationTask]:
    return success(service.relocalize())
