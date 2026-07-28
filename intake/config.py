"""Every setting the intake bot reads from the environment.

This is the only module that touches ``os.environ``. When the bot misbehaves,
this file plus ``.env.example`` should explain why: each setting is read once
at import time, and anything unset falls back to a documented default.

Nothing in here has a real Discord or Google ID baked in as a default. Point
the bot at your server through ``.env`` and nowhere else.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

def get_text(name: str, default: str = "") -> str:
    """Read a text setting. Missing or blank values fall back to `default`."""
    return os.getenv(name, "").strip() or default


def get_int(name: str, default: int = 0) -> int:
    """Read a whole-number setting, such as a Discord role or channel ID."""
    raw = get_text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s should be a number but is %r - using %s instead", name, raw, default)
        return default


def get_bool(name: str, default: bool = False) -> bool:
    """Read an on/off setting. Accepts 1, true, yes or on in any casing."""
    raw = get_text(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_keyword_list(name: str, default: str = "") -> list[str]:
    """Read a comma-separated list of keywords, lowercased."""
    return [word.strip().lower() for word in get_text(name, default).split(",") if word.strip()]


def get_id_list(name: str) -> list[int]:
    """Read a list of Discord IDs, accepting commas, spaces and <@123> mentions."""
    ids: list[int] = []
    for part in re.split(r"[,\s]+", get_text(name)):
        digits = re.sub(r"\D", "", part)
        if digits:
            ids.append(int(digits))
    return list(dict.fromkeys(ids))  # drop duplicates, keep the original order

FORM_URL = get_text("INTAKE_FORM_URL")
SHEET_ID = get_text("INTAKE_SHEET_ID")
SHEET_TAB = get_text("INTAKE_SHEET_TAB")
SHEET_GID = get_text("INTAKE_SHEET_GID", "0")
SERVICE_ACCOUNT_FILE = get_text("GOOGLE_SERVICE_ACCOUNT_FILE")

# Reading the sheet over public CSV requires it to be shared with "anyone with
# the link", which exposes every submission to anyone who learns the sheet ID.
# It stays off unless somebody deliberately turns it on.
ALLOW_PUBLIC_CSV = get_bool("INTAKE_ALLOW_PUBLIC_CSV", False)

POLL_SECONDS = max(15, get_int("INTAKE_POLL_SECONDS", 60))
MAX_TICKETS_PER_POLL = max(1, get_int("INTAKE_MAX_TICKETS_PER_POLL", 5))
PROCESS_BACKLOG = get_bool("INTAKE_PROCESS_BACKLOG", False)

# Google Form "entry.XXXX" ID of a hidden Discord ID question, if you have one.
DISCORD_ID_ENTRY = get_text("INTAKE_DISCORD_ID_ENTRY")

TICKET_CATEGORY_ID = get_int("INTAKE_TICKET_CATEGORY_ID")
OVERFLOW_CATEGORY_IDS = get_id_list("INTAKE_OVERFLOW_CATEGORY_IDS")
TRANSCRIPT_CHANNEL_ID = get_int("INTAKE_TRANSCRIPT_CHANNEL_ID")
DELETE_ON_CLOSE = get_bool("INTAKE_DELETE_ON_CLOSE", True)
DELETE_DELAY_SECONDS = max(0, get_int("INTAKE_DELETE_DELAY_SECONDS", 60))

OPS_ROLE_ID = get_int("OPS_ROLE_ID")
COMP_ROLE_ID = get_int("COMP_ROLE_ID")
SECRETARY_ROLE_ID = get_int("SECRETARY_ROLE_ID")
CLOSE_ROLE_ID = get_int("CLOSE_ROLE_ID")

# I cba to make this cleaner, just read the form directly.

DEPARTMENT_FIELD = get_text(
    "INTAKE_DEPARTMENT_FIELD",
    "What branch do you require help from (operations/competitive)",
)
USER_FIELD = get_text("INTAKE_USER_FIELD", "Discord handle")
DISCORD_ID_FIELD = get_text("INTAKE_DISCORD_ID_FIELD", "Discord ID")
COMP_SUPPORT_FIELD = get_text(
    "INTAKE_COMP_SUPPORT_FIELD",
    "What Competitive Team(s) do you need support from?",
)
OPS_SUPPORT_FIELD = get_text(
    "INTAKE_OPS_SUPPORT_FIELD",
    "What Operations Teams do you need support from?",
)
APPROVER_FIELD = get_text("INTAKE_APPROVER_FIELD", "Who would be most appropriate to help")

OPS_KEYWORDS = get_keyword_list("INTAKE_OPS_KEYS", "ops,operations")
COMP_KEYWORDS = get_keyword_list("INTAKE_COMP_KEYS", "comp,competitive")
SECRETARY_KEYWORDS = get_keyword_list("INTAKE_SECRETARY_KEYS", "president,vp,treasurer")

STATE_FILE = get_text("INTAKE_STATE_FILE", "intake_state.json")
SUBSCRIPTIONS_FILE = get_text("INTAKE_SUBSCRIPTIONS_FILE", "intake_subscriptions.json")

@dataclass(frozen=True)
class Team:
    """One competitive or operations team, and who to notify about it."""

    name: str
    aliases: tuple[str, ...]
    role_id: int
    user_ids: tuple[int, ...]

    @property
    def is_routed(self) -> bool:
        """True if anyone at all is notified when this team is picked."""
        return bool(self.role_id or self.user_ids)


# Team name -> other spellings members type on the form.
OPS_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "Broadcasting": ("broadcast", "stream", "streaming"),
    "Event Planning": ("events", "event"),
    "Marketing": (),
    "Graphics": ("graphic design", "design", "art"),
    "Social Media": ("socials", "social"),
    "Community": ("community management",),
}

COMP_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "Call of Duty": ("cod",),
    "Counter-Strike 2": ("counter strike 2", "counterstrike", "counter strike", "cs2", "csgo", "cs"),
    "Guilty Gear: Strive": ("guilty gear strive", "guilty gear", "ggst"),
    "Halo": (),
    "League of Legends": ("league", "lol"),
    "Marvel Rivals": ("rivals",),
    "osu!": ("osu",),
    "Overwatch 2": ("overwatch", "ow2", "ow"),
    "Rainbow Six Siege": ("rainbow six", "siege", "r6"),
    "Rocket League": ("rl",),
    "Splatoon": (),
    "Teamfight Tactics": ("tft",),
    "Valorant": ("val",),
    "Apex Legends": ("apex",),
    "Chess": (),
    "Deadlock": (),
    "Dota 2": ("dota",),
    "Mario Kart": ("mario kart 8", "mariokart", "mk"),
    "Starcraft II": ("starcraft 2", "starcraft", "sc2"),
    "Super Smash Bros.": ("super smash bros", "smash", "ssbu", "melee"),
    "Team Fortress 2": ("team fortress", "tf2"),
    "Tetris": (),
}


def _build_team(name: str, aliases: tuple[str, ...], env_prefix: str) -> Team:
    """Load one team's routing from the environment.

    The env var names come from the team name in SHOUTY_SNAKE_CASE, so
    "Rocket League" with prefix "COMP_TEAM" reads COMP_TEAM_ROLE_ROCKET_LEAGUE
    and COMP_TEAM_USERS_ROCKET_LEAGUE.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return Team(
        name=name,
        aliases=aliases,
        role_id=get_int(f"{env_prefix}_ROLE_{slug}"),
        user_ids=tuple(get_id_list(f"{env_prefix}_USERS_{slug}")),
    )


