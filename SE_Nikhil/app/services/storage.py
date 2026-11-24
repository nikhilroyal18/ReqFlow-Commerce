from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

from app.schemas import Project


class ProjectStore:
    def __init__(self) -> None:
        self.root = Path(os.getenv("APP_DATA_DIR", "data"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def save(self, project: Project) -> None:
        with self._lock:
            (self.root / f"{project.id}.json").write_text(
                project.model_dump_json(indent=2), encoding="utf-8"
            )

    def get(self, project_id: str) -> Project:
        path = self.root / f"{project_id}.json"
        if not path.exists():
            raise KeyError(project_id)
        return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[Project]:
        with self._lock:
            projects = [Project.model_validate_json(path.read_text(encoding="utf-8")) for path in self.root.glob("*.json")]
        return sorted(projects, key=lambda item: item.created_at, reverse=True)

    def delete(self, project_id: str) -> None:
        path = self.root / f"{project_id}.json"
        with self._lock:
            if not path.exists():
                raise KeyError(project_id)
            path.unlink()

    def export(self, project: Project) -> dict:
        return json.loads(project.model_dump_json())
