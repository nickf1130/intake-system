"""Who is allowed to run staff actions.

A manager is anyone holding the operations or competitive manager role, or
anyone with the Manage Server permission (which covers admins and owners).
"""

from __future__ import annotations

import discord

from . import config


def has_role(member: discord.Member, role_id: int) -> bool:
    """True if the member holds this role. An unset role ID matches nobody."""
    if not role_id:
        return False
    return any(role.id == role_id for role in getattr(member, "roles", []))


def is_manager(member: discord.Member | None) -> bool:
    """True if this member may claim, close, and administer tickets."""
    if member is None:
        return False
    if has_role(member, config.OPS_ROLE_ID) or has_role(member, config.COMP_ROLE_ID):
        return True
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)
