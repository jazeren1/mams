"""Tests for the shared parsing utilities in src/mams/identification.py
(year/edition/part/technical-token extraction). These are the building
blocks both the movie parser and the television parser use to strip
release-name noise down to a clean title; the movie/television parsers
themselves are tested in their own test files.
"""

from __future__ import annotations

from mams import identification as ident


# --- _clean_separators -------------------------------------------------------


def test_clean_separators_collapses_dots_and_underscores() -> None:
    assert ident._clean_separators("Movie.Title_Here") == "Movie Title Here"


def test_clean_separators_collapses_repeated_whitespace() -> None:
    assert ident._clean_separators("Movie   Title") == "Movie Title"


def test_clean_separators_strips_leading_trailing_dash_and_space() -> None:
    assert ident._clean_separators(" - Movie Title - ") == "Movie Title"


# --- _extract_year ------------------------------------------------------------


def test_extract_year_prefers_parenthesized_form() -> None:
    remainder, year, matched = ident._extract_year("Alien (1979) 1080p")
    assert year == 1979
    assert matched == "(1979)"
    assert "1979" not in remainder


def test_extract_year_bracketed_form() -> None:
    remainder, year, matched = ident._extract_year("Alien [1979]")
    assert year == 1979
    assert matched == "[1979]"


def test_extract_year_bare_form() -> None:
    remainder, year, matched = ident._extract_year("Alien 1979")
    assert year == 1979
    assert matched == "1979"


def test_extract_year_no_year_present() -> None:
    remainder, year, matched = ident._extract_year("Alien")
    assert year is None
    assert matched is None
    assert remainder == "Alien"


def test_extract_year_rejects_four_digit_values_outside_plausible_range() -> None:
    # 2160 (a resolution token) does not start with 19 or 20 -> not a year.
    remainder, year, matched = ident._extract_year("Movie 2160p")
    assert year is None
    assert matched is None


def test_extract_year_does_not_match_digits_embedded_in_a_longer_number() -> None:
    remainder, year, matched = ident._extract_year("Movie 199901")
    assert year is None


def test_extract_year_parenthesized_preferred_over_conflicting_bare_year() -> None:
    remainder, year, matched = ident._extract_year("Movie (1999) 2005")
    assert year == 1999
    assert matched == "(1999)"


# --- _extract_technical_tokens -------------------------------------------------


def test_extract_technical_tokens_removes_known_tokens() -> None:
    remainder, matched = ident._extract_technical_tokens("Movie 1080p BluRay x264 AAC")
    assert set(matched) == {"1080p", "BluRay", "x264", "AAC"}
    for token in matched:
        assert token not in remainder


def test_extract_technical_tokens_is_case_insensitive() -> None:
    remainder, matched = ident._extract_technical_tokens("Movie bluray HEVC")
    assert {m.lower() for m in matched} == {"bluray", "hevc"}


def test_extract_technical_tokens_preserves_order_of_appearance() -> None:
    remainder, matched = ident._extract_technical_tokens("Movie WEBRip x265 DTS")
    assert matched == ["WEBRip", "x265", "DTS"]


def test_extract_technical_tokens_none_present() -> None:
    remainder, matched = ident._extract_technical_tokens("Plain Movie Title")
    assert matched == []
    assert remainder == "Plain Movie Title"


def test_extract_technical_tokens_does_not_remove_substrings_of_real_words() -> None:
    # "DV" is a recognized token, but must not match inside an unrelated word.
    remainder, matched = ident._extract_technical_tokens("Movie Advocate")
    assert matched == []


# --- _extract_edition -----------------------------------------------------------


def test_extract_edition_directors_cut_with_apostrophe() -> None:
    remainder, edition = ident._extract_edition("Movie Director's Cut")
    assert edition == "Director's Cut"
    assert "Director" not in remainder


def test_extract_edition_directors_cut_without_apostrophe() -> None:
    remainder, edition = ident._extract_edition("Movie Directors Cut")
    assert edition == "Director's Cut"


def test_extract_edition_extended_edition() -> None:
    remainder, edition = ident._extract_edition("Movie Extended Edition")
    assert edition == "Extended Edition"


def test_extract_edition_case_insensitive() -> None:
    remainder, edition = ident._extract_edition("Movie UNRATED")
    assert edition == "Unrated"


def test_extract_edition_none_present() -> None:
    remainder, edition = ident._extract_edition("Plain Movie Title")
    assert edition is None
    assert remainder == "Plain Movie Title"


# --- _extract_part_number ----------------------------------------------------


def test_extract_part_number_part_form() -> None:
    remainder, number = ident._extract_part_number("Movie Part 2")
    assert number == 2


def test_extract_part_number_disc_form() -> None:
    remainder, number = ident._extract_part_number("Movie Disc 1")
    assert number == 1


def test_extract_part_number_cd_form() -> None:
    remainder, number = ident._extract_part_number("Movie CD2")
    assert number == 2


def test_extract_part_number_none_present() -> None:
    remainder, number = ident._extract_part_number("Plain Movie Title")
    assert number is None


# --- _clean_release_text (full pipeline) --------------------------------------


def test_clean_release_text_full_pipeline() -> None:
    cleaned = ident._clean_release_text("Alien.1979.Directors.Cut.1080p.BluRay.x264-GROUP")
    assert cleaned.year == 1979
    assert cleaned.edition == "Director's Cut"
    assert "1080p" in cleaned.removed_tokens
    assert "BluRay" in cleaned.removed_tokens
    assert "x264" in cleaned.removed_tokens
    assert cleaned.title.startswith("Alien")


def test_clean_release_text_no_noise() -> None:
    cleaned = ident._clean_release_text("Alien")
    assert cleaned.title == "Alien"
    assert cleaned.year is None
    assert cleaned.edition is None
    assert cleaned.part_number is None
    assert cleaned.removed_tokens == ()


# --- domain dataclasses --------------------------------------------------------


def test_identification_candidate_defaults() -> None:
    candidate = ident.IdentificationCandidate(
        media_file_id=1,
        candidate_type=ident.CandidateType.UNKNOWN,
        confidence=ident.Confidence.UNKNOWN,
        parser_version=ident.PARSER_VERSION,
        evidence={},
    )
    assert candidate.parsed_title is None
    assert candidate.episode_numbers == ()
    assert candidate.parser_version == 1
