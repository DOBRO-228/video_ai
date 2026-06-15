from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from style_kb.config.models import AppConfig
from style_kb.diagnostics import PipelineLogger
from style_kb.models import Job, StrictModel
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
    progress_callback: Callable[[str], None] | None = None
    pipeline_logger: PipelineLogger | None = None
    run_id: str | None = None
    openai_batch_enabled: bool = False


class StageValidationReport(StrictModel):
    stage_name: str
    valid_outputs: bool
    outputs_current: bool
    can_skip: bool
    reasons: list[str]
    missing_input_files: list[str]
    missing_output_files: list[str]
    stale_output_files: list[str]


class Stage(ABC):
    name: str
    ordinal: int

    def input_files(self, context: StageContext) -> list[Path]:
        return []

    def output_files(self, context: StageContext) -> list[Path]:
        return []

    def outputs_are_current(self, context: StageContext) -> bool:
        input_files = [path for path in self.input_files(context) if path.exists()]
        output_files = [path for path in self.output_files(context) if path.exists()]
        if not output_files:
            return False
        if not input_files:
            return True
        newest_input = max(path.stat().st_mtime for path in input_files)
        oldest_output = min(path.stat().st_mtime for path in output_files)
        return oldest_output >= newest_input

    def diagnose_outputs(self, context: StageContext) -> StageValidationReport:
        input_files = self.input_files(context)
        output_files = self.output_files(context)
        missing_input_files = [str(path) for path in input_files if not path.exists()]
        missing_output_files = [str(path) for path in output_files if not path.exists()]
        valid_outputs, validation_error = self._diagnose_validate_outputs(context)
        outputs_current, freshness_error = self._diagnose_outputs_current(context)
        stale_output_files = _stale_output_files(input_files, output_files)
        reasons = _stage_validation_reasons(
            valid_outputs=valid_outputs,
            outputs_current=outputs_current,
            missing_output_files=missing_output_files,
            stale_output_files=stale_output_files,
            validation_error=validation_error,
            freshness_error=freshness_error,
        )
        return StageValidationReport(
            stage_name=self.name,
            valid_outputs=valid_outputs,
            outputs_current=outputs_current,
            can_skip=valid_outputs and outputs_current,
            reasons=reasons,
            missing_input_files=missing_input_files,
            missing_output_files=missing_output_files,
            stale_output_files=stale_output_files,
        )

    def _diagnose_validate_outputs(self, context: StageContext) -> tuple[bool, str | None]:
        try:
            return self.validate_outputs(context), None
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    def _diagnose_outputs_current(self, context: StageContext) -> tuple[bool, str | None]:
        try:
            return self.outputs_are_current(context), None
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    @abstractmethod
    def validate_outputs(self, context: StageContext) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(self, context: StageContext) -> StageResult:
        raise NotImplementedError


def _stale_output_files(input_files: list[Path], output_files: list[Path]) -> list[str]:
    existing_inputs = [path for path in input_files if path.exists()]
    existing_outputs = [path for path in output_files if path.exists()]
    if not existing_inputs or not existing_outputs:
        return []
    newest_input_mtime = max(path.stat().st_mtime for path in existing_inputs)
    return [str(path) for path in existing_outputs if path.stat().st_mtime < newest_input_mtime]


def _stage_validation_reasons(
    *,
    valid_outputs: bool,
    outputs_current: bool,
    missing_output_files: list[str],
    stale_output_files: list[str],
    validation_error: str | None,
    freshness_error: str | None,
) -> list[str]:
    reasons = []
    if missing_output_files:
        reasons.append(f"missing output files: {len(missing_output_files)}")
    if stale_output_files:
        reasons.append(f"stale output files: {len(stale_output_files)}")
    if validation_error is not None:
        reasons.append(f"validate_outputs raised {validation_error}")
    elif not valid_outputs:
        reasons.append("validate_outputs returned false")
    if freshness_error is not None:
        reasons.append(f"outputs_are_current raised {freshness_error}")
    elif not outputs_current:
        reasons.append("outputs_are_current returned false")
    if not reasons and valid_outputs and outputs_current:
        reasons.append("outputs are valid and current")
    return reasons
