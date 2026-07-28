"""Entry point for the RIT Esports intake bot.

    python main.py

Everything is configured through a `.env` file - copy `.env.example` and fill
it in. This file only wires things together: the actual intake logic lives in
the `intake` package.
"""

import asyncio
import logging
import logging.handlers
import os
from pathlib import Path

from dotenv import load_dotenv

# Settings are read at import time, so the .env file has to be loaded and the
# state paths decided before anything from the intake package is imported.
load_dotenv()

STATE_DIR = Path(os.getenv("STATE_DIR", "./state")).expanduser()
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs")).expanduser()
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Point the storage layer at the state directory, unless someone has already
# set an explicit path themselves.
os.environ.setdefault("INTAKE_STATE_FILE", str(STATE_DIR / "intake_state.json"))
os.environ.setdefault("INTAKE_SUBSCRIPTIONS_FILE", str(STATE_DIR / "intake_subscriptions.json"))

import discord  # noqa: E402  (must come after load_dotenv)
from discord.ext import commands  # noqa: E402

import intake  # noqa: E402
from intake import config, permissions, sync, tickets  # noqa: E402

log = logging.getLogger("intake.main")


def configure_logging() -> None:
    """Log to the console and to a rotating file in LOG_DIR.

    Print statements vanish the moment the terminal closes. When something
    breaks during a tournament, logs/intake.log is what you go and read.
    """
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "intake.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), file_handler],
    )
    # discord.py logs every heartbeat at INFO, which drowns out everything else.
    logging.getLogger("discord").setLevel(logging.WARNING)


def handle_background_exception(loop, context: dict) -> None:
    """Catch errors from tasks nobody is awaiting, which would otherwise be silent."""
    error = context.get("exception")
    message = context.get("message", "unknown error")
    if error:
        log.error("Unhandled error in a background task: %s", message, exc_info=error)
    else:
        log.error("Unhandled error in a background task: %s", message)


intents = discord.Intents.default()
intents.members = True  # needed to grant ticket access to specific members
bot = discord.Bot(intents=intents)


class TicketingCog(commands.Cog):
    """Staff commands for running the intake system."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def _reject_non_managers(self, ctx: discord.ApplicationContext) -> bool:
        """Returns True (and replies) if the caller is not allowed to do this."""
        if permissions.is_manager(ctx.author):
            return False
        await ctx.respond("You need a manager role to use this command.", ephemeral=True)
        return True

    @discord.slash_command(name="post_intake", description="Post the intake form prompt in a channel")
    async def post_intake(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.Option(
            discord.TextChannel, "Where to post it (defaults to here)", required=False
        ) = None,
        guidelines: discord.Option(
            bool, "Also post the 'before you submit' guidelines", required=False
        ) = True,
    ):
        if await self._reject_non_managers(ctx):
            return

        target = channel or ctx.channel
        await ctx.defer(ephemeral=True)
        try:
            await intake.post_intake_prompt(target, with_guidelines=guidelines)
        except discord.Forbidden:
            await ctx.respond(f"I cannot post in {target.mention}. Check my permissions.", ephemeral=True)
        except Exception as error:
            log.exception("Failed to post the intake prompt")
            await ctx.respond(f"Failed to post the intake prompt: {error}", ephemeral=True)
        else:
            await ctx.respond(f"Intake prompt posted in {target.mention}.", ephemeral=True)

    @discord.slash_command(name="intake_status", description="Show intake bot health and configuration")
    async def intake_status(self, ctx: discord.ApplicationContext):
        if await self._reject_non_managers(ctx):
            return
        await ctx.respond(embed=sync.build_status_embed(), ephemeral=True)

    @discord.slash_command(name="intake_sync", description="Check the response sheet right now")
    async def intake_sync(self, ctx: discord.ApplicationContext):
        if await self._reject_non_managers(ctx):
            return

        await ctx.defer(ephemeral=True)
        try:
            created = await sync.run_once(self.bot)
        except Exception as error:
            log.exception("Manual intake sync failed")
            await ctx.respond(f"Sync failed: {error}", ephemeral=True)
            return

        await ctx.respond(
            f"Opened {created} ticket(s). {sync.STATUS.pending} response(s) still waiting."
            if created
            else "No new responses to process.",
            ephemeral=True,
        )

    @discord.slash_command(name="close_ticket", description="Close and archive the ticket you are in")
    async def close_ticket(
        self,
        ctx: discord.ApplicationContext,
        reason: discord.Option(str, "Optional closing note", required=False) = "",
    ):
        if await self._reject_non_managers(ctx):
            return

        category_ids = {config.TICKET_CATEGORY_ID, *config.OVERFLOW_CATEGORY_IDS}
        parent_id = getattr(ctx.channel.category, "id", None)
        if parent_id not in category_ids:
            await ctx.respond("This does not look like an intake ticket channel.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        result = await tickets.close_ticket(ctx.channel, ctx.author, reason)
        await ctx.respond(result.describe(), ephemeral=True)


bot.add_cog(TicketingCog(bot))


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

    # Persistent views must be re-registered after every restart so the buttons
    # on existing messages keep working. on_ready fires again on reconnect, so
    # this only runs the first time.
    if not getattr(bot, "_intake_ready", False):
        bot._intake_ready = True

        asyncio.get_running_loop().set_exception_handler(handle_background_exception)
        bot.add_view(intake.IntakeFormView())
        bot.add_view(intake.TicketControlsView())

        for warning in config.configuration_warnings():
            log.warning("%s", warning)

        if sync.is_configured():
            bot._intake_sync_task = asyncio.create_task(sync.sync_loop(bot), name="intake_sync")
        else:
            log.error(
                "Sheet sync is off: set INTAKE_SHEET_ID and INTAKE_TICKET_CATEGORY_ID to enable it."
            )


@bot.event
async def on_disconnect():
    log.warning("Disconnected from Discord, reconnecting automatically")


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: Exception):
    """Make sure a failing command says so instead of showing 'application did not respond'."""
    log.exception("Command /%s failed", ctx.command.qualified_name if ctx.command else "?", exc_info=error)
    message = "Something went wrong running that command. A manager should check logs/intake.log."
    try:
        if ctx.response.is_done():
            await ctx.followup.send(message, ephemeral=True)
        else:
            await ctx.respond(message, ephemeral=True)
    except Exception:
        log.exception("Could not report the command failure back to Discord")


def main() -> None:
    configure_logging()

    if missing := config.missing_required_settings():
        raise SystemExit(
            "Missing required settings: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in."
        )

    log.info("Starting intake bot")
    bot.run(os.getenv("TOKEN"))


if __name__ == "__main__":
    main()
