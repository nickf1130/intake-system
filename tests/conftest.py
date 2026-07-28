"""Shared setup and fake Discord objects for the test suite.

``intake.config`` reads the environment the first time it is imported, so the
settings below have to be in place *before* anything from ``intake`` is
imported. pytest always loads conftest.py first, which makes this the right
place to do it.

The tests deliberately avoid pytest-asyncio: `run()` below is all the async
support they need, and it is one less thing for a new maintainer to install
or understand.
"""

import asyncio
import os
import tempfile

# IDs used throughout the tests. Real Discord IDs are 17-20 digits, and some
# code paths check that, so these have to look realistic.
TICKET_CATEGORY_ID = 111111111111111111
OPS_ROLE_ID = 222222222222222222
COMP_ROLE_ID = 333333333333333333
VALORANT_ROLE_ID = 444444444444444444
ROCKET_LEAGUE_USER_IDS = (555555555555555555, 666666666666666666)
MARKETING_ROLE_ID = 777777777777777777
TRANSCRIPT_CHANNEL_ID = 888888888888888888
SECRETARY_ROLE_ID = 999999999999999999
SUBMITTER_ID = 123456789012345678

_scratch = tempfile.mkdtemp(prefix="intake-tests-")

os.environ.update(
    {
        "TOKEN": "test-token",
        "INTAKE_FORM_URL": "https://forms.gle/example",
        "INTAKE_SHEET_ID": "test-sheet",
        "INTAKE_TICKET_CATEGORY_ID": str(TICKET_CATEGORY_ID),
        "INTAKE_TRANSCRIPT_CHANNEL_ID": str(TRANSCRIPT_CHANNEL_ID),
        "OPS_ROLE_ID": str(OPS_ROLE_ID),
        "COMP_ROLE_ID": str(COMP_ROLE_ID),
        "SECRETARY_ROLE_ID": str(SECRETARY_ROLE_ID),
        "COMP_TEAM_ROLE_VALORANT": str(VALORANT_ROLE_ID),
        "COMP_TEAM_USERS_ROCKET_LEAGUE": ", ".join(str(i) for i in ROCKET_LEAGUE_USER_IDS),
        "OPS_TEAM_ROLE_MARKETING": str(MARKETING_ROLE_ID),
        "INTAKE_DELETE_DELAY_SECONDS": "0",
        "INTAKE_MAX_TICKETS_PER_POLL": "10",
        # Point at a temp directory so a stray write never touches the project.
        "INTAKE_STATE_FILE": os.path.join(_scratch, "state.json"),
        "INTAKE_SUBSCRIPTIONS_FILE": os.path.join(_scratch, "subscriptions.json"),
    }
)

import datetime  # noqa: E402
import types  # noqa: E402

import pytest  # noqa: E402

from intake import sheets, storage, sync, tickets  # noqa: E402


def run(coro):
    """Run one async call from an ordinary (non-async) test."""
    return asyncio.run(coro)

@pytest.fixture
def clean_storage(tmp_path, monkeypatch):
    """Give the test its own empty state and subscription files."""
    monkeypatch.setattr(
        storage,
        "_state_file",
        storage.JsonFile(str(tmp_path / "state.json"), storage.default_state),
    )
    monkeypatch.setattr(
        storage,
        "_subscriptions_file",
        storage.JsonFile(str(tmp_path / "subscriptions.json"), storage.default_subscriptions),
    )
    return tmp_path


@pytest.fixture
def fresh_status(monkeypatch):
    """Reset the sync job's status counters so tests do not affect each other."""
    status = sync.SyncStatus()
    monkeypatch.setattr(sync, "STATUS", status)
    return status

def make_response(row_number: int = 2, answers: dict | None = None) -> sheets.FormResponse:
    """A realistic response, shaped like the real intake form."""
    base = {
        "Timestamp": f"2026-07-28 10:{row_number:02d}:00",
        "Email address": "jr@rit.edu",
        "Your name": "Jordan Reyes",
        "Discord username": "jordanr",
        "Discord ID": str(SUBMITTER_ID),
        "Are you affiliated with RIT Esports?": "Yes",
        "What branch do you require help from (operations/competitive)": "Competitive",
        "What Competitive Team(s) do you need support from?": "Valorant, Rocket League",
        "Please describe what you need help with in detail": (
            "Our scrim block clashes with the LAN setup on Friday."
        ),
        "If you are not affiliated, how did you hear about us?": "",
    }
    if answers:
        base.update(answers)
    return sheets.FormResponse(row_number, base)


def make_minimal_response(row_number: int) -> sheets.FormResponse:
    """The smallest response that still has a stable fingerprint."""
    return sheets.FormResponse(
        row_number,
        {
            "Timestamp": f"2026-07-{row_number:02d} 10:00",
            "Discord ID": f"1000000000000000{row_number:02d}",
        },
    )

class FakeUser:
    """Someone pressing a button."""

    def __init__(self, name: str = "nick", user_id: int = SUBMITTER_ID):
        self.name = name
        self.id = user_id
        self.mention = f"<@{user_id}>"

    def __str__(self) -> str:
        return self.name


class FakeLogChannel:
    """The staff channel a transcript gets posted to."""

    def __init__(self):
        self.posts = []

    async def send(self, *args, **kwargs):
        self.posts.append(kwargs)


class FakeTicketChannel:
    """A ticket channel that records what was done to it, in order."""

    def __init__(self, log_channel=None, name: str = "intake-competitive-jordanr-2"):
        self.name = name
        self.id = 4242
        self.created_at = datetime.datetime.now(datetime.timezone.utc)
        self.events: list[str] = []
        self.messages: list[str] = []
        self.guild = types.SimpleNamespace(
            name="RIT Esports",
            get_channel=lambda _id: log_channel,
            get_role=lambda _id: None,
            get_member=lambda _id: None,
            default_role="@everyone",
            me="bot",
        )

    async def send(self, content=None, **kwargs):
        self.events.append("announce" if content and "closed by" in content else "send")
        if content:
            self.messages.append(content)

    async def edit(self, **kwargs):
        self.events.append("lock")

    async def delete(self, **kwargs):
        self.events.append("delete")

    def history(self, **kwargs):
        async def empty():
            return
            yield  # pragma: no cover - makes this an async generator

        return empty()


async def close_and_settle(channel, closed_by, reason: str = ""):
    """Close a ticket and wait for the scheduled deletion to finish.

    `close_ticket` schedules the delete as a background task so the button
    press can return straight away. Tests need to wait for it.
    """
    result = await tickets.close_ticket(channel, closed_by, reason)
    if tickets._pending_deletions:
        await asyncio.gather(*list(tickets._pending_deletions))
    return result
