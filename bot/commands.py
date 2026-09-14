import discord
from discord import app_commands
from database.database import get_verified_user, audit
from bot.permissions import sync_member_roles

def is_admin(interaction):
    return any(r.name == "Ops Bot Admin" for r in interaction.user.roles) or interaction.user.guild_permissions.administrator

def register_commands(bot, sheet):

    @bot.tree.command(name="access_status", description="Show managed access status for a member")
    async def access_status(interaction: discord.Interaction, member: discord.Member):
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
