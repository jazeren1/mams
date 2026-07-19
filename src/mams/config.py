from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    @property
    def database_path(self) -> Path:
        return Path(self.raw["project"]["database_path"]).expanduser()
    @property
    def dry_run(self) -> bool:
        return bool(self.raw["project"].get("dry_run", True))
    @property
    def nas_categories(self) -> dict[str, str]:
        return dict(self.raw.get("nas", {}).get("categories", {}))

def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping.")
    return AppConfig(raw=data)
