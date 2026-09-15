import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from bot.guild_authorization import (
    get_authorized_guild_id,
    is_authorized_guild,
    load_authorized_guild_id,
    require_authorized_interaction,
)
from database.database import init_db
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

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = load_authorized_guild_id()
SYNC_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "15"))

if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is required in .env")

intents = discord.Intents.default()
intents.members = True

class AuthorizedCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_authorized_interaction(interaction)


class OpsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, tree_cls=AuthorizedCommandTree)
        self.sheet = AccessSheet()
        self.synced_commands = False

    async def setup_hook(self):
        self.add_view(VerifyView(self.sheet))
        register_commands(self, self.sheet)
        guild_obj = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)

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

    if bot.get_guild(get_authorized_guild_id()) is None:
        logger.warning("Authorized guild unavailable during startup; guild operations will be skipped")

    if not hasattr(bot, "_access_sync"):
        bot._access_sync = AccessSync(bot, bot.sheet, GUILD_ID, SYNC_MINUTES)
        bot._access_sync.loop.start()


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

if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1].lower() == "setup":
        async def runner():
            task = asyncio.create_task(run_setup_when_ready())
            try:
                await bot.start(TOKEN)
                await task
            finally:
                if not task.done():
                    task.cancel()
        asyncio.run(runner())
    else:
        bot.run(TOKEN)
