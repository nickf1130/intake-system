"""The background job that turns new responses into tickets.

The behaviour this file exists to protect is the "poison row" handling. A
response that Discord will never accept must be recorded as handled and
skipped, because the previous version left it queued and created a fresh
channel for it on every single poll, forever.
"""

import types

import pytest
from conftest import make_minimal_response, run

from intake import sheets, sync, tickets


class FakeDiscord:
    """Stands in for Discord and the sheet for a whole test."""

    def __init__(self, monkeypatch, responses):
        self.responses = list(responses)
        self.created_rows = []
        self.behaviour = {}  # row number -> "ok" | "permanent" | "temporary"

        guild = types.SimpleNamespace(name="RIT Esports", id=1)
        category = types.SimpleNamespace(guild=guild, name="tickets", channels=[])

        async def fetch_responses():
            return list(self.responses)

        async def open_ticket(guild_, request, view):
            row = request.response.row_number
            mode = self.behaviour.get(row, "ok")
            if mode == "permanent":
                raise tickets.PermanentTicketError("Discord rejected the message")
            if mode == "temporary":
                raise tickets.TemporaryTicketError("every category is full")
            self.created_rows.append(row)
            return types.SimpleNamespace(name=f"intake-{row}", id=row)

        monkeypatch.setattr(sheets, "fetch_responses", fetch_responses)
        monkeypatch.setattr(tickets, "open_ticket", open_ticket)
        monkeypatch.setattr(tickets, "get_primary_category", lambda client: category)

    def add(self, row_number, behaviour="ok"):
        self.responses.append(make_minimal_response(row_number))
        self.behaviour[row_number] = behaviour

    def poll(self, times=1):
        for _ in range(times):
            created = run(sync.run_once(object()))
        return created


@pytest.fixture
def discord_and_sheet(monkeypatch, clean_storage, fresh_status):
    """A fake world with two responses already in the sheet, both handled."""
    fake = FakeDiscord(monkeypatch, [make_minimal_response(2), make_minimal_response(3)])
    fake.poll()  # first run marks the existing backlog as handled
    assert fake.created_rows == []
    return fake

def test_the_existing_backlog_is_not_ticketed(discord_and_sheet):
    assert discord_and_sheet.created_rows == []


def test_a_new_response_becomes_a_ticket(discord_and_sheet):
    discord_and_sheet.add(4)
    assert discord_and_sheet.poll() == 1
    assert discord_and_sheet.created_rows == [4]


def test_a_response_is_only_ticketed_once(discord_and_sheet):
    discord_and_sheet.add(4)
    discord_and_sheet.poll(times=3)
    assert discord_and_sheet.created_rows == [4]


def test_nothing_new_creates_nothing(discord_and_sheet):
    assert discord_and_sheet.poll() == 0


def test_only_a_limited_number_are_created_per_poll(discord_and_sheet, monkeypatch):
    """A safety valve against flooding the server."""
    monkeypatch.setattr(sync.config, "MAX_TICKETS_PER_POLL", 2)
    for row in (4, 5, 6, 7):
        discord_and_sheet.add(row)

    assert discord_and_sheet.poll() == 2
    assert discord_and_sheet.poll() == 2
    assert discord_and_sheet.created_rows == [4, 5, 6, 7]

def test_a_response_discord_always_rejects_is_skipped_once(discord_and_sheet, fresh_status):
    """This is the infinite-channel-creation bug.

    A response that could not be posted was never recorded, so the next poll
    created another channel for it, and so on every minute forever.
    """
    discord_and_sheet.add(4, behaviour="permanent")
    discord_and_sheet.poll(times=5)

    assert discord_and_sheet.created_rows == []
    assert fresh_status.responses_skipped == 1


def test_a_skipped_response_does_not_block_later_ones(discord_and_sheet):
    discord_and_sheet.add(4, behaviour="permanent")
    discord_and_sheet.add(5)
    discord_and_sheet.poll()
    assert discord_and_sheet.created_rows == [5]


def test_a_temporary_failure_keeps_the_response_queued(discord_and_sheet):
    """A full category or a rate limit must not lose the submission."""
    discord_and_sheet.add(4, behaviour="temporary")
    discord_and_sheet.poll(times=3)
    assert discord_and_sheet.created_rows == []

    discord_and_sheet.behaviour[4] = "ok"
    assert discord_and_sheet.poll() == 1
    assert discord_and_sheet.created_rows == [4]


def test_a_temporary_failure_stops_the_rest_of_the_batch(discord_and_sheet):
    """Later responses would hit the same wall, so it stops and retries later."""
    discord_and_sheet.add(4, behaviour="temporary")
    discord_and_sheet.add(5)
    discord_and_sheet.poll()
    assert discord_and_sheet.created_rows == []


def test_a_missing_category_raises_rather_than_failing_silently(
    monkeypatch, clean_storage, fresh_status
):
    fake = FakeDiscord(monkeypatch, [make_minimal_response(2)])
    fake.poll()
    fake.add(3)
    monkeypatch.setattr(tickets, "get_primary_category", lambda client: None)

    with pytest.raises(RuntimeError, match="INTAKE_TICKET_CATEGORY_ID"):
        fake.poll()


def test_a_failure_to_save_state_stops_rather_than_duplicating(
    discord_and_sheet, monkeypatch, fresh_status
):
    """Carrying on would open the same ticket again on the next poll."""

    async def disk_full(fingerprints):
        raise OSError("no space left on device")

    discord_and_sheet.add(4)
    discord_and_sheet.add(5)
    monkeypatch.setattr(sync.storage, "mark_handled", disk_full)

    assert discord_and_sheet.poll() == 0
    assert discord_and_sheet.created_rows == [4]  # stopped instead of doing 5 too

def test_status_counts_what_happened(discord_and_sheet, fresh_status):
    discord_and_sheet.add(4)
    discord_and_sheet.add(5, behaviour="permanent")
    discord_and_sheet.poll()

    assert fresh_status.tickets_created == 1
    assert fresh_status.responses_skipped == 1
    assert fresh_status.last_success_at is not None


def test_status_embed_builds_without_a_live_connection(fresh_status):
    fresh_status.last_error = "could not reach the sheet"
    fresh_status.note_problem("something went wrong")
    embed = sync.build_status_embed()
    assert embed.title == "Intake bot status"


def test_only_the_most_recent_problems_are_kept(fresh_status):
    for i in range(20):
        fresh_status.note_problem(f"problem {i}")
    assert len(fresh_status.recent_problems) == 5


def test_sync_is_configured_in_the_test_environment():
    assert sync.is_configured() is True
