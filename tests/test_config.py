"""Settings parsing and team matching."""

from conftest import MARKETING_ROLE_ID, ROCKET_LEAGUE_USER_IDS, VALORANT_ROLE_ID

from intake import config


def names(teams):
    return [team.name for team in teams]

def test_bad_number_falls_back_instead_of_crashing(monkeypatch):
    """A typo in .env must not stop the bot from starting."""
    monkeypatch.setenv("SOME_ID", "not-a-number")
    assert config.get_int("SOME_ID", 7) == 7


def test_whitespace_only_value_is_treated_as_unset(monkeypatch):
    """`int("  ")` raises, which used to crash the whole module at import."""
    monkeypatch.setenv("SOME_ID", "   ")
    assert config.get_int("SOME_ID", 7) == 7


def test_id_list_accepts_commas_spaces_and_mentions(monkeypatch):
    monkeypatch.setenv("SOME_IDS", "111111111111111111, <@222222222222222222> 333333333333333333")
    assert config.get_id_list("SOME_IDS") == [
        111111111111111111,
        222222222222222222,
        333333333333333333,
    ]


def test_id_list_removes_duplicates(monkeypatch):
    monkeypatch.setenv("SOME_IDS", "111111111111111111, 111111111111111111")
    assert config.get_id_list("SOME_IDS") == [111111111111111111]


def test_bool_accepts_the_usual_spellings(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SOME_FLAG", value)
        assert config.get_bool("SOME_FLAG") is True
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("SOME_FLAG", value)
        assert config.get_bool("SOME_FLAG") is False


def test_team_routing_is_read_from_the_environment():
    valorant = next(t for t in config.COMP_TEAMS if t.name == "Valorant")
    rocket_league = next(t for t in config.COMP_TEAMS if t.name == "Rocket League")
    marketing = next(t for t in config.OPS_TEAMS if t.name == "Marketing")

    assert valorant.role_id == VALORANT_ROLE_ID
    assert rocket_league.user_ids == ROCKET_LEAGUE_USER_IDS
    assert marketing.role_id == MARKETING_ROLE_ID


def test_teams_without_routing_are_reported_as_warnings():
    warnings = " ".join(config.configuration_warnings())
    assert "Halo" in warnings  # no role or users configured in the test env

def test_matches_a_plain_team_name():
    assert names(config.find_teams("Valorant", config.COMP_TEAMS)) == ["Valorant"]


def test_rocket_league_is_not_mistaken_for_league_of_legends():
    """The old substring matching counted "Rocket League" as both teams."""
    assert names(config.find_teams("Rocket League", config.COMP_TEAMS)) == ["Rocket League"]


def test_both_leagues_are_found_when_both_are_named():
    found = names(config.find_teams("League of Legends and Rocket League", config.COMP_TEAMS))
    assert set(found) == {"League of Legends", "Rocket League"}


def test_common_shorthand_is_understood():
    found = names(config.find_teams("cs2, val and smash", config.COMP_TEAMS))
    assert set(found) == {"Counter-Strike 2", "Valorant", "Super Smash Bros."}


def test_short_aliases_do_not_match_inside_other_words():
    """"cs" must not fire on "docs", and "rl" must not fire on "world"."""
    assert config.find_teams("see the docs in our world channel", config.COMP_TEAMS) == []


def test_punctuation_in_a_team_name_is_handled():
    assert names(config.find_teams("osu!", config.COMP_TEAMS)) == ["osu!"]
    assert names(config.find_teams("osu", config.COMP_TEAMS)) == ["osu!"]


def test_empty_answer_matches_nothing():
    assert config.find_teams("", config.COMP_TEAMS) == []


def test_a_team_is_only_returned_once():
    found = names(config.find_teams("Valorant, valorant, val", config.COMP_TEAMS))
    assert found == ["Valorant"]
