"""Tests for destination.py: deterministic canonical destination naming."""

from __future__ import annotations

import pytest

from mams.destination import (
    DestinationError,
    episode_destination,
    movie_destination,
    sanitize_path_component,
)


def test_movie_title_and_year() -> None:
    plan = movie_destination(title="Alien", year=1979, extension=".mkv", destination_root="/Volumes/NASMedia/Movies")
    assert plan.directory == "/Volumes/NASMedia/Movies/Alien (1979)"
    assert plan.filename == "Alien (1979).mkv"
    assert plan.full_path == "/Volumes/NASMedia/Movies/Alien (1979)/Alien (1979).mkv"


def test_movie_with_confirmed_edition() -> None:
    plan = movie_destination(
        title="Blade Runner", year=1982, extension=".mkv", destination_root="/Volumes/NASMedia/Movies", edition="Final Cut"
    )
    assert plan.directory == "/Volumes/NASMedia/Movies/Blade Runner (1982)"
    assert plan.filename == "Blade Runner (1982) - Final Cut.mkv"


def test_movie_requires_positive_year() -> None:
    with pytest.raises(DestinationError):
        movie_destination(title="Alien", year=0, extension=".mkv", destination_root="/Volumes/NASMedia/Movies")


def test_movie_requires_non_empty_title() -> None:
    with pytest.raises(DestinationError):
        movie_destination(title="   ", year=1979, extension=".mkv", destination_root="/Volumes/NASMedia/Movies")


def test_movie_rejects_unsupported_extension() -> None:
    with pytest.raises(DestinationError):
        movie_destination(title="Alien", year=1979, extension=".exe", destination_root="/Volumes/NASMedia/Movies")


def test_movie_accepts_extension_without_leading_dot() -> None:
    plan = movie_destination(title="Alien", year=1979, extension="mkv", destination_root="/Volumes/NASMedia/Movies")
    assert plan.filename == "Alien (1979).mkv"


def test_tv_single_episode() -> None:
    plan = episode_destination(
        series_title="Breaking Bad", season_number=1, episode_numbers=(1,), extension=".mkv",
        destination_root="/Volumes/NASMedia/TV", episode_title="Pilot",
    )
    assert plan.directory == "/Volumes/NASMedia/TV/Breaking Bad/Season 01"
    assert plan.filename == "Breaking Bad - S01E01 - Pilot.mkv"


def test_tv_episode_without_title() -> None:
    plan = episode_destination(
        series_title="Breaking Bad", season_number=1, episode_numbers=(1,), extension=".mkv",
        destination_root="/Volumes/NASMedia/TV",
    )
    assert plan.filename == "Breaking Bad - S01E01.mkv"


def test_tv_multi_episode() -> None:
    plan = episode_destination(
        series_title="Breaking Bad", season_number=1, episode_numbers=(2, 3), extension=".mkv",
        destination_root="/Volumes/NASMedia/TV",
    )
    assert plan.filename == "Breaking Bad - S01E02-E03.mkv"


def test_tv_special() -> None:
    plan = episode_destination(
        series_title="Breaking Bad", season_number=0, episode_numbers=(1,), extension=".mkv",
        destination_root="/Volumes/NASMedia/TV", episode_title="Cook's Tour",
    )
    assert plan.directory == "/Volumes/NASMedia/TV/Breaking Bad/Season 00"
    assert plan.filename == "Breaking Bad - S00E01 - Cook's Tour.mkv"


def test_episode_requires_at_least_one_episode_number() -> None:
    with pytest.raises(DestinationError):
        episode_destination(
            series_title="Breaking Bad", season_number=1, episode_numbers=(), extension=".mkv",
            destination_root="/Volumes/NASMedia/TV",
        )


# --- sanitization -----------------------------------------------------------------


def test_punctuation_is_preserved_when_safe() -> None:
    assert sanitize_path_component("Amélie: A Story") == "Amélie: A Story"


def test_slashes_are_replaced() -> None:
    assert "/" not in sanitize_path_component("Fast/Furious")
    assert "\\" not in sanitize_path_component("A\\B")


def test_unicode_normalization_is_deterministic() -> None:
    # "e" + combining acute accent (NFD) vs precomposed "é" (NFC) --
    # both must sanitize to the same NFC string.
    nfd = "Amélie"
    nfc = "Amélie"
    assert sanitize_path_component(nfd) == sanitize_path_component(nfc)


def test_trailing_dots_and_spaces_are_stripped() -> None:
    assert sanitize_path_component("Alien.. ") == "Alien"


def test_control_characters_are_stripped() -> None:
    assert sanitize_path_component("Alien\x00\x01") == "Alien"


def test_empty_after_sanitization_raises() -> None:
    with pytest.raises(DestinationError):
        sanitize_path_component("   ...   ")


def test_dot_and_dotdot_are_rejected() -> None:
    with pytest.raises(DestinationError):
        sanitize_path_component(".")
    with pytest.raises(DestinationError):
        sanitize_path_component("..")


def test_reserved_windows_device_name_is_escaped() -> None:
    assert sanitize_path_component("CON") == "_CON"
    assert sanitize_path_component("con") == "_con"


def test_leading_dot_is_escaped_to_avoid_hidden_file() -> None:
    result = sanitize_path_component(".hidden")
    assert not result.startswith(".")


def test_path_traversal_cannot_survive_into_a_destination_plan() -> None:
    """A title containing slashes/dot-segments must never let the
    resulting path escape destination_root -- slashes are replaced before
    any path is built, so `../../etc/passwd` becomes one opaque directory
    name, not three real path segments."""
    from pathlib import PurePosixPath

    plan = movie_destination(
        title="../../etc/passwd", year=1999, extension=".mkv", destination_root="/Volumes/NASMedia/Movies"
    )
    root = PurePosixPath("/Volumes/NASMedia/Movies")
    directory = PurePosixPath(plan.directory)
    assert directory.parent == root  # exactly one segment below the root, no escape
    assert ".." not in directory.relative_to(root).parts
    assert plan.full_path.startswith("/Volumes/NASMedia/Movies/")
