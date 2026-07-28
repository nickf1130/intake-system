"""Building the embeds the bot posts.

The old version turned every single form question into its own full-width
field, which pushed the actual problem to the bottom of a very tall message
and blew past Discord's 25-field limit on longer forms. This version leads
with the issue, keeps the routing details compact and inline, and folds
everything else into a short "Additional details" block.
"""

from __future__ import annotations

import discord

from .sheets import FormResponse

# Discord's hard limits. Going over any of them makes the whole send fail, so
# every piece of text is shortened before it goes in.
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FIELDS = 25
MAX_TOTAL = 6000

# Leave headroom so a later edit (claiming, closing) can never tip an embed
# that was fine at post time over the total limit.
TOTAL_BUDGET = MAX_TOTAL - 400

COLOUR_OPEN = 0x2D98DA
COLOUR_CLAIMED = 0xF7B731
COLOUR_CLOSED = 0x778CA3
COLOUR_PROMPT = 0x4B7BEC

STATUS_FIELD = "Status"
CLAIMED_FIELD = "Claimed by"

# Phrases used to recognise the important questions on the form.
ISSUE_PHRASES = (
    "please describe what you need help with in detail",
    "describe what you need help with",
    "what do you need help with",
    "need help with",
    "describe your issue",
    "issue",
)
AFFILIATION_PHRASES = ("affiliated with rit esports", "affiliated")
OPS_TEAM_PHRASES = ("what operations teams", "operations teams")
COMP_TEAM_PHRASES = ("what competitive teams", "competitive teams", "competitive team")
IDENTITY_PHRASES = (
    "timestamp",
    "email address",
    "email",
    "name",
    "discord id",
    "discord handle",
    "discord",
    "gamer tag",
)


