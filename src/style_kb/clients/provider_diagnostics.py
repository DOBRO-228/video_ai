from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any


class ProviderName(StrEnum):
    OPENAI = "openai"
    GEMINI = "gemini"
    SONIOX = "soniox"


@dataclass(slots=True)
class OperationTimer:
    started_at: str
    started_perf: float


@dataclass(slots=True)
class ProviderCallDiagnostics:
    provider: ProviderName
    model: str | None = None
    raw_output_path: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cached: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "model": self.model,
            "raw_output_path": self.raw_output_path,
            "request_id": self.request_id,
            "response_id": self.response_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "usage": self.usage,
            "cached": self.cached,
        }

    def event_data(self, *, operation: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        data = {"operation": operation, **self.to_dict()}
        if extra:
            data.update(extra)
        return data

    def with_updates(self, **updates: Any) -> "ProviderCallDiagnostics":
        return replace(self, **updates)

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any] | None,
        *,
        provider: ProviderName,
        model: str | None = None,
        raw_output_path: Path | str | None = None,
        usage: dict[str, int] | None = None,
        cached: bool | None = None,
    ) -> "ProviderCallDiagnostics":
        value = value or {}
        return cls(
            provider=_provider_name(value.get("provider"), default=provider),
            model=_string_or_none(value.get("model")) or model,
            raw_output_path=_string_or_none(value.get("raw_output_path")) or _path_string(raw_output_path),
            request_id=_string_or_none(value.get("request_id")),
            response_id=_string_or_none(value.get("response_id")),
            started_at=_string_or_none(value.get("started_at")),
            finished_at=_string_or_none(value.get("finished_at")),
            duration_seconds=_float_or_none(value.get("duration_seconds")),
            usage=dict(value.get("usage") or usage or {}),
            cached=value.get("cached") if value.get("cached") is not None else cached,
        )


def start_operation() -> OperationTimer:
    return OperationTimer(started_at=datetime.now(tz=UTC).isoformat(), started_perf=perf_counter())


def finish_operation(timer: OperationTimer) -> tuple[str, float]:
    return datetime.now(tz=UTC).isoformat(), perf_counter() - timer.started_perf


def openai_response_diagnostics(
    response: Any,
    raw_payload: dict[str, Any],
    *,
    timer: OperationTimer,
    raw_output_path: Path,
) -> ProviderCallDiagnostics:
    finished_at, duration_seconds = finish_operation(timer)
    return ProviderCallDiagnostics(
        provider=ProviderName.OPENAI,
        raw_output_path=str(raw_output_path),
        request_id=_string_or_none(getattr(response, "_request_id", None)),
        response_id=_string_or_none(raw_payload.get("id") or getattr(response, "id", None)),
        started_at=timer.started_at,
        finished_at=finished_at,
        duration_seconds=round(duration_seconds, 6),
    )


def cached_openai_diagnostics(
    cached_payload: dict[str, Any],
    raw_payload: dict[str, Any],
    *,
    model: str | None,
    usage: dict[str, int],
) -> ProviderCallDiagnostics:
    diagnostics = raw_payload.get("_style_kb_diagnostics")
    if isinstance(cached_payload.get("diagnostics"), dict):
        diagnostics = {**(diagnostics if isinstance(diagnostics, dict) else {}), **cached_payload["diagnostics"]}
    return ProviderCallDiagnostics.from_mapping(
        diagnostics if isinstance(diagnostics, dict) else {},
        provider=ProviderName.OPENAI,
        model=model,
        usage=usage,
    )


def gemini_response_diagnostics(
    response: Any,
    raw_payload: dict[str, Any],
    *,
    timer: OperationTimer,
    raw_output_path: Path,
    model: str | None,
) -> ProviderCallDiagnostics:
    finished_at, duration_seconds = finish_operation(timer)
    return ProviderCallDiagnostics(
        provider=ProviderName.GEMINI,
        model=_string_or_none(raw_payload.get("model_version") or raw_payload.get("modelVersion")) or model,
        raw_output_path=str(raw_output_path),
        request_id=_string_or_none(getattr(response, "_request_id", None)),
        response_id=_string_or_none(raw_payload.get("response_id") or raw_payload.get("responseId")),
        started_at=timer.started_at,
        finished_at=finished_at,
        duration_seconds=round(duration_seconds, 6),
    )


def cached_gemini_diagnostics(
    raw_payload: dict[str, Any],
    *,
    model: str | None,
    usage: dict[str, int],
) -> ProviderCallDiagnostics:
    diagnostics = raw_payload.get("_style_kb_diagnostics")
    return ProviderCallDiagnostics.from_mapping(
        diagnostics if isinstance(diagnostics, dict) else {},
        provider=ProviderName.GEMINI,
        model=model,
        usage=usage,
    )


def request_id_from_error(error: BaseException) -> str | None:
    for current in _exception_chain(error):
        request_id = _string_or_none(getattr(current, "request_id", None) or getattr(current, "_request_id", None))
        if request_id is not None:
            return request_id
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            continue
        for name in (
            "x-request-id",
            "request-id",
            "openai-request-id",
            "x-stainless-request-id",
            "x-goog-request-id",
            "x-google-request-id",
        ):
            header_value = _header_value(headers, name)
            if header_value is not None:
                return header_value
    return None


def response_status_from_error(error: BaseException) -> int | None:
    for current in _exception_chain(error):
        response = getattr(current, "response", None)
        status_code = (
            getattr(response, "status_code", None)
            or getattr(current, "status_code", None)
            or getattr(current, "code", None)
        )
        try:
            if status_code is not None:
                return int(status_code)
        except (TypeError, ValueError):
            continue
    return None


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _header_value(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name) or getter(name.lower()) or getter(name.upper())
    return str(value) if value is not None else None


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _path_string(value: Path | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _provider_name(value: object, *, default: ProviderName) -> ProviderName:
    text = _string_or_none(value)
    if text is None:
        return default
    try:
        return ProviderName(text)
    except ValueError:
        return default


def _float_or_none(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
