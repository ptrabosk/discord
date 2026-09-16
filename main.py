import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from app_paths import ENV_FILE


load_dotenv(dotenv_path=ENV_FILE)

from bot.guild_authorization import (
    get_authorized_guild_id,
    is_authorized_guild,
    load_authorized_guild_id,
    require_authorized_interaction,
)
from bot.health import check_google_sheet, get_bot_version, smtp_configuration_status
from bot.instance_lock import InstanceLock
from database.database import LOCK_PATH, DB_PATH, check_database, init_db
from integrations.google_sheets import AccessSheet
from bot.setup_server import run_setup
from bot.onboarding import VerifyView
from bot.commands import register_commands
from bot.sync import AccessSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in .env")
    return value


def _positive_integer(name, default):
    raw_value = os.getenv(name, default).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


TOKEN = _required("DISCORD_BOT_TOKEN")
GUILD_ID = load_authorized_guild_id()
SYNC_MINUTES = _positive_integer("SYNC_INTERVAL_MINUTES", "15")
_positive_integer("VERIFICATION_EXPIRY_MINUTES", "10")
_positive_integer("VERIFICATION_MAX_ATTEMPTS", "5")
_positive_integer("VERIFICATION_RESEND_SECONDS", "60")

intents = discord.Intents.default()
intents.members = True

class AuthorizedCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_authorized_interaction(interaction)

    async def on_error(self, interaction, error):
        logger.error(
            "Application command failed: command=%s guild_id=%s user_id=%s",
            getattr(interaction.command, "qualified_name", "unknown"),
            interaction.guild_id,
            getattr(interaction.user, "id", None),
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "The command failed unexpectedly. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class OpsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, tree_cls=AuthorizedCommandTree)
        self.sheet = AccessSheet()
        self.started_at = datetime.now(timezone.utc)
        self.version = get_bot_version()
        self.access_sync = AccessSync(self, self.sheet, GUILD_ID, SYNC_MINUTES)
        self._shutdown_started = False

    async def setup_hook(self):
        self.add_view(VerifyView(self.sheet))
        register_commands(self, self.sheet)
        guild_obj = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)
        self.access_sync.loop.start()
        logger.info("Persistent verification view and guild commands registered")

        google_ok, google_detail = await check_google_sheet(self.sheet, timeout_seconds=15)
        if google_ok:
            logger.info("Google Sheet startup check: accessible")
        else:
            logger.warning("Google Sheet startup check failed: %s", google_detail)

    async def close(self):
        if not self._shutdown_started:
            self._shutdown_started = True
            logger.info("Graceful shutdown started")
            sync_task = self.access_sync.loop.get_task()
            if self.access_sync.loop.is_running():
                self.access_sync.loop.cancel()
            if sync_task is not None:
                await asyncio.gather(sync_task, return_exceptions=True)
        await super().close()
        if self._shutdown_started:
            logger.info("Graceful shutdown complete")

bot = OpsBot()

@bot.event
async def on_ready():
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

    for guild in list(bot.guilds):
        if is_authorized_guild(guild):
            continue
        logger.warning(
            "Unauthorized guild connected; leaving: guild_id=%s name=%r",
            guild.id,
            guild.name,
        )
        try:
            await guild.leave()
            logger.info("Unauthorized guild left: guild_id=%s name=%r", guild.id, guild.name)
        except discord.HTTPException:
            logger.exception(
                "Failed to leave unauthorized guild: guild_id=%s name=%r",
                guild.id,
                guild.name,
            )

    authorized_guild = bot.get_guild(get_authorized_guild_id())
    if authorized_guild is None:
        logger.warning("Authorized guild unavailable during startup; guild operations will be skipped")
    else:
        logger.info(
            "Authorized guild connected: guild_id=%s name=%r",
            authorized_guild.id,
            authorized_guild.name,
        )


