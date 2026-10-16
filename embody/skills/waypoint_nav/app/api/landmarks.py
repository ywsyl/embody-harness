from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends

from app.api.deps import get_landmark_service
from app.api.response import success
from app.model.models import ApiResponse, LandmarkSummary
from app.service.landmark_service import LandmarkService

router = APIRouter(prefix="/landmarks", tags=["landmarks"])


@router.get("", response_model=ApiResponse[List[LandmarkSummary]])
def list_landmarks(
    service: LandmarkService = Depends(get_landmark_service),
) -> ApiResponse[List[LandmarkSummary]]:
    return success(service.list_landmarks())
