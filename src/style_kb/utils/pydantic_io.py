from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from style_kb.utils.files import read_json, read_jsonl, write_json_atomic, write_jsonl_atomic

ModelT = TypeVar("ModelT", bound=BaseModel)


def read_model(path: Path, model_class: type[ModelT]) -> ModelT:
    return model_class.model_validate(read_json(path))


def read_models_jsonl(path: Path, model_class: type[ModelT]) -> list[ModelT]:
    return [model_class.model_validate(row) for row in read_jsonl(path)]


def write_model(path: Path, model: BaseModel) -> None:
    write_json_atomic(path, model.model_dump(mode="json"))


def write_models_jsonl(path: Path, models: list[BaseModel]) -> None:
    write_jsonl_atomic(path, [model.model_dump(mode="json") for model in models])


def write_mapping_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    write_jsonl_atomic(path, rows)

