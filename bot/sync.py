from discord.ext import tasks
from database.database import list_verified_users, touch_sync, audit
from bot.permissions import sync_member_roles

class AccessSync:
    def __init__(self, bot, sheet, guild_id, minutes):
        self.bot = bot
        self.sheet = sheet
        self.guild_id = guild_id
        self.loop.change_interval(minutes=minutes)

    @tasks.loop(minutes=15)
    async def loop(self):
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return
        for user_id, email in list_verified_users():
            try:
                member = guild.get_member(user_id)
                if not member:
                    continue
                record = self.sheet.get_by_email(email)
                added, removed = await sync_member_roles(member, record)
                touch_sync(user_id)
                if added or removed:
                    audit(user_id, email, "ROLE_SYNC", f"Added={added}; Removed={removed}")
            except Exception as exc:
                audit(user_id, email, "SYNC_FAILED", repr(exc))

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
