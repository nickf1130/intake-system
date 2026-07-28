"""Routing a response to the right people, and closing a ticket safely."""

import types

from conftest import (
    COMP_ROLE_ID,
    MARKETING_ROLE_ID,
    ROCKET_LEAGUE_USER_IDS,
    SECRETARY_ROLE_ID,
    SUBMITTER_ID,
    VALORANT_ROLE_ID,
    FakeLogChannel,
    FakeTicketChannel,
    FakeUser,
    close_and_settle,
    make_response,
    run,
)

from intake import tickets

def test_a_plain_id_is_recognised():
    assert tickets.parse_discord_id("123456789012345678") == 123456789012345678


def test_a_mention_is_recognised():
    assert tickets.parse_discord_id("<@123456789012345678>") == 123456789012345678
    assert tickets.parse_discord_id("<@!123456789012345678>") == 123456789012345678


def test_a_gamertag_containing_digits_is_not_an_id():
    """The old check just stripped non-digits, so this became a bogus mention."""
    assert tickets.parse_discord_id("sniper12345678901234") is None


def test_a_phone_number_is_not_an_id():
    assert tickets.parse_discord_id("585-555-0142") is None


def test_too_few_digits_is_not_an_id():
    assert tickets.parse_discord_id("12345") is None

def test_competitive_and_operations_are_recognised():
    assert tickets.resolve_department("Competitive")[0] == "Competitive"
    assert tickets.resolve_department("operations")[0] == "Operations"


def test_an_unrecognised_branch_falls_back_to_general():
    department, role_id = tickets.resolve_department("no idea")
    assert department == "General"
    assert role_id is None


def test_officers_route_to_the_secretary():
    """president / vp / treasurer ping the secretary, as the form explains."""
    assert tickets.resolve_ping_role("the treasurer", None) == SECRETARY_ROLE_ID


def test_an_empty_approver_answer_keeps_the_department_role():
    assert tickets.resolve_ping_role("", COMP_ROLE_ID) == COMP_ROLE_ID

def test_a_typical_response_routes_to_the_named_teams():
    request = tickets.build_request(make_response())

    assert request.department == "Competitive"
    assert request.submitter_id == SUBMITTER_ID
    assert set(request.team_names) == {"Valorant", "Rocket League"}
    assert VALORANT_ROLE_ID in request.ping_role_ids
    assert COMP_ROLE_ID in request.ping_role_ids
    assert set(ROCKET_LEAGUE_USER_IDS).issubset(request.ping_user_ids)


def test_operations_responses_route_to_operations_teams():
    response = make_response(
        answers={
            "What branch do you require help from (operations/competitive)": "Operations",
            "What Competitive Team(s) do you need support from?": "",
            "What Operations Teams do you need support from?": "Marketing",
        }
    )
    request = tickets.build_request(response)
    assert request.department == "Operations"
    assert MARKETING_ROLE_ID in request.ping_role_ids


def test_the_submitter_can_see_their_own_ticket():
    """They used to be pinged into a channel they had no access to."""
    request = tickets.build_request(make_response())
    assert SUBMITTER_ID in request.member_ids


def test_the_submitter_is_pinged_first():
    mentions = tickets.build_request(make_response()).build_mentions()
    assert mentions.startswith(f"<@{SUBMITTER_ID}>")


def test_nobody_is_pinged_twice():
    mentions = tickets.build_request(make_response()).build_mentions().split()
    assert len(mentions) == len(set(mentions))


def test_a_response_with_no_identifiable_submitter_still_works():
    response = make_response(
        answers={"Discord ID": "", "Discord username": "", "Your name": "", "Email address": ""}
    )
    request = tickets.build_request(response)
    assert request.submitter_id is None
    assert request.submitter_label == "Unknown submitter"

def test_channel_names_are_valid_and_readable():
    name = tickets.build_channel_name(tickets.build_request(make_response(7)))
    assert name == "intake-competitive-jordanr-7"


def test_channel_names_contain_no_mention_syntax():
    """Names used to come out as "intake-competitive-<@123456789012345678>"."""
    response = make_response(answers={"Discord username": "", "Your name": ""})
    name = tickets.build_channel_name(tickets.build_request(response))
    assert "<" not in name and "@" not in name


def test_repeat_submissions_get_distinct_channel_names():
    first = tickets.build_channel_name(tickets.build_request(make_response(4)))
    second = tickets.build_channel_name(tickets.build_request(make_response(5)))
    assert first != second


