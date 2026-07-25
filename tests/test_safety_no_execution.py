"""Safety tests (Milestone 7C): proves the full post-scan workflow --
identify evaluate, manual override, resolve evaluate (against a fake
provider), ingest plan, approve, and the execution-readiness audit --
never mutates the filesystem or spawns a subprocess.

No filesystem executor exists anywhere in this codebase; this is the
automated guarantee that stays true as the codebase grows, not just a
one-time manual observation from the Milestone 7B/7C validation runs.

`Path.rename/replace`, `shutil.move/copy/copy2`, `os.rename/replace/
remove/unlink`, and `subprocess.run/Popen` are monkeypatched to raise
`AssertionError` unconditionally for this phase of the workflow -- nothing
in identify/resolve/ingest/readiness code legitimately calls any of them,
so an unconditional raise is a precise proof of absence, not a broad
workaround.

`Path.mkdir` needs a narrower guard: `db.connect()` legitimately calls
`path.parent.mkdir(parents=True, exist_ok=True)` on every single CLI
command to ensure the local scratch database directory exists (pure local
bookkeeping, never a NAS path) -- by the time the safety patches are
applied (after the initial scan step, which already created that
directory), every legitimate mkdir target already exists. The patch below
allows an `exist_ok`-style call on an already-existing directory to pass
through as a no-op, and raises only if a call would actually create a new
directory -- which is exactly "no destination directory is created," this
milestone's own safety constraint, verified precisely rather than by
special-casing specific paths.

The scan step itself (report/database bookkeeping directories) runs
before any patch is applied, exactly like every other CLI test in this
suite -- this file only guards the ingest workflow this milestone is
actually about.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from mams import resolution_service
from mams.cli import (
    run_identify_evaluate,
    run_identify_override,
    run_ingest_approve,
    run_ingest_audit,
    run_ingest_plan,
    run_inventory_scan,
    run_resolve_evaluate,
)
from mams.config import load_config
from mams.db import connect
from mams.tmdb import MovieResult


def _write_config(tmp_path: Path, *, incoming_root: Path, nas_root: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "MAMS Test",
                    "database_path": str(tmp_path / "database" / "mams.db"),
                    "log_level": "INFO",
                    "dry_run": True,
                },
                "nas": {"categories": {"movies": str(nas_root / "Movies")}},
                "ingest": {
                    "incoming_roots": [str(incoming_root)],
                    "movie_destination_category": "movies",
                    "tv_destination_category": "tv",
                    "kids_movie_destination_category": "kids_movies",
                    "kids_tv_destination_category": "kids_shows",
                },
                "tmdb": {"token_env_var": "TMDB_API_TOKEN"},
            }
        ),
        encoding="utf-8",
    )
    return config_path


class FakeProvider:
    """Stands in for resolution_service.build_provider's real TMDbClient --
    no real network call happens in this file (see test_live_tmdb.py for
    the real-API tests)."""

    def search_movie(self, title: str, year: int | None = None) -> list[MovieResult]:
        return [
            MovieResult(
                provider_id=348, title="Alien", original_title="Alien", release_year=1979,
                original_language="en", popularity=50.0,
            )
        ]

    def get_movie(self, provider_id: int) -> MovieResult | None:
        return None

    def search_tv(self, series_title: str) -> list[object]:
        return []

    def get_tv_series(self, provider_id: int) -> object | None:
        return None

    def get_tv_episode(self, series_id: int, season_number: int, episode_number: int) -> object | None:
        return None


def _forbid(name: str):  # type: ignore[no-untyped-def]
    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name} must never be called during the post-scan ingest workflow")

    return _raise


def _guarded_mkdir(self: Path, *args: object, **kwargs: object) -> None:
    if self.exists():
        return None  # exist_ok-style no-op on an already-bookkept directory
    raise AssertionError(f"Path.mkdir must never create a new directory during the post-scan ingest workflow: {self}")


def _mark_media_file_healthy(config: object, media_file_id: int) -> None:
    """The real inventory scan never invokes MediaInfo unless --metadata
    is passed, so directly simulate a completed, healthy probe -- the
    same approach test_cli_ingest.py/test_ingest_service.py already use.
    A plain database write, not a filesystem mutation."""
    from mams.config import AppConfig

    assert isinstance(config, AppConfig)
    connection = connect(config.database_path)
    try:
        connection.execute(
            "UPDATE media_files SET duration_seconds = 7020.0, media_info_probed_at = '2024-01-01T00:00:00', "
            "container = 'Matroska' WHERE id = ?",
            (media_file_id,),
        )
        connection.execute("INSERT INTO video_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
        connection.execute("INSERT INTO audio_tracks (media_file_id, track_index) VALUES (?, 0)", (media_file_id,))
        connection.commit()
    finally:
        connection.close()


def _apply_safety_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "mkdir", _guarded_mkdir)
    monkeypatch.setattr(Path, "rename", _forbid("Path.rename"))
    monkeypatch.setattr(Path, "replace", _forbid("Path.replace"))
    monkeypatch.setattr(shutil, "move", _forbid("shutil.move"))
    monkeypatch.setattr(shutil, "copy", _forbid("shutil.copy"))
    monkeypatch.setattr(shutil, "copy2", _forbid("shutil.copy2"))
    monkeypatch.setattr(os, "rename", _forbid("os.rename"))
    monkeypatch.setattr(os, "replace", _forbid("os.replace"))
    monkeypatch.setattr(os, "remove", _forbid("os.remove"))
    monkeypatch.setattr(os, "unlink", _forbid("os.unlink"))
    monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen"))


def test_full_workflow_never_mutates_the_filesystem_or_spawns_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    (incoming_root / "Alien.mkv").write_bytes(b"\0" * 1_000_000)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))

    # Scan legitimately creates local report/database bookkeeping
    # directories (never a NAS path) -- runs before the safety patches.
    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))
    _mark_media_file_healthy(config, 1)
    capsys.readouterr()

    _apply_safety_patches(monkeypatch)

    result = run_identify_evaluate(config)
    assert result.created == 1

    override = run_identify_override(config, 1, candidate_type="MOVIE", title="Alien", year=1979)
    assert override is not None

    monkeypatch.setenv("TMDB_API_TOKEN", "fake-token")
    monkeypatch.setattr(resolution_service, "build_provider", lambda cfg, conn: FakeProvider())
    attempts = run_resolve_evaluate(config)
    assert attempts and attempts[0].status == "RESOLVED"

    plan = run_ingest_plan(config, 1, destination_category="movie")
    assert plan is not None
    assert plan.status == "READY_FOR_REVIEW"

    approved = run_ingest_approve(config, plan.id)
    assert approved is not None
    assert approved.status == "APPROVED"

    audit_result = run_ingest_audit(config, plan.id)
    assert audit_result is not None

    # Direct filesystem confirmation, independent of the patches above:
    # no destination directory or file was ever created, and the source
    # is still present, byte-for-byte where it started.
    assert not (tmp_path / "NAS").exists()
    assert (incoming_root / "Alien.mkv").exists()
    assert (incoming_root / "Alien.mkv").stat().st_size == 1_000_000


def test_override_and_clear_override_do_not_touch_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mams.cli import run_identify_clear_override, run_identify_show_effective

    incoming_root = tmp_path / "Incoming"
    incoming_root.mkdir(parents=True)
    (incoming_root / "Alien.mkv").write_bytes(b"\0" * 1_000_000)
    config = load_config(_write_config(tmp_path, incoming_root=incoming_root, nas_root=tmp_path / "NAS"))
    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))
    run_identify_evaluate(config)
    capsys.readouterr()

    _apply_safety_patches(monkeypatch)

    run_identify_override(config, 1, candidate_type="MOVIE", title="Alien", year=1979)
    run_identify_show_effective(config, 1)
    run_identify_clear_override(config, 1)
    run_identify_show_effective(config, 1)

    assert (incoming_root / "Alien.mkv").exists()
