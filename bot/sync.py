import asyncio
import logging
from discord.ext import tasks
from database.database import list_verified_users, touch_sync, audit
from bot.permissions import sync_member_roles

log = logging.getLogger(__name__)

class AccessSync:
    def __init__(self, bot, sheet, guild_id, minutes):
        self.bot = bot
        self.sheet = sheet
        self.guild_id = guild_id
        self.loop.change_interval(minutes=minutes)

    @tasks.loop(minutes=15)
    async def loop(self):
        try:
            await self.sync_all()
        except Exception:
            # An escaped exception would stop tasks.loop for good.
            log.exception("Access sync pass failed")

    async def sync_all(self):
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return
        # One roster read per pass, not one per member: Sheets allows about 60 reads a minute.
        # If it fails, skip the pass rather than treat everyone as missing and remove their roles.
        try:
            records = await asyncio.to_thread(self.sheet.all_records)
        except Exception as exc:
            log.exception("Access sync could not read the roster")
            audit(None, None, "SYNC_FAILED", f"Roster read: {exc!r}"[:300])
            return
        roster = {str(r.get("Email", "")).strip().lower(): r for r in records}
        for user_id, email in list_verified_users():
            try:
                member = guild.get_member(user_id)
                if not member:
                    continue
                added, removed = await sync_member_roles(member, roster.get(email.strip().lower()))
                touch_sync(user_id)
                if added or removed:
                    audit(user_id, email, "ROLE_SYNC", f"Added={added}; Removed={removed}")
            except Exception as exc:
                audit(user_id, email, "SYNC_FAILED", repr(exc))

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
