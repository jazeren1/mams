from __future__ import annotations
import os
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
    @property
    def tmdb_token(self) -> str | None:
        """The configured TMDb bearer token, read from the environment
        variable named by `tmdb.token_env_var` -- never from the config
        file itself (same pattern as `plex.token_env_var`). `None` if no
        env var is configured or it is unset/empty."""
        env_var = self.raw.get("tmdb", {}).get("token_env_var")
        if not env_var:
            return None
        return os.environ.get(env_var) or None
    @property
    def tmdb_cache_ttl_seconds(self) -> int:
        return int(self.raw.get("tmdb", {}).get("cache_ttl_seconds", 7 * 24 * 3600))

def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping.")
    return AppConfig(raw=data)
