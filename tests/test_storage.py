"""The JSON files that stop the bot re-posting tickets after a restart.

The failure this file mostly guards against is the bot deciding it has never
seen any response before and opening a ticket for the entire sheet.
"""

import asyncio
import json

import pytest
from conftest import make_minimal_response, run

from intake import storage


def rows(*numbers):
    return [make_minimal_response(n) for n in numbers]


def pending(responses, handled):
    return [r.row_number for r in responses if r.fingerprint not in handled]

def test_first_run_does_not_ticket_the_whole_backlog(clean_storage):
    """Otherwise a new install opens hundreds of channels at once."""
    existing = rows(2, 3, 4, 5)
    handled, was_first_run = run(storage.load_handled_fingerprints(existing))

    assert was_first_run is True
    assert pending(existing, handled) == []


def test_responses_after_the_first_run_are_new(clean_storage):
    existing = rows(2, 3)
    run(storage.load_handled_fingerprints(existing))

    later = rows(2, 3, 4)
    handled, was_first_run = run(storage.load_handled_fingerprints(later))

    assert was_first_run is False
    assert pending(later, handled) == [4]


def test_backlog_is_processed_when_explicitly_requested(clean_storage, monkeypatch):
    monkeypatch.setattr(storage.config, "PROCESS_BACKLOG", True)
    existing = rows(2, 3)
    handled, was_first_run = run(storage.load_handled_fingerprints(existing))

    assert was_first_run is False
    assert pending(existing, handled) == [2, 3]

def test_marking_a_response_stops_it_coming_back(clean_storage):
    existing = rows(2, 3)
    run(storage.load_handled_fingerprints(existing))

    later = rows(2, 3, 4)
    run(storage.mark_handled([later[-1].fingerprint]))

    handled, _ = run(storage.load_handled_fingerprints(later))
    assert pending(later, handled) == []


def test_marking_nothing_is_harmless(clean_storage):
    run(storage.load_handled_fingerprints(rows(2)))
    run(storage.mark_handled([]))
    assert run(storage.handled_count()) == 1


def test_old_fingerprints_are_eventually_forgotten(clean_storage, monkeypatch):
    """The file must not grow forever."""
    monkeypatch.setattr(storage, "MAX_REMEMBERED_RESPONSES", 10)
    run(storage.load_handled_fingerprints([]))
    run(storage.mark_handled([f"fingerprint-{i}" for i in range(25)]))
    assert run(storage.handled_count()) == 10


def test_deleting_a_sheet_row_does_not_replay_tickets(clean_storage):
    """Row numbers shift when a row is deleted. Fingerprints do not.

    The previous version tracked a single `last_row`, so deleting one row made
    the bot re-post tickets for everything below it.
    """
    original = rows(2, 3, 4, 5, 6)
    run(storage.load_handled_fingerprints(original))

    # Row 3 is deleted in the sheet; everything below shifts up one.
    surviving = [r for r in original if r.row_number != 3]
    for new_number, response in enumerate(surviving, start=2):
        response.row_number = new_number

    handled, _ = run(storage.load_handled_fingerprints(surviving))
    assert pending(surviving, handled) == []

def test_a_version_1_state_file_is_migrated(clean_storage):
    """Version 1 stored `{"last_row": N}`. Everything up to N was handled."""
    (clean_storage / "state.json").write_text(json.dumps({"last_row": 4}))

    existing = rows(2, 3, 4, 5, 6)
    handled, was_first_run = run(storage.load_handled_fingerprints(existing))

    assert was_first_run is False
    assert pending(existing, handled) == [5, 6]


def test_a_corrupt_state_file_raises_instead_of_resetting(clean_storage):
    """Silently starting over would re-ticket the entire sheet."""
    (clean_storage / "state.json").write_text("{ not valid json")

    with pytest.raises(json.JSONDecodeError):
        run(storage.load_handled_fingerprints(rows(2)))


def test_writes_are_atomic(clean_storage):
    """A failed write must leave the previous file untouched."""
    run(storage.load_handled_fingerprints(rows(2)))
    before = (clean_storage / "state.json").read_text()

    unserialisable = {"processed": {1, 2, 3}}  # a set cannot be written as JSON
    with pytest.raises(TypeError):
        storage._state_file.write(unserialisable)

    assert (clean_storage / "state.json").read_text() == before
    assert not (clean_storage / "state.json.tmp").exists()

def test_subscribing_and_unsubscribing(clean_storage):
    assert run(storage.toggle_subscription(1, 42)) is True
    assert run(storage.get_subscribers(1)) == [42]

    assert run(storage.toggle_subscription(1, 42)) is False
    assert run(storage.get_subscribers(1)) == []


def test_subscriptions_are_kept_per_ticket(clean_storage):
    run(storage.toggle_subscription(1, 42))
    run(storage.toggle_subscription(2, 99))
    assert run(storage.get_subscribers(1)) == [42]
    assert run(storage.get_subscribers(2)) == [99]


def test_simultaneous_presses_do_not_lose_subscribers(clean_storage):
    """Read-modify-write without a lock used to drop whichever write lost."""

    async def everyone_at_once():
        await asyncio.gather(*[storage.toggle_subscription(1, uid) for uid in range(100, 120)])
        return await storage.get_subscribers(1)

    assert len(run(everyone_at_once())) == 20


def test_closing_a_ticket_forgets_its_subscribers(clean_storage):
    run(storage.toggle_subscription(1, 42))
    run(storage.clear_subscriptions(1))
    assert run(storage.get_subscribers(1)) == []


def test_clearing_an_unknown_ticket_is_harmless(clean_storage):
    run(storage.clear_subscriptions(12345))
