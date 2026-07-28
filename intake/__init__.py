"""RIT Esports intake bot.

The package is split so each file has one job:

    config.py       every setting, read from the environment
    permissions.py  who counts as a manager
    sheets.py       reading responses out of the Google Sheet
    embeds.py       building the messages Discord shows
    tickets.py      opening, routing and closing ticket channels
    views.py        the buttons on those messages
    storage.py      the small JSON files that survive a restart
    sync.py         the background job tying it all together

Start at ``sync.run_once`` to follow what happens when someone submits the form.
"""

from __future__ import annotations

from . import config, embeds, permissions, sheets, storage, sync, tickets, views
from .embeds import build_guidelines_embed, build_prompt_embed
from .permissions import is_manager
from .sync import build_status_embed, is_configured, run_once, sync_loop
from .views import IntakeFormView, TicketControlsView

__all__ = [
    "config",
    "embeds",
    "permissions",
    "sheets",
    "storage",
    "sync",
    "tickets",
    "views",
    "build_guidelines_embed",
    "build_prompt_embed",
    "build_status_embed",
    "is_configured",
    "is_manager",
    "post_intake_prompt",
    "run_once",
    "sync_loop",
    "IntakeFormView",
    "TicketControlsView",
    # Older names, kept so anything still importing them keeps working.
    "IntakeThreadView",
    "is_sync_configured",
    "intake_sync_loop",
]


async def post_intake_prompt(channel, *, with_guidelines: bool = True):
    """Post the intake guidelines and the button that hands out the form link."""
    if with_guidelines:
        await channel.send(embed=build_guidelines_embed())
    return await channel.send(embed=build_prompt_embed(), view=IntakeFormView())


# Names this package used before it was split up. They point at the current
# implementations so older code does not break.
IntakeThreadView = TicketControlsView
is_sync_configured = is_configured
intake_sync_loop = sync_loop
