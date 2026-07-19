from mams.models import EpisodeIdentity, MovieIdentity

def test_movie_naming() -> None:
    assert MovieIdentity("District 9", 2009).canonical_stem == "District 9 (2009)"

def test_episode_naming() -> None:
    episode = EpisodeIdentity("Carnivàle", 1, 1, "Milfay")
    assert episode.canonical_stem == "Carnivàle - S01E01 - Milfay"
