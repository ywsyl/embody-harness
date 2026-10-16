from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import deps
from app.api.landmarks import router as landmarks_router
from app.api.localization import router as localization_router
from app.api.navigation import router as navigation_router
from app.api.accompany import router as accompany_router
from app.model.models import ApiResponse, ErrorCode
from app.ros.ros_accompany import ROSAccompany
from app.ros.ros_localization import ROSLocalization
from app.ros.ros_navigation import ROSNavigation
from app.ros.ros_node import ROSAdapter
from app.service.accompany_service import AccompanyService
from app.service.exceptions import ServiceError
from app.service.landmark_service import LandmarkService
from app.service.localization_service import LocalizationService
from app.service.navigation_service import NavigationService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LANDMARK_FILE = BASE_DIR / "data" / "landmarks.json"
ros_adapter = ROSAdapter()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    deps.landmark_service = LandmarkService(LANDMARK_FILE)
    ros_adapter.start()
    deps.localization_service = LocalizationService(ROSLocalization(ros_adapter))
    deps.navigation_service = NavigationService(
        deps.landmark_service,
        ROSNavigation(ros_adapter),
    )
    deps.accompany_service = AccompanyService(ROSAccompany(ros_adapter))
    yield
    ros_adapter.stop()


app = FastAPI(
    title="Robot Navigation Service",
    version="1.0.0",
    description="HTTP robot capability API backed by ROS2 navigation topics.",
    lifespan=lifespan,
)

app.include_router(landmarks_router, prefix="/api/v1")
app.include_router(localization_router, prefix="/api/v1")
app.include_router(navigation_router, prefix="/api/v1")
app.include_router(accompany_router, prefix="/api/v1")


@app.get("/health", response_model=ApiResponse[Dict[str, Any]])
def health() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse(
        code=0,
        message="success",
        data={
            "service": "ok",
            "ros_available": ros_adapter.is_available,
            "ros_error": ros_adapter.startup_error,
            "ros_debug": ros_adapter.get_debug_state(),
        },
    )


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=ApiResponse(code=int(exc.code), message=exc.message, data=None).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ApiResponse(
            code=int(ErrorCode.VALIDATION_ERROR),
            message="Request validation failed.",
            data={"errors": exc.errors()},
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ApiResponse(
            code=int(ErrorCode.INTERNAL_ERROR),
            message="Internal server error.",
            data=None,
        ).model_dump(),
    )
