from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from style_kb.config.models import AppConfig
from style_kb.models import Job
from style_kb.pipeline.paths import JobPaths
from style_kb.state.repository import StateRepository


@dataclass(slots=True)
class StageResult:
    output_files: list[Path] = field(default_factory=list)
    remote_refs: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StageContext:
    config: AppConfig
    repository: StateRepository
    job: Job
    paths: JobPaths


class Stage(ABC):
    name: str
    ordinal: int

    def input_files(self, context: StageContext) -> list[Path]:
        return []

    def output_files(self, context: StageContext) -> list[Path]:
        return []

    @abstractmethod
    def validate_outputs(self, context: StageContext) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(self, context: StageContext) -> StageResult:
        raise NotImplementedError