OPS_TEAMS = [_build_team(name, aliases, "OPS_TEAM") for name, aliases in OPS_TEAM_ALIASES.items()]
COMP_TEAMS = [_build_team(name, aliases, "COMP_TEAM") for name, aliases in COMP_TEAM_ALIASES.items()]


def find_teams(answer: str, teams: list[Team]) -> list[Team]:
    """Pick out the teams named in a free-text form answer.

    Longer names are matched first and then blanked out of the text, so
    "Rocket League" is never also counted as "League of Legends". Matches
    respect word boundaries, so "cs" does not match "cs2" or "docs".
    """
    if not answer:
        return []

    # Every phrase that identifies a team, longest first.
    phrases = sorted(
        ((phrase.lower(), team) for team in teams for phrase in (team.name, *team.aliases)),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

    remaining_text = answer.lower()
    matched: list[Team] = []
    for phrase, team in phrases:
        if team in matched:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
        if re.search(pattern, remaining_text):
            matched.append(team)
            remaining_text = re.sub(pattern, " ", remaining_text)
    return matched

def missing_required_settings() -> list[str]:
    """Names of settings the bot cannot run without. Empty list means good to go."""
    required = {
        "TOKEN": get_text("TOKEN"),
        "INTAKE_FORM_URL": FORM_URL,
        "INTAKE_SHEET_ID": SHEET_ID,
        "INTAKE_TICKET_CATEGORY_ID": TICKET_CATEGORY_ID,
    }
    return [name for name, value in required.items() if not value]


def configuration_warnings() -> list[str]:
    """Things that will work but probably are not what you want."""
    warnings = []
    if not TRANSCRIPT_CHANNEL_ID:
        warnings.append(
            "INTAKE_TRANSCRIPT_CHANNEL_ID is not set, so closed tickets are locked "
            "and hidden rather than deleted (no transcript can be saved)."
        )
    if not (OPS_ROLE_ID or COMP_ROLE_ID):
        warnings.append(
            "Neither OPS_ROLE_ID nor COMP_ROLE_ID is set, so only members with the "
            "Manage Server permission can claim or close tickets."
        )
    if ALLOW_PUBLIC_CSV and not SERVICE_ACCOUNT_FILE:
        warnings.append(
            "INTAKE_ALLOW_PUBLIC_CSV is on, which requires the response sheet to be "
            "readable by anyone with the link. Prefer a service account."
        )
    unrouted = [team.name for team in (*OPS_TEAMS, *COMP_TEAMS) if not team.is_routed]
    if unrouted:
        warnings.append(f"No role or users configured for: {', '.join(unrouted)}")
    return warnings
