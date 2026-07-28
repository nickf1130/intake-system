"""The background job that turns new form responses into ticket channels.

It polls the sheet every ``INTAKE_POLL_SECONDS``. The important guarantee is
that a response is recorded as handled exactly when it has been dealt with -
whether that means a ticket was opened, or the response turned out to be
impossible to post and was deliberately skipped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

import discord

from . import config, embeds, sheets, storage, tickets, views

log = logging.getLogger(__name__)

# How long to wait after a failure before trying again, growing each time.
BACKOFF_STEP_SECONDS = 30
BACKOFF_MAX_SECONDS = 300


@dataclass
class SyncStatus:
    """A snapshot of how the sync job is doing, shown by /intake_status."""

    last_attempt_at: dt.datetime | None = None
    last_success_at: dt.datetime | None = None
    last_error: str = ""
    tickets_created: int = 0
    responses_skipped: int = 0
    pending: int = 0
    is_running: bool = False
    recent_problems: list[str] = field(default_factory=list)

    def note_problem(self, message: str) -> None:
        self.recent_problems.append(f"{dt.datetime.now(dt.timezone.utc):%H:%M} {message}")
        del self.recent_problems[:-5]  # keep only the last five


STATUS = SyncStatus()


def is_configured() -> bool:
    """True if there is enough configuration for the sync job to do anything."""
    return bool(config.SHEET_ID and config.TICKET_CATEGORY_ID)


async def run_once(client: discord.Client) -> int:
    """Check the sheet once and open tickets for anything new.

    Returns the number of tickets created. Raises if the sheet or the category
    could not be reached at all; individual bad responses are handled inline.
    """
    if not is_configured():
        raise RuntimeError("INTAKE_SHEET_ID and INTAKE_TICKET_CATEGORY_ID must both be set")

    STATUS.last_attempt_at = dt.datetime.now(dt.timezone.utc)

    responses = await sheets.fetch_responses()
    handled, was_first_run = await storage.load_handled_fingerprints(responses)
    if was_first_run:
        STATUS.last_success_at = STATUS.last_attempt_at
        STATUS.last_error = ""
        return 0

    pending = [response for response in responses if response.fingerprint not in handled]
    STATUS.pending = len(pending)
    if not pending:
        STATUS.last_success_at = STATUS.last_attempt_at
        STATUS.last_error = ""
        return 0

    category = tickets.get_primary_category(client)
    if category is None:
        raise RuntimeError(
            f"INTAKE_TICKET_CATEGORY_ID ({config.TICKET_CATEGORY_ID}) is not a category "
            "the bot can see. Check the ID and the bot's permissions."
        )

    guild = category.guild
    batch = pending[: config.MAX_TICKETS_PER_POLL]
    if len(pending) > len(batch):
        log.info("%s responses waiting; handling %s this round", len(pending), len(batch))

    created = 0
    for response in batch:
        request = tickets.build_request(response)

        try:
            channel = await tickets.open_ticket(guild, request, views.TicketControlsView())

        except tickets.PermanentTicketError as error:
            # This response can never post. Record it as handled anyway,
            # otherwise the bot retries it forever, every single poll.
            log.error("Skipping sheet row %s permanently: %s", response.row_number, error)
            STATUS.note_problem(f"Skipped row {response.row_number}: {error}")
            STATUS.responses_skipped += 1
            await storage.mark_handled([response.fingerprint])
            continue

        except tickets.TemporaryTicketError as error:
            # Stop for now and keep the response queued. Trying the rest of the
            # batch would almost certainly hit the same wall.
            log.warning("Pausing intake sync at row %s: %s", response.row_number, error)
            STATUS.note_problem(str(error))
            break

        try:
            await storage.mark_handled([response.fingerprint])
        except Exception:
            log.exception(
                "Opened #%s but could not save state. Stopping to avoid duplicate tickets.",
                channel.name,
            )
            STATUS.note_problem("Could not write the state file - check disk permissions.")
            break

        created += 1
        STATUS.tickets_created += 1
        log.info("Opened ticket #%s for sheet row %s", channel.name, response.row_number)

    STATUS.last_success_at = dt.datetime.now(dt.timezone.utc)
    STATUS.last_error = ""
    STATUS.pending = len(pending) - created
    return created


async def sync_loop(client: discord.Client) -> None:
    """Run `run_once` forever, backing off when the sheet or Discord is unhappy."""
    await client.wait_until_ready()
    log.info("Intake sync started, checking every %s seconds", config.POLL_SECONDS)

    STATUS.is_running = True
    backoff = 0
    try:
        while True:
            try:
                await run_once(client)
                backoff = 0
                await asyncio.sleep(config.POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                STATUS.last_error = str(error)
                STATUS.note_problem(str(error))
                backoff = min(backoff + BACKOFF_STEP_SECONDS, BACKOFF_MAX_SECONDS)
                log.error("Intake sync failed, retrying in %ss: %s", backoff, error)
                await asyncio.sleep(backoff)
    finally:
        STATUS.is_running = False
        log.info("Intake sync stopped")


def build_status_embed() -> discord.Embed:
    """A staff-facing summary of configuration and recent sync activity."""
    healthy = STATUS.is_running and not STATUS.last_error
    embed = discord.Embed(
        title="Intake bot status",
        colour=embeds.COLOUR_OPEN if healthy else embeds.COLOUR_CLAIMED,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )

    def timestamp(moment: dt.datetime | None) -> str:
        return f"<t:{int(moment.timestamp())}:R>" if moment else "never"

    embeds.set_field(embed, "Sync job", "running" if STATUS.is_running else "**stopped**")
    embeds.set_field(embed, "Last successful check", timestamp(STATUS.last_success_at))
    embeds.set_field(embed, "Checking every", f"{config.POLL_SECONDS}s")
    embeds.set_field(embed, "Tickets opened", str(STATUS.tickets_created))
    embeds.set_field(embed, "Responses skipped", str(STATUS.responses_skipped))
    embeds.set_field(embed, "Waiting in the sheet", str(STATUS.pending))

    if STATUS.last_error:
        embeds.set_field(embed, "Last error", STATUS.last_error, inline=False)
    if STATUS.recent_problems:
        embeds.set_field(embed, "Recent problems", "\n".join(STATUS.recent_problems), inline=False)

    if warnings := config.configuration_warnings():
        embeds.set_field(embed, "Configuration warnings", "\n".join(f"- {w}" for w in warnings), inline=False)

    return embed
