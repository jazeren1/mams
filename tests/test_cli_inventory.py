from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mams.cli import build_parser, run_inventory_scan, run_mediainfo
from mams.config import load_config
from mams.mediainfo import MediaInfo, MediaInfoOutcome, MediaInfoProvider


def _write_config(tmp_path: Path, categories: dict[str, str]) -> Path:
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
                "nas": {"categories": categories},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_parser_accepts_inventory_scan_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "scan", "--json", "--output", "reports/custom.json"])

    assert args.command == "inventory"
    assert args.inventory_command == "scan"
    assert args.json is True
    assert args.output == "reports/custom.json"


def test_parser_defaults_for_inventory_scan() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "scan"])

    assert args.json is False
    assert args.output == "reports/library.json"
    assert args.metadata is False


def test_parser_accepts_metadata_flag() -> None:
    parser = build_parser()

    args = parser.parse_args(["inventory", "scan", "--metadata"])

    assert args.metadata is True


def test_parser_accepts_mediainfo_command() -> None:
    parser = build_parser()

    args = parser.parse_args(["mediainfo", "/path/to/movie.mkv"])

    assert args.command == "mediainfo"
    assert args.path == "/path/to/movie.mkv"
    assert args.json is False


def test_run_inventory_scan_writes_json_and_summary_reports(tmp_path: Path) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)

    config_path = _write_config(tmp_path, {"movies": str(movies_root)})
    config = load_config(config_path)

    output_path = tmp_path / "reports" / "library.json"
    report = run_inventory_scan(config, json_output=False, output=str(output_path))

    assert report.file_count == 1
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["file_count"] == 1

    summary_path = output_path.with_name("library-summary.txt")
    assert summary_path.exists()
    assert "files: 1" in summary_path.read_text(encoding="utf-8")


def test_run_inventory_scan_never_writes_into_scanned_categories(tmp_path: Path) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)
    before = sorted(p.name for p in movies_root.iterdir())

    config_path = _write_config(tmp_path, {"movies": str(movies_root)})
    config = load_config(config_path)

    run_inventory_scan(config, json_output=False, output=str(tmp_path / "reports" / "library.json"))

    after = sorted(p.name for p in movies_root.iterdir())
    assert before == after


def test_run_inventory_scan_without_metadata_flag_skips_mediainfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)

    def _fail_if_called(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        raise AssertionError("MediaInfoProvider.probe should not be called without --metadata")

    monkeypatch.setattr(MediaInfoProvider, "probe", _fail_if_called)

    config_path = _write_config(tmp_path, {"movies": str(movies_root)})
    config = load_config(config_path)

    report = run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), metadata=False
    )

    assert report.categories[0].files[0].media_info is None


def test_run_inventory_scan_with_metadata_flag_enriches_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movies_root = tmp_path / "Movies"
    movies_root.mkdir()
    (movies_root / "Movie (2001).mkv").write_bytes(b"\0" * 10)

    fake_info = MediaInfo(
        container="Matroska",
        duration_seconds=100.0,
        overall_bitrate=5000,
        video_tracks=(),
        audio_tracks=(),
        subtitle_tracks=(),
    )

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        return MediaInfoOutcome(media_info=fake_info, error=None)

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    config_path = _write_config(tmp_path, {"movies": str(movies_root)})
    config = load_config(config_path)

    report = run_inventory_scan(
        config, json_output=False, output=str(tmp_path / "reports" / "library.json"), metadata=True
    )

    assert report.categories[0].files[0].media_info is not None
    assert report.categories[0].files[0].media_info.container == "Matroska"


def test_run_mediainfo_prints_summary_for_successful_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_info = MediaInfo(
        container="Matroska",
        duration_seconds=100.0,
        overall_bitrate=5000,
        video_tracks=(),
        audio_tracks=(),
        subtitle_tracks=(),
    )

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        return MediaInfoOutcome(media_info=fake_info, error=None)

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    outcome = run_mediainfo(str(tmp_path / "movie.mkv"), json_output=False)

    assert outcome.ok is True
    assert outcome.media_info is not None
    assert outcome.media_info.container == "Matroska"


def test_run_mediainfo_reports_error_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        return MediaInfoOutcome(media_info=None, error="mediainfo executable not found on PATH")

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    outcome = run_mediainfo(str(tmp_path / "movie.mkv"), json_output=False)

    assert outcome.ok is False
    assert outcome.media_info is None


def test_run_mediainfo_does_not_modify_target_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"\0" * 10)
    before = (target.stat().st_size, target.stat().st_mtime_ns)

    fake_info = MediaInfo(
        container="Matroska",
        duration_seconds=None,
        overall_bitrate=None,
        video_tracks=(),
        audio_tracks=(),
        subtitle_tracks=(),
    )

    def _fake_probe(self: MediaInfoProvider, path: Path) -> MediaInfoOutcome:
        return MediaInfoOutcome(media_info=fake_info, error=None)

    monkeypatch.setattr(MediaInfoProvider, "probe", _fake_probe)

    run_mediainfo(str(target), json_output=False)

    after = (target.stat().st_size, target.stat().st_mtime_ns)
    assert before == after
