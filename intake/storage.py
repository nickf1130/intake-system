"""The two small JSON files that let the bot survive a restart.

* ``intake_state.json``         - which form responses already became tickets
* ``intake_subscriptions.json`` - who wants DM updates about which ticket

Both are written atomically (write to a temp file, then rename) so a crash
mid-write cannot leave a half-written file behind, and both are guarded by a
lock so two button presses at the same time cannot overwrite each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable, Iterable
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Bumped when the shape of intake_state.json changes. See `_migrate_state`.
STATE_VERSION = 2

# How many response fingerprints to remember. Comfortably more than a year of
# submissions, and keeps the file from growing forever.
MAX_REMEMBERED_RESPONSES = 5000


class JsonFile:
    """A JSON file on disk that is only ever replaced, never edited in place."""

    def __init__(self, path: str, build_default: Callable[[], dict]):
        self.path = path
        self.build_default = build_default

    def read(self) -> dict:
        """Return the file's contents, or a fresh default if it does not exist.

        A file that exists but cannot be parsed raises instead of quietly
        resetting. Silently starting over would make the bot think no response
        had ever been handled and re-post a ticket for the entire sheet.
        """
        if not os.path.exists(self.path):
            return self.build_default()
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def write(self, data: dict) -> None:
        """Replace the file's contents. Either fully succeeds or changes nothing."""
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)

        temp_path = f"{self.path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())  # make sure it really hit the disk
            os.replace(temp_path, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
            raise


def default_state() -> dict:
    """What the state file looks like for a bot that has never run before."""
    return {"version": STATE_VERSION, "bootstrapped": False, "processed": []}


def default_subscriptions() -> dict:
    """What the subscriptions file looks like when nobody has subscribed yet."""
    return {"by_ticket": {}}


_state_file = JsonFile(config.STATE_FILE, default_state)
_subscriptions_file = JsonFile(config.SUBSCRIPTIONS_FILE, default_subscriptions)

_state_lock = asyncio.Lock()
_subscriptions_lock = asyncio.Lock()

def _migrate_state(state: dict, responses: list[Any]) -> dict:
    """Bring an older state file up to the current format.

    Version 1 tracked a single ``last_row`` number. That broke whenever a row
    was deleted from the sheet, because every row below it shifted up. We now
    remember a fingerprint per response instead, and convert by marking every
    row at or below the old ``last_row`` as already handled.
    """
    last_row = int(state.get("last_row") or 0)
    already_handled = [response.fingerprint for response in responses if response.row_number <= last_row]
    log.info(
        "Upgrading intake state to version %s: %s earlier responses marked as handled",
        STATE_VERSION,
        len(already_handled),
    )
    return {"version": STATE_VERSION, "bootstrapped": True, "processed": already_handled}


async def load_handled_fingerprints(responses: list[Any]) -> tuple[set[str], bool]:
    """Return which responses are already handled, setting up state on first run.

    The second value is True when this call performed first-time setup. On a
    brand new install every existing response is marked as handled so the bot
    does not create a ticket for the club's entire response history. Set
    INTAKE_PROCESS_BACKLOG=1 if you actually want that backlog.
    """
    async with _state_lock:
        state = await asyncio.to_thread(_state_file.read)

        if state.get("version") != STATE_VERSION:
            state = _migrate_state(state, responses)
            await asyncio.to_thread(_state_file.write, state)
            return set(state["processed"]), False

        if not state.get("bootstrapped"):
            if config.PROCESS_BACKLOG:
                log.warning(
                    "First run with INTAKE_PROCESS_BACKLOG enabled: creating tickets "
                    "for all %s existing responses",
                    len(responses),
                )
                handled: list[str] = []
            else:
                log.info(
                    "First run: marking %s existing responses as already handled. "
                    "Only new submissions from here on will become tickets.",
                    len(responses),
                )
                handled = [response.fingerprint for response in responses]

            state = {"version": STATE_VERSION, "bootstrapped": True, "processed": handled}
            await asyncio.to_thread(_state_file.write, state)
            return set(handled), not config.PROCESS_BACKLOG

        return set(state.get("processed", [])), False


async def mark_handled(fingerprints: Iterable[str]) -> None:
    """Record that these responses have been turned into tickets."""
    new = list(fingerprints)
    if not new:
        return
    async with _state_lock:
        state = await asyncio.to_thread(_state_file.read)
        handled = list(state.get("processed", []))
        known = set(handled)
        handled.extend(fingerprint for fingerprint in new if fingerprint not in known)
        state["version"] = STATE_VERSION
        state["bootstrapped"] = True
        state["processed"] = handled[-MAX_REMEMBERED_RESPONSES:]
        await asyncio.to_thread(_state_file.write, state)


async def handled_count() -> int:
    """How many responses the bot currently remembers handling."""
    state = await asyncio.to_thread(_state_file.read)
    return len(state.get("processed", []))

async def toggle_subscription(ticket_channel_id: int, user_id: int) -> bool:
    """Subscribe or unsubscribe a user. Returns True if they are now subscribed."""
    async with _subscriptions_lock:
        data = await asyncio.to_thread(_subscriptions_file.read)
        by_ticket = data.setdefault("by_ticket", {})
        key = str(ticket_channel_id)

        subscribers = set(by_ticket.get(key, []))
        is_subscribing = user_id not in subscribers
        if is_subscribing:
            subscribers.add(user_id)
        else:
            subscribers.discard(user_id)

        if subscribers:
            by_ticket[key] = sorted(subscribers)
        else:
            by_ticket.pop(key, None)

        await asyncio.to_thread(_subscriptions_file.write, data)
        return is_subscribing


async def get_subscribers(ticket_channel_id: int) -> list[int]:
    """User IDs who asked for DM updates about this ticket."""
    data = await asyncio.to_thread(_subscriptions_file.read)
    return [int(user_id) for user_id in data.get("by_ticket", {}).get(str(ticket_channel_id), [])]


async def clear_subscriptions(ticket_channel_id: int) -> None:
    """Forget a closed ticket, so the file does not grow forever."""
    async with _subscriptions_lock:
        data = await asyncio.to_thread(_subscriptions_file.read)
        if data.get("by_ticket", {}).pop(str(ticket_channel_id), None) is not None:
            await asyncio.to_thread(_subscriptions_file.write, data)
