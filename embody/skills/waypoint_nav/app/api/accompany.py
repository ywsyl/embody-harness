from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_accompany_service
from app.api.response import success
from app.model.models import AccompanyStatus, ApiResponse, NavigationTask
from app.service.accompany_service import AccompanyService

router = APIRouter(prefix="/accompany", tags=["accompany"])


@router.post("/start", response_model=ApiResponse[NavigationTask])
def start_accompany(
    service: AccompanyService = Depends(get_accompany_service),
) -> ApiResponse[NavigationTask]:
    return success(service.start())


@router.post("/stop", response_model=ApiResponse[None], response_model_exclude_none=True)
def stop_accompany(
    service: AccompanyService = Depends(get_accompany_service),
) -> ApiResponse[None]:
    service.stop()
    return success()


@router.post("/end", response_model=ApiResponse[None], response_model_exclude_none=True)
def end_accompany(
    service: AccompanyService = Depends(get_accompany_service),
) -> ApiResponse[None]:
    service.stop()
    return success()


@router.get("/status", response_model=ApiResponse[AccompanyStatus])
def get_accompany_status(
    service: AccompanyService = Depends(get_accompany_service),
) -> ApiResponse[AccompanyStatus]:
    return success(service.get_status())