def shorten(text: str, limit: int) -> str:
    """Trim text to fit a Discord limit, marking it if anything was cut."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def set_field(embed: discord.Embed, name: str, value: str, inline: bool = True) -> bool:
    """Add a field, or update it in place if a field with that name exists.

    Returns False if the field did not fit. Dropping a field is always better
    than exceeding a limit, because going over makes Discord reject the entire
    message and the ticket never gets posted at all.
    """
    name = shorten(name, MAX_FIELD_NAME)
    value = shorten(value, MAX_FIELD_VALUE) or "-"

    for index, existing in enumerate(embed.fields):
        if existing.name == name:
            embed.set_field_at(index, name=name, value=value, inline=inline)
            return True

    if len(embed.fields) >= MAX_FIELDS or len(embed) + len(name) + len(value) > TOTAL_BUDGET:
        return False
    embed.add_field(name=name, value=value, inline=inline)
    return True


def _is_yes(answer: str) -> bool:
    return answer.strip().lower().startswith("y")


def _detail_lines(response: FormResponse, shown: set[str]) -> list[str]:
    """Format the leftover questions as short "question then answer" lines."""
    is_affiliated = _is_yes(response.answer_matching(*AFFILIATION_PHRASES))

    # The form has a branch for affiliated members and one for everyone else.
    # Only show the branch that applies to this submitter.
    affiliated_only = set(response.questions_matching("if you are affiliated"))
    unaffiliated_only = set(response.questions_matching("if you are not affiliated"))

    lines = []
    for question, answer in response.answers.items():
        answer = answer.strip()
        if not answer or question in shown:
            continue
        if question in (unaffiliated_only if is_affiliated else affiliated_only):
            continue
        lines.append(f"**{shorten(question, 120)}**\n{shorten(answer, 400)}")
    return lines


def _add_details(embed: discord.Embed, lines: list[str]) -> bool:
    """Pack the detail lines into as few fields as they will fit in.

    Returns False if anything had to be left out, so the caller can point
    staff at the sheet for the full answers.
    """
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > MAX_FIELD_VALUE:
            if current:
                chunks.append(current)
            current = shorten(line, MAX_FIELD_VALUE)
        else:
            current = candidate
    if current:
        chunks.append(current)

    everything_fitted = True
    for index, chunk in enumerate(chunks):
        label = "Additional details" if index == 0 else f"Additional details ({index + 1})"
        if not set_field(embed, label, chunk, inline=False):
            everything_fitted = False
    return everything_fitted


def build_ticket_embed(
    response: FormResponse,
    *,
    department: str,
    submitter_label: str,
    team_names: list[str],
) -> discord.Embed:
    """The main embed posted at the top of a new ticket channel."""
    shown: set[str] = set()

    issue_question = response.question_matching(*ISSUE_PHRASES)
    if issue_question:
        shown.add(issue_question)
        issue_text = response.answers[issue_question].strip()
    else:
        issue_text = "_No description was provided on the form._"

    embed = discord.Embed(
        title=shorten(f"{department} Intake Request", MAX_TITLE),
        description=shorten(issue_text, MAX_DESCRIPTION),
        colour=COLOUR_OPEN,
        timestamp=discord.utils.utcnow(),
    )

    set_field(embed, "Submitted by", submitter_label)
    set_field(embed, STATUS_FIELD, "Open - waiting for a manager")

    if affiliation_question := response.question_matching(*AFFILIATION_PHRASES):
        shown.add(affiliation_question)
        set_field(embed, "RIT Esports member", response.answers[affiliation_question].strip())

    if submitted_at := response.answer_matching("timestamp"):
        set_field(embed, "Submitted at", submitted_at)
    shown.update(response.questions_matching("timestamp"))

    if contact := response.answer_matching("email address", "email"):
        set_field(embed, "Contact", contact)
    shown.update(response.questions_matching("email address", "email"))

    # The team questions are summarised in one field instead of two, and the
    # names shown are the ones the bot actually matched and routed to.
    if team_names:
        set_field(embed, "Routed to", ", ".join(team_names), inline=False)
    shown.update(response.questions_matching(*OPS_TEAM_PHRASES, *COMP_TEAM_PHRASES))

    everything_fitted = _add_details(embed, _detail_lines(response, shown))

    footer = f"Sheet row {response.row_number}  |  ref {response.fingerprint}"
    if not everything_fitted:
        footer += "  |  some answers were too long to show - see the sheet"
    embed.set_footer(text=shorten(footer, 2048))
    return embed


def mark_claimed(embed: discord.Embed, claimed_by: discord.abc.User) -> discord.Embed:
    """Update a ticket embed to show it has been picked up."""
    embed.colour = COLOUR_CLAIMED
    set_field(embed, STATUS_FIELD, "Claimed")
    set_field(embed, CLAIMED_FIELD, claimed_by.mention)
    return embed


def mark_closed(embed: discord.Embed, closed_by: discord.abc.User, reason: str = "") -> discord.Embed:
    """Update a ticket embed to show it has been resolved."""
    embed.colour = COLOUR_CLOSED
    set_field(embed, STATUS_FIELD, f"Closed by {closed_by.mention}")
    if reason:
        set_field(embed, "Closing note", reason, inline=False)
    return embed


def build_prompt_embed() -> discord.Embed:
    """The permanent message with the "open the form" button."""
    embed = discord.Embed(
        title="Need help from RIT Esports staff?",
        description=(
            "Press the button below and the bot will send you a private link to "
            "the intake form.\n\n"
            "Once you submit it, a private channel is opened with the managers "
            "who can help, and you will be added to it."
        ),
        colour=COLOUR_PROMPT,
    )
    embed.set_footer(text="Bot broken or need further help? DM @hoaxcs")
    return embed


def build_guidelines_embed() -> discord.Embed:
    """Posted above the prompt so people know what makes a useful ticket."""
    return discord.Embed(
        title="Before you submit",
        colour=COLOUR_PROMPT,
        description=(
            "A little detail up front saves a lot of back and forth:\n\n"
            "- **Be specific.** Include dates, names, and what you have already tried.\n"
            "- **Pick the right team(s)** so the people who can actually help get pinged.\n"
            "- **One issue per ticket.** Separate problems are easier to track separately.\n"
            "- **Keep it brief.** A short paragraph is usually plenty."
        ),
    )


def build_transcript_embed(
    channel_name: str,
    *,
    closed_by: discord.abc.User,
    reason: str,
    message_count: int,
    opened_at: str,
) -> discord.Embed:
    """The summary posted alongside a transcript file in the staff log."""
    embed = discord.Embed(
        title=f"Ticket closed: {shorten(channel_name, 200)}",
        colour=COLOUR_CLOSED,
        timestamp=discord.utils.utcnow(),
    )
    set_field(embed, "Closed by", closed_by.mention)
    set_field(embed, "Messages", str(message_count))
    set_field(embed, "Opened", opened_at)
    if reason:
        set_field(embed, "Reason", reason, inline=False)
    return embed
