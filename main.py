import os, sys, asyncio
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv

from database.database import audit, delete_verified_user, init_db, list_verified_users
from integrations.google_sheets import AccessSheet
from bot.setup_server import run_setup
from bot.onboarding import VerifyView
from bot.commands import register_commands
from bot.applications import register_applications
from bot.sync import AccessSync, forget_removed_member
from forms.service import FormsService, FormsSync
from forms.views import register_forms
from bot.channel_docs import INITIAL_SETUP_DOC, WORKFLOWS_DOC, clear_bot_posts, post_doc_once
from reachouts.service import ReachoutLoop, ReachoutService, reachouts_enabled
from reachouts.views import register_reachouts

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
SYNC_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "15"))
FORMS_SYNC_MINUTES = int(os.getenv("FORM_SHEETS_RETRY_MINUTES", "5"))

if not TOKEN or not GUILD_ID:
    raise RuntimeError("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID in .env")

intents = discord.Intents.default()
intents.members = True

class OpsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.guild_id = GUILD_ID
        self.sheet = AccessSheet()
        self.forms = FormsService(self, self.sheet)
        self.reachouts = ReachoutService(self, self.sheet) if reachouts_enabled() else None

    async def setup_hook(self):
        self.add_view(VerifyView(self.sheet))
        register_commands(self, self.sheet)
        register_applications(self)
        register_forms(self, self.forms)
        if self.reachouts:
            register_reachouts(self, self.reachouts)
        guild_obj = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)

bot = OpsBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    # `main.py setup` only builds the server; it must not sync roles, write Sheets, or send reachouts.
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("setup", "refresh-docs"):
        return
    if not hasattr(bot, "_access_sync"):
        bot._access_sync = AccessSync(bot, bot.sheet, GUILD_ID, SYNC_MINUTES)
        bot._access_sync.loop.start()
    if not hasattr(bot, "_forms_sync"):
        bot._forms_sync = FormsSync(bot, bot.forms, FORMS_SYNC_MINUTES)
        bot._forms_sync.loop.start()
    if bot.reachouts and not hasattr(bot, "_reachout_loop"):
        bot._reachout_loop = ReachoutLoop(bot, bot.reachouts)
        bot._reachout_loop.loop.start()

@bot.event
async def on_member_remove(member):
    # Leaving, being kicked, and being banned all end membership; the person must verify again to rejoin.
    if member.guild.id == GUILD_ID:
        await forget_removed_member(member.id, bot.sheet)

async def _has_component_message(channel):
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.components:
            return True
    return False

async def post_setup_message(guild):
    setup = discord.utils.get(guild.text_channels, name="initial-setup")
    if setup:
        # The verify button goes above the doc, which tells new members to "Click Verify Personal Email above".
        if not await _has_component_message(setup):
            await setup.send(
                "Welcome.\n\nComplete account verification to receive access to your assigned team channels.",
                view=VerifyView(bot.sheet)
            )
        await post_doc_once(setup, INITIAL_SETUP_DOC, bot.user.id)
    # The workflows main channel is WORKFLOWS/main.
    workflows = workflows_channel(guild)
    if workflows:
        await post_doc_once(workflows, WORKFLOWS_DOC, bot.user.id)

def workflows_channel(guild):
    return next(
        (c for c in guild.text_channels if c.name == "main" and c.category and c.category.name == "WORKFLOWS"),
        None,
    )

async def refresh_channel_docs(guild):
    """Clear the bot's posts in the two doc channels, then repost them from the current doc files."""
    setup = discord.utils.get(guild.text_channels, name="initial-setup")
    if setup:
        await clear_bot_posts(setup, bot.user.id)
    workflows = workflows_channel(guild)
    if workflows:
        await clear_bot_posts(workflows, bot.user.id)
    await post_setup_message(guild)

async def refresh_docs_when_ready():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        raise RuntimeError("Guild not found. Confirm DISCORD_GUILD_ID and bot membership.")
    await refresh_channel_docs(guild)
    print("Channel docs refreshed.")
    await bot.close()

async def run_setup_when_ready():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        raise RuntimeError("Guild not found. Confirm DISCORD_GUILD_ID and bot membership.")
    await run_setup(guild)
    await post_setup_message(guild)
    print("Server setup complete.")
    await bot.close()

def backfill_verified() -> int:
    """Set Verified to TRUE on the Access sheet for everyone already in verified_users. Sheets only, no Discord login."""
    emails = [email for _, email in list_verified_users()]
    matched = AccessSheet().mark_verified_many(emails)
    print(f"Verified users: {len(emails)}. Access rows set to TRUE: {matched}. "
          f"Users with no matching Access row: {len(emails) - matched}.")
    return 0

def reset_access(email: str) -> int:
    """Forget one person's verification: delete their verified record and set their Access Verified cell to FALSE.
    Discord roles are not touched; remove them by hand."""
    matches = [user_id for user_id, saved in list_verified_users() if saved.strip().lower() == email.strip().lower()]
    for user_id in matches:
        delete_verified_user(user_id)
        audit(user_id, email, "ACCESS_RESET", "Reset by operator command")
    matched = AccessSheet().mark_verified_many([email], value="FALSE")
    print(f"Verified records removed: {len(matches)}. Access rows set to FALSE: {matched}. "
          "Remove Discord roles by hand.")
    return 0

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if len(sys.argv) > 2 and sys.argv[1].lower() == "reset-access":
        init_db()
        sys.exit(reset_access(sys.argv[2]))
    if len(sys.argv) > 1 and sys.argv[1].lower() == "reachout-sheet-setup":
        # Sheets only: this never logs in to Discord, so it cannot message anyone.
        from reachouts.sheets import setup_cli
        sys.exit(setup_cli())
    if len(sys.argv) > 1 and sys.argv[1].lower() == "backfill-verified":
        init_db()
        sys.exit(backfill_verified())
    init_db()
    bot.forms.repository.init()
    if bot.reachouts:
        bot.reachouts.init()
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("setup", "refresh-docs"):
        ready_task = run_setup_when_ready if sys.argv[1].lower() == "setup" else refresh_docs_when_ready
        async def runner():
            task = asyncio.create_task(ready_task())
            try:
                await bot.start(TOKEN)
            finally:
                if not task.done():
                    task.cancel()
        asyncio.run(runner())
    else:
        # basicConfig above already logs; discord.py's own handler would print every line twice.
        bot.run(TOKEN, log_handler=None)
