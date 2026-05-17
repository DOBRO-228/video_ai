from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    ensure_parent(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def write_text_atomic(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    write_bytes_atomic(path, payload.encode(encoding))


def append_text(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    ensure_parent(path)
    with path.open("a", encoding=encoding) as handle:
        handle.write(payload)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_jsonl_atomic(path: Path, rows: Iterable[Any]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    write_text_atomic(path, text)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def copy_file_atomic(source: Path, destination: Path) -> None:
    write_bytes_atomic(destination, source.read_bytes())