def test_channel_names_respect_discord_length_limit():
    response = make_response(answers={"Discord username": "x" * 300, "Discord ID": ""})
    name = tickets.build_channel_name(tickets.build_request(response))
    assert len(name) <= tickets.MAX_CHANNEL_NAME


def test_a_name_of_only_symbols_still_produces_something_valid():
    response = make_response(answers={"Discord username": "!!!", "Discord ID": "", "Your name": ""})
    name = tickets.build_channel_name(tickets.build_request(response))
    assert name.strip("-")

def category(channel_count, category_id):
    return types.SimpleNamespace(
        id=category_id, name=f"category-{category_id}", channels=list(range(channel_count))
    )


def guild_with(categories):
    lookup = {c.id: c for c in categories}
    return types.SimpleNamespace(get_channel=lambda i: lookup.get(i), name="RIT Esports")


def test_a_category_with_room_is_used(monkeypatch):
    monkeypatch.setattr(tickets.config, "TICKET_CATEGORY_ID", 1)
    monkeypatch.setattr(tickets.config, "OVERFLOW_CATEGORY_IDS", [])
    monkeypatch.setattr(tickets.discord, "CategoryChannel", types.SimpleNamespace)

    found = tickets.find_available_category(guild_with([category(10, 1)]))
    assert found.id == 1


def test_a_full_category_falls_through_to_the_overflow(monkeypatch):
    """Discord caps categories at 50 channels; the bot used to just fail here."""
    monkeypatch.setattr(tickets.config, "TICKET_CATEGORY_ID", 1)
    monkeypatch.setattr(tickets.config, "OVERFLOW_CATEGORY_IDS", [2])
    monkeypatch.setattr(tickets.discord, "CategoryChannel", types.SimpleNamespace)

    guild = guild_with([category(tickets.CHANNELS_PER_CATEGORY_LIMIT, 1), category(0, 2)])
    assert tickets.find_available_category(guild).id == 2


def test_no_room_anywhere_returns_nothing(monkeypatch):
    monkeypatch.setattr(tickets.config, "TICKET_CATEGORY_ID", 1)
    monkeypatch.setattr(tickets.config, "OVERFLOW_CATEGORY_IDS", [2])
    monkeypatch.setattr(tickets.discord, "CategoryChannel", types.SimpleNamespace)

    full = tickets.CHANNELS_PER_CATEGORY_LIMIT
    guild = guild_with([category(full, 1), category(full, 2)])
    assert tickets.find_available_category(guild) is None

def test_the_closing_notice_is_posted_before_the_channel_is_locked():
    """Locking removes the submitter's access, so the order matters."""
    channel = FakeTicketChannel()
    run(close_and_settle(channel, FakeUser()))
    assert channel.events[0] == "announce"


def test_a_channel_is_never_deleted_without_a_transcript():
    """Deleting would otherwise destroy the only copy of the conversation."""
    channel = FakeTicketChannel(log_channel=None)
    result = run(close_and_settle(channel, FakeUser()))

    assert result.transcript_saved is False
    assert result.deleting_in_seconds is None
    assert "delete" not in channel.events


def test_a_ticket_is_archived_then_deleted(monkeypatch):
    log_channel = FakeLogChannel()
    monkeypatch.setattr(tickets.discord, "TextChannel", FakeLogChannel)

    channel = FakeTicketChannel(log_channel=log_channel)
    result = run(close_and_settle(channel, FakeUser(), "sorted in DMs"))

    assert result.transcript_saved is True
    assert channel.events == ["announce", "lock", "delete"]
    assert log_channel.posts, "the transcript was never posted to the staff log"


def test_a_failure_to_lock_is_reported_not_hidden(monkeypatch):
    """The old version swallowed the error and still said "Ticket closed"."""

    async def refuse(**kwargs):
        raise RuntimeError("missing permissions")

    channel = FakeTicketChannel()
    monkeypatch.setattr(channel, "edit", refuse)

    result = run(close_and_settle(channel, FakeUser()))
    assert result.locked is False
    assert any("permission" in problem for problem in result.problems)
    assert "Could not lock" in result.describe()


def test_the_closing_note_reaches_the_channel():
    channel = FakeTicketChannel()
    run(close_and_settle(channel, FakeUser(), "sorted in DMs"))
    assert any("sorted in DMs" in message for message in channel.messages)
