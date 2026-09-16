import asyncio
import logging

import discord
from database.database import check_database, get_verified_user, audit
from bot.guild_authorization import (
    UNAUTHORIZED_MESSAGE,
    get_authorized_guild_id,
    is_authorized_guild,
)
from bot.health import check_google_sheet, format_timestamp, format_uptime, smtp_configuration_status
from bot.permissions import sync_member_roles


logger = logging.getLogger(__name__)


def is_admin(interaction):
    return any(r.name == "Ops Bot Admin" for r in interaction.user.roles) or interaction.user.guild_permissions.administrator

def register_commands(bot, sheet):

    @bot.tree.command(name="access_status", description="Show managed access status for a member")
    async def access_status(interaction: discord.Interaction, member: discord.Member):
        if not is_authorized_guild(member.guild):
            await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        row = get_verified_user(member.id)
        if not row:
            await interaction.response.send_message("User is not verified.", ephemeral=True)
            return
        _, email, verified_at, last_sync = row
        record = sheet.get_by_email(email)
        await interaction.response.send_message(
            f"Member: {member.mention}\nEmail: {email}\nActive source record: {'Yes' if record else 'No'}\n"
            f"Verified: {verified_at}\nLast sync: {last_sync or 'Never'}",
            ephemeral=True
        )

    @bot.tree.command(name="access_sync", description="Synchronize one member's managed roles")
    async def access_sync(interaction: discord.Interaction, member: discord.Member):
        if not is_authorized_guild(member.guild):
            await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        row = get_verified_user(member.id)
        if not row:
            await interaction.response.send_message("User is not verified.", ephemeral=True)
            return
        email = row[1]
        record = sheet.get_by_email(email)
        added, removed = await sync_member_roles(member, record)
        audit(member.id, email, "MANUAL_SYNC", f"By={interaction.user.id}; Added={added}; Removed={removed}")
        await interaction.response.send_message(f"Added: {added or 'None'}\nRemoved: {removed or 'None'}", ephemeral=True)

    @bot.tree.command(name="health", description="Show production health for the operations bot")
    async def health(interaction: discord.Interaction):
        if not is_authorized_guild(interaction.guild_id):
            await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        database_ok, database_detail = await asyncio.to_thread(check_database)
        google_ok, google_detail = await check_google_sheet(sheet)
        if google_ok:
            logger.info("Google Sheet health check succeeded")
        else:
            logger.warning("Google Sheet health check failed: %s", google_detail)

        smtp_ok, smtp_detail = smtp_configuration_status()
        guild_connected = bot.get_guild(get_authorized_guild_id()) is not None
        access_sync = bot.access_sync
        sync_running = access_sync.loop.is_running() and not access_sync.loop.failed()

        lines = [
            "Bot status: Online",
            f"Uptime: {format_uptime(bot.started_at)}",
            f"Version: {bot.version}",
            f"Authorized guild: {'Connected' if guild_connected else 'Unavailable'}",
            f"Google Sheets: {'OK' if google_ok else 'ERROR'} ({google_detail})",
            f"Database: {'OK' if database_ok else 'ERROR'} ({database_detail})",
            f"SMTP: {'OK' if smtp_ok else 'ERROR'} ({smtp_detail})",
            f"Last successful role/access sync: {format_timestamp(access_sync.last_successful_sync)}",
            f"Last sync failure: {access_sync.last_failure or 'None'}",
            f"Background sync task: {'Running' if sync_running else 'Stopped'}",
        ]
        await interaction.edit_original_response(content="\n".join(lines))
