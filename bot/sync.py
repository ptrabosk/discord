import asyncio
import logging
from discord.ext import tasks
from database.database import list_verified_users, touch_sync, audit, get_verified_user, delete_verified_user
from bot.permissions import sync_member_roles

log = logging.getLogger(__name__)

async def forget_removed_member(user_id, sheet):
    """Drop a removed member's verification so their email is free for the right account.

    Returns True if there was a verification to drop. Their roles are already gone with their membership.
    """
    row = get_verified_user(user_id)
    if not row:
        return False
    email = row[1]
    delete_verified_user(user_id)
    audit(user_id, email, "ACCESS_RESET", "Member left or was removed from the server")
    try:
        await asyncio.to_thread(sheet.mark_verified_many, [email], "FALSE")
    except Exception:
        # The local link is already gone, which is what blocks or allows verification; the sheet cell is a convenience.
        log.exception("Could not clear the Verified cell for a removed member")
        audit(user_id, email, "ACCESS_RESET_SHEET_FAILED", "Verified cell not cleared")
    return True

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
        roster.pop("", None)
        # A read can succeed and still be wrong: an emptied sheet or a renamed Email header matches no one,
        # and acting on it would remove every member's roles in one pass.
        if not roster:
            log.error("Access sync skipped: the roster has no rows with an Email value")
            audit(None, None, "SYNC_FAILED", "Roster has no rows with an Email value")
            return
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
