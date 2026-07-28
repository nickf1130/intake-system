"""The buttons attached to intake messages.

Both views are "persistent": they have a fixed ``custom_id`` and no timeout,
so they keep working after the bot restarts, as long as the bot re-registers
them on startup (see ``main.py``).

One py-cord detail worth knowing: a persistent view registered with
``bot.add_view`` is a *single shared object* used by every message. Mutating
``self.children`` in a button handler would therefore leak into other tickets,
so handlers always build a fresh view to send back instead.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import discord

from . import config, embeds, permissions, storage, tickets

log = logging.getLogger(__name__)

CLAIM_BUTTON_ID = "intake_claim"
CLOSE_BUTTON_ID = "intake_close"
SUBSCRIBE_BUTTON_ID = "intake_subscribe"


def build_prefilled_form_url(form_url: str, entry_id: str, user_id: int) -> str:
    """Add the member's Discord ID to the form link, if the form has a field for it."""
    if not form_url or not entry_id:
        return form_url
    try:
        parsed = urlparse(form_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query[f"entry.{entry_id}"] = str(user_id)
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    except Exception:
        log.warning("Could not pre-fill the form URL, handing out the plain link instead")
        return form_url


class IntakeFormView(discord.ui.View):
    """The single permanent message people press to get the form link."""

    def __init__(self, *, persistent: bool = True):
        super().__init__(timeout=None if persistent else 300)

    @discord.ui.button(
        label="Open Intake Form",
        style=discord.ButtonStyle.primary,
        emoji="\N{MEMO}",
        custom_id="intake_open_form",
    )
    async def open_form(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not config.FORM_URL:
            await interaction.response.send_message(
                "The intake form link has not been set up yet. Please let a manager know.",
                ephemeral=True,
            )
            return

        url = build_prefilled_form_url(config.FORM_URL, config.DISCORD_ID_ENTRY, interaction.user.id)
        await interaction.response.send_message(
            f"Here is your intake form: {url}\n"
            "Once you submit it, the bot opens a private channel for you within a minute.",
            ephemeral=True,
        )

    async def on_error(self, error: Exception, item, interaction: discord.Interaction):
        log.exception("Intake form button failed", exc_info=error)
        await _report_error(interaction, "Something went wrong handing out the form link.")


class CloseTicketModal(discord.ui.Modal):
    """Asks for an optional closing note, and doubles as a confirmation step.

    Closing deletes the channel, so it is worth one deliberate extra click.
    """

    def __init__(self, ticket_message: discord.Message):
        super().__init__(title="Close this ticket")
        self.ticket_message = ticket_message
        self.add_item(
            discord.ui.InputText(
                label="Closing note (optional)",
                placeholder="How was this resolved?",
                style=discord.InputTextStyle.long,
                required=False,
                max_length=500,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        reason = (self.children[0].value or "").strip()
        await interaction.response.defer(ephemeral=True)

        # Show the outcome on the ticket itself before the archiving starts.
        try:
            embed = _first_embed(self.ticket_message)
            await self.ticket_message.edit(
                embed=embeds.mark_closed(embed, interaction.user, reason),
                view=TicketControlsView().as_closed(),
            )
        except Exception:
            log.exception("Could not update the ticket message while closing")

        result = await tickets.close_ticket(interaction.channel, interaction.user, reason)
        await interaction.followup.send(result.describe(), ephemeral=True)


class TicketControlsView(discord.ui.View):
    """Subscribe / Claim / Close, shown at the top of every ticket channel."""

    def __init__(self, *, persistent: bool = True):
        super().__init__(timeout=None if persistent else 300)

    def _button(self, custom_id: str) -> discord.ui.Button | None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == custom_id:
                return child
        return None

    def as_claimed(self) -> TicketControlsView:
        """This view with the Claim button used up."""
        if button := self._button(CLAIM_BUTTON_ID):
            button.disabled = True
            button.label = "Claimed"
        return self

    def as_closed(self) -> TicketControlsView:
        """This view with everything switched off."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        return self

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        if permissions.is_manager(interaction.user):
            return True
        await interaction.response.send_message(
            "Only managers can do that. If you need an update, use "
            "**Subscribe** and the bot will DM you.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Subscribe",
        style=discord.ButtonStyle.secondary,
        emoji="\N{BELL}",
        custom_id=SUBSCRIBE_BUTTON_ID,
    )
    async def subscribe(self, button: discord.ui.Button, interaction: discord.Interaction):
        now_subscribed = await storage.toggle_subscription(interaction.channel.id, interaction.user.id)
        await interaction.response.send_message(
            "You will get a DM when a manager claims this ticket."
            if now_subscribed
            else "You will no longer get DM updates about this ticket.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        emoji="\N{RAISED HAND}",
        custom_id=CLAIM_BUTTON_ID,
    )
    async def claim(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._require_manager(interaction):
            return

        embed = embeds.mark_claimed(_first_embed(interaction.message), interaction.user)
        await interaction.response.edit_message(embed=embed, view=TicketControlsView().as_claimed())

        notified = await _notify_subscribers(interaction)
        await interaction.followup.send(
            f"You claimed this ticket. {notified} subscriber(s) notified.", ephemeral=True
        )

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji="\N{LOCK}",
        custom_id=CLOSE_BUTTON_ID,
    )
    async def close(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._require_manager(interaction):
            return
        await interaction.response.send_modal(CloseTicketModal(interaction.message))

    async def on_error(self, error: Exception, item, interaction: discord.Interaction):
        log.exception("Ticket button failed", exc_info=error)
        await _report_error(interaction, "Something went wrong. A manager should check the bot logs.")

def _first_embed(message: discord.Message | None) -> discord.Embed:
    """The ticket's embed, or a blank one if the message somehow has none."""
    if message and message.embeds:
        return message.embeds[0]
    return discord.Embed(title="Intake Ticket", colour=embeds.COLOUR_OPEN)


async def _notify_subscribers(interaction: discord.Interaction) -> int:
    """DM everyone watching this ticket that it has been claimed."""
    channel = interaction.channel
    sent = 0
    for user_id in await storage.get_subscribers(channel.id):
        try:
            user = interaction.client.get_user(user_id) or await interaction.client.fetch_user(user_id)
            await user.send(
                f"**{interaction.user}** has claimed your intake ticket in "
                f"{channel.guild.name}.\nJump to it: {channel.jump_url}"
            )
            sent += 1
        except discord.Forbidden:
            log.info("Cannot DM %s - they have DMs closed", user_id)
        except Exception:
            log.exception("Failed to DM subscriber %s", user_id)
    return sent


async def _report_error(interaction: discord.Interaction, message: str) -> None:
    """Tell the user something failed, whichever stage the interaction is at."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        log.exception("Could not report an error back to the user")
