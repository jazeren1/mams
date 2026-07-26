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
    @property
    def ingest_incoming_roots(self) -> list[str]:
        return list(self.raw.get("ingest", {}).get("incoming_roots", []))
    @property
    def incoming_categories(self) -> dict[str, str]:
        """Synthesizes a category name for each configured incoming root
        so Incoming files flow through the same canonical inventory scan
        as NAS categories (see docs/DATABASE.md, "Incoming as a
        category") -- `"incoming"` for a single root, `"incoming_1"`,
        `"incoming_2"`, ... for more than one."""
        roots = self.ingest_incoming_roots
        if not roots:
            return {}
        if len(roots) == 1:
            return {"incoming": roots[0]}
        return {f"incoming_{i}": root for i, root in enumerate(roots, start=1)}
    @property
    def ingest_destination_categories(self) -> dict[str, str]:
        """Maps a logical destination kind (movie/tv/kids_movie/kids_tv)
        to the `nas.categories` key whose root path it resolves to."""
        ingest = self.raw.get("ingest", {})
        return {
            "movie": ingest.get("movie_destination_category", "movies"),
            "tv": ingest.get("tv_destination_category", "tv"),
            "kids_movie": ingest.get("kids_movie_destination_category", "kids_movies"),
            "kids_tv": ingest.get("kids_tv_destination_category", "kids_shows"),
        }
    @property
    def checksum_algorithm(self) -> str:
        return str(self.raw.get("execution", {}).get("checksum_algorithm", "sha256"))
    @property
    def copy_buffer_bytes(self) -> int:
        return int(self.raw.get("execution", {}).get("copy_buffer_bytes", 8 * 1024 * 1024))
    @property
    def execution_state_directory(self) -> Path | None:
        """Local scratch directory for Milestone 8's execution lock
        files -- `None` if unconfigured, which is itself a preflight
        failure (see execution.py); no implicit fallback path is chosen
        for something this safety-critical."""
        value = self.raw.get("execution", {}).get("state_directory")
        return Path(value).expanduser() if value else None
    @property
    def enable_plex_refresh(self) -> bool:
        return bool(self.raw.get("execution", {}).get("enable_plex_refresh", False))

def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping.")
    return AppConfig(raw=data)
