import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from discord.ext import tasks
from database.database import list_verified_users, touch_sync, audit
from bot.guild_authorization import get_authorized_guild_id, is_authorized_guild
from bot.permissions import sync_member_roles


logger = logging.getLogger(__name__)

class AccessSync:
    def __init__(self, bot, sheet, guild_id, minutes):
        self.bot = bot
        self.sheet = sheet
        self.guild_id = guild_id
        self.last_successful_sync: Optional[datetime] = None
        self.last_failure: Optional[str] = None
        self.loop.change_interval(minutes=minutes)

    @tasks.loop(minutes=15)
    async def loop(self):
        logger.info("Scheduled access sync started")
        authorized_guild_id = get_authorized_guild_id()
        if self.guild_id != authorized_guild_id:
            logger.error("Scheduled sync configured with a non-authorized guild; skipping run")
            self._record_failure("GuildConfigurationError")
            return
        guild = self.bot.get_guild(authorized_guild_id)
        if not guild:
            logger.warning("Authorized guild unavailable during scheduled sync; skipping run")
            self._record_failure("AuthorizedGuildUnavailable")
            return
        if not is_authorized_guild(guild):
            logger.error("Scheduled sync resolved a non-authorized guild; skipping run")
            self._record_failure("GuildAuthorizationError")
            return

        try:
            verified_users = await asyncio.to_thread(list_verified_users)
        except Exception as exc:
            self._record_failure(type(exc).__name__)
            logger.exception("Scheduled sync could not read verified users")
            return

        failures = 0
        updated = 0
        for user_id, email in verified_users:
            try:
                member = guild.get_member(user_id)
                if not member:
                    continue
                record = await asyncio.to_thread(self.sheet.get_by_email, email)
                added, removed = await sync_member_roles(member, record)
                await asyncio.to_thread(touch_sync, user_id)
                updated += 1
                if added or removed:
                    await asyncio.to_thread(
                        audit,
                        user_id,
                        email,
                        "ROLE_SYNC",
                        f"Added={added}; Removed={removed}",
                    )
            except Exception as exc:
                failures += 1
                self._record_failure(type(exc).__name__)
                logger.exception("Scheduled sync failed for user_id=%s", user_id)
                try:
                    await asyncio.to_thread(
                        audit, user_id, email, "SYNC_FAILED", type(exc).__name__
                    )
                except Exception:
                    logger.exception("Could not record scheduled sync failure for user_id=%s", user_id)

        if failures:
            logger.warning(
                "Scheduled access sync completed with failures: checked=%s updated=%s failures=%s",
                len(verified_users),
                updated,
                failures,
            )
            return

        self.last_successful_sync = datetime.now(timezone.utc)
        self.last_failure = None
        logger.info(
            "Scheduled access sync completed: checked=%s updated=%s",
            len(verified_users),
            updated,
        )

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    @loop.error
    async def loop_error(self, error):
        self._record_failure(type(error).__name__)
        logger.error(
            "Background access sync task stopped unexpectedly",
            exc_info=(type(error), error, error.__traceback__),
        )

    def _record_failure(self, error_name):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.last_failure = f"{timestamp} ({error_name})"
