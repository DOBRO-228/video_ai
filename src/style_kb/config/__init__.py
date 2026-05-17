from __future__ import annotations

from pathlib import Path

import yaml

from style_kb.config.models import AppConfig
from style_kb.errors import ConfigError


def default_config_path() -> Path:
    return Path(__file__).resolve().parent / "default.yaml"


def load_default_config() -> AppConfig:
    path = default_config_path()
    if not path.exists():
        raise ConfigError(f"default config not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ConfigError(f"default config has invalid shape: {path}")
    return AppConfig.model_validate(payload)

