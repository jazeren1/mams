"""Tests for src/mams/identification.evaluate_file() -- the classification
entrypoint that decides which parser (movie/TV) runs for a given
category/layout, and the fallback for files whose category/layout give no
signal at all.
"""

from __future__ import annotations

from mams.identification import CandidateType, Confidence, IdentificationInput, evaluate_file


def _input(
    *,
    filename: str,
    category: str,
    layout: str,
    parent_directory: str = "/Volumes/media",
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


# --- routing by category/layout -----------------------------------------------


def test_movie_category_routes_to_movie_parser() -> None:
    candidate = evaluate_file(_input(filename="Alien (1979).mkv", category="movies", layout="movie_flat"))
    assert candidate.candidate_type == CandidateType.MOVIE
    assert candidate.confidence == Confidence.HIGH


def test_tv_category_routes_to_television_parser() -> None:
    candidate = evaluate_file(
        _input(
            filename="Carnivale S01E02.mkv",
            category="tv",
            layout="tv_series_folder",
            parent_directory="/Volumes/tv/Carnivale",
        )
    )
    assert candidate.candidate_type == CandidateType.EPISODE
    assert candidate.confidence == Confidence.HIGH


def test_movie_layout_routes_to_movie_parser_even_with_unrecognized_category() -> None:
    candidate = evaluate_file(_input(filename="Alien (1979).mkv", category="specials", layout="movie_flat"))
    assert candidate.candidate_type == CandidateType.MOVIE


def test_tv_layout_routes_to_television_parser_even_with_unrecognized_category() -> None:
    candidate = evaluate_file(
        _input(
            filename="Carnivale S01E02.mkv",
            category="specials",
            layout="tv_series_folder",
            parent_directory="/Volumes/specials/Carnivale",
        )
    )
    assert candidate.candidate_type == CandidateType.EPISODE


# --- confidence rubric summary (cross-cutting) --------------------------------


def test_high_confidence_movie() -> None:
    candidate = evaluate_file(_input(filename="Alien (1979).mkv", category="movies", layout="movie_flat"))
    assert candidate.confidence == Confidence.HIGH


def test_medium_confidence_movie_without_year() -> None:
    candidate = evaluate_file(_input(filename="Alien.mkv", category="movies", layout="movie_flat"))
    assert candidate.confidence == Confidence.MEDIUM


def test_high_confidence_episode() -> None:
    candidate = evaluate_file(
        _input(
            filename="Carnivale S01E02.mkv",
            category="tv",
            layout="tv_series_folder",
            parent_directory="/Volumes/tv/Carnivale",
        )
    )
    assert candidate.confidence == Confidence.HIGH


def test_low_ambiguous_candidate() -> None:
    candidate = evaluate_file(_input(filename="123.mkv", category="movies", layout="movie_flat"))
    assert candidate.confidence == Confidence.LOW


def test_unknown_candidate() -> None:
    candidate = evaluate_file(
        _input(filename="readme.mkv", category="unsorted", layout="unknown", parent_directory="/Volumes/unsorted")
    )
    assert candidate.candidate_type == CandidateType.UNKNOWN
    assert candidate.confidence == Confidence.UNKNOWN


def test_deterministic_confidence_across_repeated_calls() -> None:
    make = lambda: evaluate_file(_input(filename="Alien (1979) 1080p BluRay x264.mkv", category="movies", layout="movie_flat"))  # noqa: E731
    assert make() == make()


# --- directory placement alone never yields HIGH -------------------------------


def test_movie_directory_placement_with_unparseable_name_is_not_high() -> None:
    candidate = evaluate_file(_input(filename="1080p.mkv", category="movies", layout="movie_flat"))
    assert candidate.confidence != Confidence.HIGH
    assert candidate.confidence == Confidence.UNKNOWN


def test_tv_directory_placement_with_unparseable_name_is_not_high() -> None:
    candidate = evaluate_file(
        _input(
            filename="Some Random File.mkv",
            category="tv",
            layout="tv_series_folder",
            parent_directory="/Volumes/tv/Carnivale",
        )
    )
    assert candidate.confidence != Confidence.HIGH


# --- unclassified fallback: category/layout give no signal ----------------------


def test_unclassified_falls_back_to_television_pattern_evidence() -> None:
    candidate = evaluate_file(
        _input(
            filename="Carnivale S01E02.mkv",
            category="unsorted",
            layout="unknown",
            parent_directory="/Volumes/unsorted",
        )
    )
    assert candidate.candidate_type == CandidateType.EPISODE


def test_unclassified_falls_back_to_movie_pattern_evidence() -> None:
    candidate = evaluate_file(
        _input(filename="Alien (1979).mkv", category="unsorted", layout="unknown", parent_directory="/Volumes/unsorted")
    )
    assert candidate.candidate_type == CandidateType.MOVIE


def test_unclassified_with_no_pattern_evidence_anywhere_is_unknown() -> None:
    candidate = evaluate_file(
        _input(filename="readme.mkv", category="unsorted", layout="unknown", parent_directory="/Volumes/unsorted")
    )
    assert candidate.candidate_type == CandidateType.UNKNOWN
    assert candidate.confidence == Confidence.UNKNOWN
