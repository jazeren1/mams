"""Tests for the television parser (src/mams/identification._parse_television).

Pure function of IdentificationInput -- no database or filesystem
involved.
"""

from __future__ import annotations

from mams import identification as ident
from mams.identification import CandidateType, Confidence, IdentificationInput


def _input(
    *,
    filename: str,
    parent_directory: str = "/Volumes/tv/Carnivale",
    category: str = "tv",
    layout: str = "tv_series_folder",
    relative_path: str | None = None,
    extension: str = ".mkv",
    media_file_id: int = 1,
) -> IdentificationInput:
    return IdentificationInput(
        media_file_id=media_file_id,
        category=category,
        layout=layout,
        filename=filename,
        relative_path=relative_path or filename,
        parent_directory=parent_directory,
        extension=extension,
    )


# --- supported patterns ----------------------------------------------------------


def test_sxxexx_uppercase() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S01E02.mkv"))
    assert candidate.candidate_type == CandidateType.EPISODE
    assert candidate.parsed_series_title == "Carnivale"
    assert candidate.season_number == 1
    assert candidate.episode_number == 2
    assert candidate.episode_numbers == (2,)
    assert candidate.confidence == Confidence.HIGH


def test_sxxexx_lowercase() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale s01e02.mkv"))
    assert candidate.season_number == 1
    assert candidate.episode_number == 2


def test_sxxexx_multi_episode_back_to_back() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S01E02E03.mkv"))
    assert candidate.episode_numbers == (2, 3)
    assert candidate.episode_number == 2


def test_sxxexx_multi_episode_hyphenated() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S01E02-E03.mkv"))
    assert candidate.episode_numbers == (2, 3)


def test_nxnn_form() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale 1x02.mkv"))
    assert candidate.season_number == 1
    assert candidate.episode_number == 2


def test_season_episode_words_form() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale Season 01 Episode 02.mkv"))
    assert candidate.season_number == 1
    assert candidate.episode_number == 2


def test_season_episode_words_form_dot_separated() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale.Season.01.Episode.02.mkv"))
    assert candidate.season_number == 1
    assert candidate.episode_number == 2


# --- season-folder evidence -------------------------------------------------------


def test_season_folder_supplies_series_title_when_filename_lacks_one() -> None:
    candidate = ident._parse_television(
        _input(
            filename="S01E02.mkv",
            parent_directory="/Volumes/tv/Carnivale/Season 01",
            layout="tv_season_folder",
        )
    )
    assert candidate.parsed_series_title == "Carnivale"
    assert candidate.confidence == Confidence.MEDIUM


def test_season_folder_corroborates_filename_season_without_overriding() -> None:
    candidate = ident._parse_television(
        _input(
            filename="Carnivale S01E02.mkv",
            parent_directory="/Volumes/tv/Carnivale/Season 01",
            layout="tv_season_folder",
        )
    )
    assert candidate.season_number == 1
    assert candidate.confidence == Confidence.HIGH
    assert "conflict" not in candidate.evidence


def test_season_folder_does_not_supply_episode_number_alone() -> None:
    # A season folder gives a season number, never an episode number --
    # this file has no season/episode pattern in its own filename at all.
    candidate = ident._parse_television(
        _input(
            filename="Making Of.mkv",
            parent_directory="/Volumes/tv/Carnivale/Season 01",
            layout="tv_season_folder",
        )
    )
    assert candidate.season_number is None
    assert candidate.episode_number is None


# --- specials ----------------------------------------------------------------------


def test_season_zero_is_special() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S00E01.mkv"))
    assert candidate.candidate_type == CandidateType.SPECIAL
    assert candidate.season_number == 0
    assert candidate.special_type == "season_zero"


# --- extras --------------------------------------------------------------------------


def test_extra_keyword_behind_the_scenes() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale Behind The Scenes.mkv"))
    assert candidate.candidate_type == CandidateType.EXTRA
    assert candidate.special_type == "behind_the_scenes"
    assert candidate.season_number is None
    assert candidate.episode_number is None


def test_extra_keyword_deleted_scene() -> None:
    candidate = ident._parse_television(_input(filename="Deleted Scene 01.mkv"))
    assert candidate.candidate_type == CandidateType.EXTRA
    assert candidate.special_type == "deleted_scene"


def test_extra_without_folder_context_is_low_confidence() -> None:
    candidate = ident._parse_television(
        _input(filename="Trailer.mkv", parent_directory="/Volumes/tv", layout="unknown")
    )
    assert candidate.candidate_type == CandidateType.EXTRA
    assert candidate.confidence == Confidence.LOW


# --- episode title extraction --------------------------------------------------------


def test_episode_title_extracted_when_clearly_present() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S01E02 Milfay.mkv"))
    assert candidate.episode_title == "Milfay"


def test_no_episode_title_when_nothing_follows_the_pattern() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S01E02.mkv"))
    assert candidate.episode_title is None


def test_no_episode_title_when_only_technical_tokens_follow() -> None:
    candidate = ident._parse_television(_input(filename="Carnivale S01E02 1080p BluRay x264.mkv"))
    assert candidate.episode_title is None


# --- no fabrication ------------------------------------------------------------------


def test_no_season_episode_pattern_and_no_extra_keyword_yields_unknown() -> None:
    candidate = ident._parse_television(_input(filename="Some Random File.mkv"))
    assert candidate.candidate_type == CandidateType.UNKNOWN
    assert candidate.confidence == Confidence.UNKNOWN
    assert candidate.season_number is None
    assert candidate.episode_number is None
    assert candidate.episode_numbers == ()


def test_unknown_candidate_still_records_folder_context_in_evidence_only() -> None:
    candidate = ident._parse_television(
        _input(
            filename="Some Random File.mkv",
            parent_directory="/Volumes/tv/Carnivale/Season 01",
            layout="tv_season_folder",
        )
    )
    assert candidate.season_number is None
    assert candidate.evidence["folder_season"] == 1
    assert candidate.evidence["folder_series_title"] == "Carnivale"


# --- conflicting evidence --------------------------------------------------------------


def test_conflicting_season_between_filename_and_season_folder_is_low_confidence() -> None:
    candidate = ident._parse_television(
        _input(
            filename="Carnivale S02E05.mkv",
            parent_directory="/Volumes/tv/Carnivale/Season 01",
            layout="tv_season_folder",
        )
    )
    assert candidate.season_number == 2  # filename wins, never overridden
    assert candidate.confidence == Confidence.LOW
    assert "conflict" in candidate.evidence


# --- ambiguous / no series title -----------------------------------------------------


def test_no_series_title_anywhere_is_low_confidence() -> None:
    candidate = ident._parse_television(
        _input(filename="S01E02.mkv", parent_directory="/Volumes/tv", layout="unknown")
    )
    assert candidate.parsed_series_title is None
    assert candidate.confidence == Confidence.LOW


# --- determinism -----------------------------------------------------------------------


def test_parsing_is_deterministic() -> None:
    first = ident._parse_television(_input(filename="Carnivale S01E02 Milfay 1080p.mkv"))
    second = ident._parse_television(_input(filename="Carnivale S01E02 Milfay 1080p.mkv"))
    assert first == second
