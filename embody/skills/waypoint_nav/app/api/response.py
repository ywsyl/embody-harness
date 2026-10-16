from __future__ import annotations

from typing import Optional, TypeVar

from app.model.models import ApiResponse

T = TypeVar("T")


def success(data: Optional[T] = None) -> ApiResponse[T]:
    return ApiResponse(code=0, message="success", data=data)
