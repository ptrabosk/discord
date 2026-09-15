import logging
import os
from typing import Any

import discord


logger = logging.getLogger(__name__)

UNAUTHORIZED_MESSAGE = "This bot is not authorized for this server."

_authorized_guild_id: int | None = None


def load_authorized_guild_id() -> int:
    """Load and validate the only guild in which the bot may operate."""
    raw_guild_id = os.getenv("DISCORD_GUILD_ID")
    if raw_guild_id is None or not raw_guild_id.strip():
        raise RuntimeError("DISCORD_GUILD_ID is required in .env")

    value = raw_guild_id.strip()
    if not value.isdecimal() or int(value) <= 0:
        raise RuntimeError("DISCORD_GUILD_ID in .env must be a positive numeric Discord guild ID")

    global _authorized_guild_id
    _authorized_guild_id = int(value)
    logger.info("Authorized guild loaded: %s", _authorized_guild_id)
    return _authorized_guild_id


def get_authorized_guild_id() -> int:
    if _authorized_guild_id is None:
        raise RuntimeError("Authorized guild has not been loaded")
    return _authorized_guild_id


def is_authorized_guild(guild_or_id: Any) -> bool:
    """Return whether a guild object or ID is the configured authorized guild."""
    if _authorized_guild_id is None or guild_or_id is None:
        return False
    guild_id = getattr(guild_or_id, "id", guild_or_id)
    try:
        return int(guild_id) == _authorized_guild_id
    except (TypeError, ValueError):
        return False


async def require_authorized_interaction(interaction: discord.Interaction) -> bool:
    """Block an interaction unless it belongs to the authorized guild."""
    if is_authorized_guild(interaction.guild_id):
        return True

    logger.warning(
        "Unauthorized interaction blocked: guild_id=%s user_id=%s type=%s",
        interaction.guild_id,
        getattr(interaction.user, "id", None),
        interaction.type,
    )
    if interaction.response.is_done():
        await interaction.followup.send(UNAUTHORIZED_MESSAGE, ephemeral=True)
    else:
        await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
    return False

