from __future__ import annotations

from typing import Optional

from app.service.accompany_service import AccompanyService
from app.service.landmark_service import LandmarkService
from app.service.localization_service import LocalizationService
from app.service.navigation_service import NavigationService


landmark_service: Optional[LandmarkService] = None
localization_service: Optional[LocalizationService] = None
navigation_service: Optional[NavigationService] = None
accompany_service: Optional[AccompanyService] = None


def get_landmark_service() -> LandmarkService:
    if landmark_service is None:
        raise RuntimeError("LandmarkService has not been initialized.")
    return landmark_service


def get_localization_service() -> LocalizationService:
    if localization_service is None:
        raise RuntimeError("LocalizationService has not been initialized.")
    return localization_service


def get_navigation_service() -> NavigationService:
    if navigation_service is None:
        raise RuntimeError("NavigationService has not been initialized.")
    return navigation_service


def get_accompany_service() -> AccompanyService:
    if accompany_service is None:
        raise RuntimeError("AccompanyService has not been initialized.")
    return accompany_service