@bot.event
async def on_guild_join(guild: discord.Guild):
    if is_authorized_guild(guild):
        return
    logger.warning(
        "Unauthorized guild joined; installation blocked: guild_id=%s name=%r",
        guild.id,
        guild.name,
    )
    try:
        await guild.leave()
        logger.info("Unauthorized guild left: guild_id=%s name=%r", guild.id, guild.name)
    except discord.HTTPException:
        logger.exception(
            "Failed to leave unauthorized guild after join: guild_id=%s name=%r",
            guild.id,
            guild.name,
        )


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or not is_authorized_guild(message.guild):
        return
    await bot.process_commands(message)


@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception("Unexpected Discord event error: event=%s", event)


async def post_setup_message(guild):
    if not is_authorized_guild(guild):
        raise RuntimeError("Refusing to post setup message outside the authorized guild")
    channel = discord.utils.get(guild.text_channels, name="initial-setup")
    if not channel:
        return
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.components:
            return
    await channel.send(
        "Welcome.\n\nComplete account verification to receive access to your assigned team channels.",
        view=VerifyView(bot.sheet)
    )

async def run_setup_when_ready():
    await bot.wait_until_ready()
    try:
        guild = bot.get_guild(get_authorized_guild_id())
        if not guild:
            logger.error("Authorized guild unavailable during setup; no server changes were made")
            raise RuntimeError("Authorized guild unavailable. Confirm the bot is a member of the configured server.")
        await run_setup(guild)
        await post_setup_message(guild)
        logger.info("Server setup complete")
    finally:
        await bot.close()


async def run_bot(setup_mode=False):
    setup_task = asyncio.create_task(run_setup_when_ready()) if setup_mode else None
    bot_task = asyncio.create_task(bot.start(TOKEN))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals = []

    def request_shutdown(signal_name):
        logger.info("Shutdown signal received: %s", signal_name)
        stop_event.set()

    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                shutdown_signal,
                request_shutdown,
                shutdown_signal.name,
            )
            registered_signals.append(shutdown_signal)
        except (NotImplementedError, RuntimeError):
            pass

    stop_task = asyncio.create_task(stop_event.wait())
    try:
        wait_tasks = {bot_task, stop_task}
        if setup_task is not None:
            wait_tasks.add(setup_task)
        done, _ = await asyncio.wait(
            wait_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            if setup_task is not None and not setup_task.done():
                setup_task.cancel()
                await asyncio.gather(setup_task, return_exceptions=True)
            await bot.close()
            if not bot_task.done():
                bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        elif setup_task is not None and setup_task in done:
            await bot_task
            await setup_task
        else:
            if setup_task is not None and not setup_task.done():
                setup_task.cancel()
                await asyncio.gather(setup_task, return_exceptions=True)
            await bot_task
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        for shutdown_signal in registered_signals:
            loop.remove_signal_handler(shutdown_signal)
        if setup_task is not None and not setup_task.done():
            setup_task.cancel()
            await asyncio.gather(setup_task, return_exceptions=True)
        if not bot.is_closed():
            await bot.close()
        if not bot_task.done():
            bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


if __name__ == "__main__":
    instance_lock = InstanceLock(LOCK_PATH)
    instance_lock.acquire()
    try:
        logger.info("Starting Offsight Operations Bot version=%s", bot.version)
        init_db()
        database_ok, database_detail = check_database()
        if not database_ok:
            raise RuntimeError(f"Database startup check failed: {database_detail}")
        logger.info("Database startup check: accessible path=%s", DB_PATH)
        logger.info("Google credentials: loaded")
        smtp_ok, smtp_detail = smtp_configuration_status()
        if smtp_ok:
            logger.info("SMTP startup check: configured (no email sent)")
        else:
            logger.warning("SMTP startup check failed: %s", smtp_detail)

        setup_mode = len(sys.argv) > 1 and sys.argv[1].lower() == "setup"
        asyncio.run(run_bot(setup_mode=setup_mode))
    except discord.LoginFailure:
        logger.critical("Discord rejected DISCORD_BOT_TOKEN; check the server configuration")
        raise
    finally:
        instance_lock.release()
