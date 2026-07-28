"""Working out who a ticket belongs to, then opening and closing its channel.

A form response arrives as free text, so most of this file is turning that
text into concrete Discord IDs: which department, which teams, which roles to
ping, and who gets to see the channel.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field

import discord

from . import config, embeds, storage
from .sheets import FormResponse

log = logging.getLogger(__name__)

# Discord refuses to put more than 50 channels in one category. The bot moves
# to INTAKE_OVERFLOW_CATEGORY_IDS when the main one fills up.
CHANNELS_PER_CATEGORY_LIMIT = 50

MAX_CHANNEL_NAME = 100
TRANSCRIPT_MESSAGE_LIMIT = 2000

# A Discord ID is 17-20 digits. Requiring the whole answer to be one stops a
# phone number or a gamertag like "sniper12345678901234" being read as an ID.
DISCORD_ID_PATTERN = re.compile(r"^<@!?(\d{17,20})>$|^(\d{17,20})$")

# Background delete tasks, kept here so Python does not garbage-collect them.
_pending_deletions: set[asyncio.Task] = set()


class TicketError(Exception):
    """Something went wrong opening a ticket."""


class TemporaryTicketError(TicketError):
    """Worth retrying on the next poll - rate limits, a full category, an outage."""


class PermanentTicketError(TicketError):
    """This response will never post successfully, so stop retrying it.

    Without this the sync loop would create a channel, fail to post into it,
    leave the response unhandled, and do the whole thing again a minute later.
    """

@dataclass
class TicketRequest:
    """Everything the bot worked out about one form response."""

    response: FormResponse
    department: str
    submitter_id: int | None
    submitter_label: str  # rendered in the embed: a mention, or a plain name
    submitter_slug: str  # used in the channel name
    teams: list[config.Team] = field(default_factory=list)
    ping_role_ids: list[int] = field(default_factory=list)
    ping_user_ids: list[int] = field(default_factory=list)

    @property
    def team_names(self) -> list[str]:
        return [team.name for team in self.teams]

    @property
    def member_ids(self) -> list[int]:
        """Everyone who should be able to see the channel."""
        ids = list(self.ping_user_ids)
        if self.submitter_id:
            ids.append(self.submitter_id)
        return list(dict.fromkeys(ids))

    def build_mentions(self) -> str:
        """The ping line posted with the ticket."""
        parts = []
        if self.submitter_id:
            parts.append(f"<@{self.submitter_id}>")
        parts.extend(f"<@&{role_id}>" for role_id in self.ping_role_ids)
        parts.extend(f"<@{user_id}>" for user_id in self.ping_user_ids)
        return " ".join(dict.fromkeys(parts))


def parse_discord_id(value: str) -> int | None:
    """Read a Discord user ID out of an answer, if that is clearly what it is."""
    match = DISCORD_ID_PATTERN.match(value.strip())
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def resolve_department(answer: str) -> tuple[str, int | None]:
    """Map the "which branch" answer to a department name and role to ping."""
    lowered = answer.strip().lower()
    if any(keyword in lowered for keyword in config.OPS_KEYWORDS):
        return "Operations", config.OPS_ROLE_ID or None
    if any(keyword in lowered for keyword in config.COMP_KEYWORDS):
        return "Competitive", config.COMP_ROLE_ID or None
    return "General", None


def resolve_ping_role(approver_answer: str, fallback_role_id: int | None) -> int | None:
    """Pick the role to ping from the "who should help" answer.

    Naming the president, VP or treasurer routes to the secretary instead,
    which is how the form is worded.
    """
    lowered = approver_answer.strip().lower()
    if not lowered:
        return fallback_role_id
    if any(keyword in lowered for keyword in config.SECRETARY_KEYWORDS) and config.SECRETARY_ROLE_ID:
        return config.SECRETARY_ROLE_ID
    if "operation" in lowered and config.OPS_ROLE_ID:
        return config.OPS_ROLE_ID
    if "competitive" in lowered and config.COMP_ROLE_ID:
        return config.COMP_ROLE_ID
    return fallback_role_id


def resolve_submitter(response: FormResponse) -> tuple[int | None, str, str]:
    """Identify who filed this ticket.

    Returns their Discord ID (if the form captured one), a label for the embed,
    and a short slug for the channel name.
    """
    candidates = [
        response.answer_to(config.DISCORD_ID_FIELD),
        response.answer_to(config.USER_FIELD),
        response.answer_matching("discord id", "discord handle", "gamer tag", "discord"),
        response.answer_matching("name"),
        response.answer_matching("email address", "email"),
    ]
    answers = [value for value in candidates if value]

    for value in answers:
        if user_id := parse_discord_id(value):
            # Prefer a readable name for the channel over a wall of digits.
            names = [other for other in answers if not parse_discord_id(other)]
            slug = names[0] if names else str(user_id)
            return user_id, f"<@{user_id}>", slug

    if answers:
        return None, embeds.shorten(answers[0], 80), answers[0]
    return None, "Unknown submitter", "unknown"


def build_request(response: FormResponse) -> TicketRequest:
    """Turn a raw form response into everything needed to open its ticket."""
    department_answer = response.answer_to(config.DEPARTMENT_FIELD) or response.answer_matching(
        "branch do you require help", "operations competitive", "branch"
    )
    department, department_role_id = resolve_department(department_answer)

    approver_answer = response.answer_to(config.APPROVER_FIELD) or response.answer_matching(
        "most appropriate to help", "who would be most"
    )
    primary_role_id = resolve_ping_role(approver_answer, department_role_id)

    comp_answer = response.answer_to(config.COMP_SUPPORT_FIELD) or response.answer_matching(
        "competitive team s do you need support", "competitive teams", "competitive team"
    )
    ops_answer = response.answer_to(config.OPS_SUPPORT_FIELD) or response.answer_matching(
        "operations teams do you need support", "operations teams"
    )

    teams = config.find_teams(comp_answer, config.COMP_TEAMS)
    teams += config.find_teams(ops_answer, config.OPS_TEAMS)

    role_ids = [primary_role_id] if primary_role_id else []
    role_ids += [team.role_id for team in teams if team.role_id]

    user_ids: list[int] = []
    for team in teams:
        user_ids.extend(team.user_ids)

    submitter_id, submitter_label, submitter_slug = resolve_submitter(response)

    return TicketRequest(
        response=response,
        department=department,
        submitter_id=submitter_id,
        submitter_label=submitter_label,
        submitter_slug=submitter_slug,
        teams=teams,
        ping_role_ids=list(dict.fromkeys(role_ids)),
        ping_user_ids=list(dict.fromkeys(user_ids)),
    )

def build_channel_name(request: TicketRequest) -> str:
    """A valid channel name like "intake-competitive-jordan-84".

    Discord silently mangles characters it does not allow, so the name is
    cleaned here instead. The row number on the end keeps repeat submissions
    from the same person distinguishable.
    """
    raw = f"intake-{request.department}-{request.submitter_slug}".lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw).strip("-") or "intake"
    suffix = f"-{request.response.row_number}"
    return cleaned[: MAX_CHANNEL_NAME - len(suffix)] + suffix


def _base_overwrites(guild: discord.Guild) -> dict:
    """Hide the channel from everyone, then let the bot manage it."""
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
        )
    return overwrites


def build_ticket_overwrites(guild: discord.Guild, request: TicketRequest) -> dict:
    """Grant access to the submitter, the routed teams, and the pinged roles."""
    overwrites = _base_overwrites(guild)
    can_participate = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True
    )

    for role_id in request.ping_role_ids:
        if role := guild.get_role(role_id):
            overwrites[role] = can_participate

    # The submitter is included here. Leaving them out was a long-standing bug:
    # they were pinged into a channel they could not open.
    for user_id in request.member_ids:
        if member := guild.get_member(user_id):
            overwrites[member] = can_participate
        else:
            log.warning("Could not find member %s in %s to grant ticket access", user_id, guild.name)

    return overwrites


def build_closed_overwrites(guild: discord.Guild) -> dict:
    """Read-only for the closing role, invisible to everyone else."""
    overwrites = _base_overwrites(guild)
    if config.CLOSE_ROLE_ID and (role := guild.get_role(config.CLOSE_ROLE_ID)):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True
        )
    return overwrites


def find_available_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    """The first configured category with room for another channel."""
    for category_id in [config.TICKET_CATEGORY_ID, *config.OVERFLOW_CATEGORY_IDS]:
        if not category_id:
            continue
        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            log.warning("Category %s does not exist or is not a category", category_id)
            continue
        if len(category.channels) < CHANNELS_PER_CATEGORY_LIMIT:
            return category
        log.info("Category %s is full (%s channels)", category.name, len(category.channels))
    return None


def get_primary_category(client: discord.Client) -> discord.CategoryChannel | None:
    """The main ticket category, used to find the guild the bot works in."""
    category = client.get_channel(config.TICKET_CATEGORY_ID)
    return category if isinstance(category, discord.CategoryChannel) else None

def _is_retryable(error: discord.HTTPException) -> bool:
    """Rate limits and Discord outages are worth another go; bad data is not."""
    return error.status == 429 or error.status >= 500


async def open_ticket(
    guild: discord.Guild, request: TicketRequest, view: discord.ui.View
) -> discord.TextChannel:
    """Create the ticket channel and post the submission into it.

    If the channel is created but the post fails, the empty channel is deleted
    again so a retry cannot leave a trail of blank channels behind.
    """
    category = find_available_category(guild)
    if category is None:
        raise TemporaryTicketError(
            "Every configured ticket category is full (Discord allows 50 channels each). "
            "Close some tickets or add INTAKE_OVERFLOW_CATEGORY_IDS."
        )

    try:
        channel = await guild.create_text_channel(
            name=build_channel_name(request),
            category=category,
            overwrites=build_ticket_overwrites(guild, request),
            topic=f"Intake ticket for {request.submitter_slug} | sheet row {request.response.row_number}",
            reason="New intake form submission",
        )
    except discord.Forbidden as error:
        raise TemporaryTicketError(
            f"Missing permission to create channels in {category.name}: {error}"
        ) from error
    except discord.HTTPException as error:
        raise TemporaryTicketError(f"Discord refused to create the channel: {error}") from error

    embed = embeds.build_ticket_embed(
        request.response,
        department=request.department,
        submitter_label=request.submitter_label,
        team_names=request.team_names,
    )

    try:
        await channel.send(
            content=request.build_mentions() or None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
    except discord.HTTPException as error:
        await _discard_channel(channel)
        if _is_retryable(error):
            raise TemporaryTicketError(f"Could not post into the new ticket: {error}") from error
        raise PermanentTicketError(f"Discord rejected this submission's message: {error}") from error
    except Exception as error:
        await _discard_channel(channel)
        raise PermanentTicketError(f"Could not post into the new ticket: {error}") from error

    return channel


async def _discard_channel(channel: discord.TextChannel) -> None:
    try:
        await channel.delete(reason="Intake: failed to post the submission")
    except Exception as error:
        log.error("Left an empty ticket channel #%s behind: %s", channel.name, error)

@dataclass
class CloseResult:
    """What actually happened, so the bot can report it honestly."""

    locked: bool = False
    transcript_saved: bool = False
    deleting_in_seconds: int | None = None
    problems: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = []
        lines.append("Channel locked." if self.locked else "Could not lock the channel.")
        if self.transcript_saved:
            lines.append("Transcript saved to the staff log.")
        elif config.TRANSCRIPT_CHANNEL_ID:
            lines.append("Transcript could **not** be saved.")
        else:
            lines.append("No transcript channel configured, so nothing was archived.")

        if self.deleting_in_seconds is not None:
            lines.append(f"This channel will be deleted in {self.deleting_in_seconds} seconds.")
        else:
            lines.append("This channel will be kept.")

        for problem in self.problems:
            lines.append(f"- {problem}")
        return "\n".join(lines)


def _describe_embed(embed: discord.Embed) -> list[str]:
    """Flatten an embed into plain text lines for the transcript."""
    lines = []
    if embed.title:
        lines.append(f"    [embed] {embed.title}")
    if embed.description:
        lines.append(f"    {embed.description}")
    for embed_field in embed.fields:
        lines.append(f"    {embed_field.name}: {embed_field.value}")
    return lines


async def build_transcript(channel: discord.TextChannel) -> tuple[str, int]:
    """Render the channel's history as plain text. Returns the text and a count."""
    lines = [
        f"Transcript of #{channel.name}",
        f"Channel ID: {channel.id}",
        f"Opened: {channel.created_at:%Y-%m-%d %H:%M UTC}",
        "=" * 70,
        "",
    ]

    message_count = 0
    async for message in channel.history(limit=TRANSCRIPT_MESSAGE_LIMIT, oldest_first=True):
        message_count += 1
        timestamp = f"{message.created_at:%Y-%m-%d %H:%M}"
        lines.append(f"[{timestamp}] {message.author} ({message.author.id}):")
        if message.content:
            lines.append(f"    {message.content}")
        for embed in message.embeds:
            lines.extend(_describe_embed(embed))
        for attachment in message.attachments:
            lines.append(f"    [attachment] {attachment.filename} - {attachment.url}")
        lines.append("")

    return "\n".join(lines), message_count


