"""Tests for the movie parser (src/mams/identification._parse_movie).

Pure function of IdentificationInput -- no database or filesystem
involved. Confidence-level assertions here cover the movie-specific rules
from the module's confidence rubric; cross-cutting classification tests
(which parser runs for which category/layout) live in
test_identification_classification.py.
"""

from __future__ import annotations

from mams import identification as ident
from mams.identification import CandidateType, Confidence, IdentificationInput


def _input(
    *,
    filename: str,
    parent_directory: str = "/Volumes/movies",
    category: str = "movies",
    layout: str = "movie_flat",
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


# --- title/year forms ----------------------------------------------------------


def test_parenthesized_year() -> None:
    candidate = ident._parse_movie(_input(filename="Alien (1979).mkv"))
    assert candidate.candidate_type == CandidateType.MOVIE
    assert candidate.parsed_title == "Alien"
    assert candidate.parsed_year == 1979
    assert candidate.confidence == Confidence.HIGH
    assert candidate.evidence["title_source"] == "filename"
    assert candidate.evidence["year_source"] == "filename"


def test_bracketed_year() -> None:
    candidate = ident._parse_movie(_input(filename="Alien [1979].mkv"))
    assert candidate.parsed_title == "Alien"
    assert candidate.parsed_year == 1979
    assert candidate.confidence == Confidence.HIGH


def test_plain_year() -> None:
    candidate = ident._parse_movie(_input(filename="Alien 1979.mkv"))
    assert candidate.parsed_title == "Alien"
    assert candidate.parsed_year == 1979
    assert candidate.confidence == Confidence.HIGH


def test_title_without_year() -> None:
    candidate = ident._parse_movie(_input(filename="Alien.mkv", parent_directory="/Volumes/movies"))
    assert candidate.parsed_title == "Alien"
    assert candidate.parsed_year is None
    assert candidate.confidence == Confidence.MEDIUM


def test_year_falls_back_to_parent_directory_when_filename_has_none() -> None:
    candidate = ident._parse_movie(
        _input(filename="movie.mkv", parent_directory="/Volumes/movies/Alien (1979)", layout="movie_folder")
    )
    assert candidate.parsed_title == "Alien"
    assert candidate.parsed_year == 1979
    assert candidate.evidence["title_source"] == "parent_directory"
    assert candidate.evidence["year_source"] == "parent_directory"
    assert candidate.confidence == Confidence.HIGH


# --- technical token removal --------------------------------------------------


def test_technical_tokens_removed_from_title_and_recorded_in_evidence() -> None:
    candidate = ident._parse_movie(_input(filename="Alien.1979.1080p.BluRay.x264.mkv"))
    assert candidate.parsed_title == "Alien"
    assert candidate.parsed_year == 1979
    removed = candidate.evidence["removed_tokens"]
    assert "1080p" in removed
    assert "BluRay" in removed
    assert "x264" in removed


# --- edition extraction ---------------------------------------------------------


def test_edition_directors_cut_extracted_and_stripped_from_title() -> None:
    candidate = ident._parse_movie(_input(filename="Alien (1979) Director's Cut.mkv"))
    assert candidate.parsed_title == "Alien"
    assert candidate.edition == "Director's Cut"


def test_edition_extended_edition() -> None:
    candidate = ident._parse_movie(_input(filename="Kill Bill (2003) Extended Edition.mkv"))
    assert candidate.edition == "Extended Edition"


# --- part/disc extraction --------------------------------------------------------


def test_part_number_extracted() -> None:
    candidate = ident._parse_movie(_input(filename="Kill Bill Part 2 (2004).mkv"))
    assert candidate.parsed_title == "Kill Bill"
    assert candidate.part_number == 2


def test_disc_number_extracted() -> None:
    candidate = ident._parse_movie(_input(filename="Movie Disc 1 (2001).mkv"))
    assert candidate.part_number == 1


# --- false four-digit year prevention -------------------------------------------


def test_resolution_token_not_mistaken_for_year() -> None:
    candidate = ident._parse_movie(_input(filename="Alien 2160p.mkv"))
    assert candidate.parsed_year is None
    assert candidate.confidence == Confidence.MEDIUM


def test_no_year_anywhere_gives_none_not_a_guess() -> None:
    candidate = ident._parse_movie(_input(filename="Some Movie.mkv", parent_directory="/Volumes/movies"))
    assert candidate.parsed_year is None


# --- conflicting evidence ---------------------------------------------------------


def test_conflicting_years_between_filename_and_folder_is_low_confidence() -> None:
    candidate = ident._parse_movie(
        _input(
            filename="Alien (1979).mkv",
            parent_directory="/Volumes/movies/Prometheus (2012)",
            layout="movie_folder",
        )
    )
    assert candidate.confidence == Confidence.LOW
    assert "conflict" in candidate.evidence


def test_conflicting_titles_between_filename_and_folder_is_low_confidence() -> None:
    candidate = ident._parse_movie(
        _input(filename="Alien.mkv", parent_directory="/Volumes/movies/Terminator", layout="movie_folder")
    )
    assert candidate.confidence == Confidence.LOW
    assert "conflict" in candidate.evidence


def test_similar_titles_between_filename_and_folder_do_not_conflict() -> None:
    candidate = ident._parse_movie(
        _input(filename="Alien (1979).mkv", parent_directory="/Volumes/movies/Alien", layout="movie_folder")
    )
    assert candidate.confidence == Confidence.HIGH
    assert "conflict" not in candidate.evidence


# --- ambiguous / unknown ----------------------------------------------------------


def test_ambiguous_non_year_digit_title_is_low_confidence() -> None:
    # "123" is not a plausible year (doesn't start with 19/20), so it
    # survives as the "title" -- a bare number too weak to be meaningful.
    candidate = ident._parse_movie(_input(filename="123.mkv", parent_directory="/Volumes/movies"))
    assert candidate.confidence == Confidence.LOW


def test_bare_year_with_no_title_yields_unknown() -> None:
    # The whole filename is consumed as the year; nothing is left as a title.
    candidate = ident._parse_movie(_input(filename="1979.mkv", parent_directory="/Volumes/movies"))
    assert candidate.parsed_title is None
    assert candidate.confidence == Confidence.UNKNOWN


def test_only_technical_tokens_yields_unknown() -> None:
    candidate = ident._parse_movie(_input(filename="1080p.mkv", parent_directory="/Volumes/movies"))
    assert candidate.parsed_title is None
    assert candidate.confidence == Confidence.UNKNOWN


# --- determinism -------------------------------------------------------------------


def test_parsing_is_deterministic() -> None:
    first = ident._parse_movie(_input(filename="Alien (1979) 1080p BluRay x264.mkv"))
    second = ident._parse_movie(_input(filename="Alien (1979) 1080p BluRay x264.mkv"))
    assert first == second
