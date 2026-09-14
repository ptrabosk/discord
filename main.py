import os, sys, asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

from database.database import init_db
from integrations.google_sheets import AccessSheet
from bot.setup_server import run_setup
from bot.onboarding import VerifyView
from bot.commands import register_commands
from bot.sync import AccessSync

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
SYNC_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "15"))

if not TOKEN or not GUILD_ID:
    raise RuntimeError("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID in .env")

intents = discord.Intents.default()
intents.members = True

class OpsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
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
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not hasattr(bot, "_access_sync"):
        bot._access_sync = AccessSync(bot, bot.sheet, GUILD_ID, SYNC_MINUTES)
        bot._access_sync.loop.start()

async def post_setup_message(guild):
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

async def setup_only():
    await bot.login(TOKEN)
    try:
        guild = await bot.fetch_guild(GUILD_ID)
        # fetch_guild is partial; use cache after connect instead via temporary ready event
    finally:
        await bot.close()

@bot.event
async def on_connect():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "setup":
        await bot.wait_until_ready()

async def run_setup_when_ready():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        raise RuntimeError("Guild not found. Confirm DISCORD_GUILD_ID and bot membership.")
    await run_setup(guild)
    await post_setup_message(guild)
    print("Server setup complete.")
    await bot.close()

if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1].lower() == "setup":
        async def runner():
            task = asyncio.create_task(run_setup_when_ready())
            try:
                await bot.start(TOKEN)
            finally:
                if not task.done():
                    task.cancel()
        asyncio.run(runner())
    else:
        bot.run(TOKEN)
