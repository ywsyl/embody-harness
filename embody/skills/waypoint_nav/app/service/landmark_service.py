from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List

from pydantic import ValidationError

from app.model.models import Landmark, LandmarkSummary
from app.service.exceptions import LandmarkNotFoundError, ServiceError
from app.model.models import ErrorCode

logger = logging.getLogger(__name__)


class LandmarkService:
    def __init__(self, landmark_file: Path) -> None:
        self._landmark_file = landmark_file
        self._landmarks: Dict[str, Landmark] = {}
        self.load()

    def load(self) -> None:
        self._landmark_file.parent.mkdir(parents=True, exist_ok=True)
        if not self._landmark_file.exists():
            self._landmark_file.write_text("[]\n", encoding="utf-8")

        try:
            raw = json.loads(self._landmark_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                f"Invalid landmark JSON file: {self._landmark_file}",
            ) from exc

        if not isinstance(raw, list):
            raise ServiceError(ErrorCode.INTERNAL_ERROR, "Landmark JSON root must be a list.")

        landmarks: Dict[str, Landmark] = {}
        for item in raw:
            try:
                landmark = Landmark.model_validate(item)
            except AttributeError:
                landmark = Landmark.parse_obj(item)
            except ValidationError as exc:
                raise ServiceError(ErrorCode.INTERNAL_ERROR, "Invalid landmark entry.") from exc
            landmarks[landmark.name] = landmark

        self._landmarks = landmarks
        logger.info("Loaded %d landmarks from %s", len(self._landmarks), self._landmark_file)

    def list_landmarks(self) -> List[LandmarkSummary]:
        return [
            LandmarkSummary(name=landmark.name, description=landmark.description)
            for landmark in sorted(self._landmarks.values(), key=lambda item: item.name)
        ]

    def get_landmark(self, name: str) -> Landmark:
        landmark = self._landmarks.get(name)
        if landmark is None:
            raise LandmarkNotFoundError(name)
        return landmark