async def _save_transcript(
    channel: discord.TextChannel, closed_by: discord.abc.User, reason: str
) -> tuple[bool, str]:
    """Post the transcript to the staff log. Returns (saved, problem)."""
    log_channel = channel.guild.get_channel(config.TRANSCRIPT_CHANNEL_ID)
    if not isinstance(log_channel, discord.TextChannel):
        return False, f"Transcript channel {config.TRANSCRIPT_CHANNEL_ID} is not a text channel I can see."

    try:
        transcript, message_count = await build_transcript(channel)
        summary = embeds.build_transcript_embed(
            channel.name,
            closed_by=closed_by,
            reason=reason,
            message_count=message_count,
            opened_at=f"{channel.created_at:%Y-%m-%d %H:%M UTC}",
        )
        await log_channel.send(
            embed=summary,
            file=discord.File(
                io.BytesIO(transcript.encode("utf-8")),
                filename=f"{channel.name}-{channel.id}.txt",
            ),
        )
        return True, ""
    except Exception as error:
        log.exception("Failed to archive #%s", channel.name)
        return False, f"Could not save the transcript: {error}"


async def _delete_after(channel: discord.TextChannel, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        await channel.delete(reason="Intake ticket closed and archived")
    except discord.NotFound:
        pass  # somebody deleted it by hand first
    except Exception as error:
        log.error("Could not delete #%s after closing: %s", channel.name, error)


async def _announce_closing(
    channel: discord.TextChannel, closed_by: discord.abc.User, reason: str
) -> None:
    """Post the closing notice while the submitter can still read the channel."""
    note = f"\n> {reason}" if reason else ""
    if config.DELETE_ON_CLOSE and config.TRANSCRIPT_CHANNEL_ID:
        outcome = (
            f"\nA transcript is being saved, and this channel will be removed in about "
            f"{config.DELETE_DELAY_SECONDS} seconds."
        )
    else:
        outcome = "\nThis channel is now locked."

    try:
        await channel.send(f"**This ticket was closed by {closed_by.mention}.**{note}{outcome}")
    except Exception:
        log.warning("Could not post the closing notice in #%s", channel.name)


async def close_ticket(
    channel: discord.TextChannel, closed_by: discord.abc.User, reason: str = ""
) -> CloseResult:
    """Lock a ticket, archive it, and schedule its deletion.

    The channel is only ever deleted once a transcript has been safely stored,
    so a failure here can never destroy the only copy of a conversation.
    """
    result = CloseResult()

    # Announce first: locking removes the submitter's access, so anything sent
    # afterwards would only ever be seen by staff.
    await _announce_closing(channel, closed_by, reason)

    try:
        await channel.edit(
            overwrites=build_closed_overwrites(channel.guild),
            reason=f"Intake ticket closed by {closed_by}",
        )
        result.locked = True
    except Exception as error:
        log.exception("Could not lock #%s", channel.name)
        result.problems.append(f"Could not change channel permissions: {error}")

    if config.TRANSCRIPT_CHANNEL_ID:
        result.transcript_saved, problem = await _save_transcript(channel, closed_by, reason)
        if problem:
            result.problems.append(problem)

    await storage.clear_subscriptions(channel.id)

    if config.DELETE_ON_CLOSE and result.transcript_saved:
        result.deleting_in_seconds = config.DELETE_DELAY_SECONDS
        task = asyncio.create_task(_delete_after(channel, config.DELETE_DELAY_SECONDS))
        _pending_deletions.add(task)
        task.add_done_callback(_pending_deletions.discard)
    elif config.DELETE_ON_CLOSE:
        result.problems.append("Keeping the channel because no transcript was saved.")

    return result
