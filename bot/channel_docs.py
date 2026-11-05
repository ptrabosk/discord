from pathlib import Path

import discord

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "channels"
INITIAL_SETUP_DOC = DOCS_DIR / "initial-setup.md"
WORKFLOWS_DOC = DOCS_DIR / "workflows-main.md"
DISCORD_LIMIT = 2000


def chunk_markdown(text: str, limit: int = DISCORD_LIMIT - 100) -> list[str]:
    """Split on blank lines so each Discord message stays under the limit."""
    chunks, current = [], ""
    for paragraph in text.strip().split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def doc_marker(path: Path) -> str:
    """The doc's first line; the bot finds its own posts by it."""
    return path.read_text(encoding="utf-8").strip().splitlines()[0]


async def post_doc_once(channel: discord.TextChannel, path: Path, bot_user_id: int) -> bool:
    """Post a markdown doc unless this bot already posted it here."""
    marker = doc_marker(path)
    async for message in channel.history(limit=100):
        if message.author.id == bot_user_id and message.content.startswith(marker):
            return False
    for chunk in chunk_markdown(path.read_text(encoding="utf-8")):
        await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    return True


async def clear_bot_posts(channel: discord.TextChannel, bot_user_id: int) -> int:
    """Delete every message this bot posted here, so setup can repost the docs and buttons in order.

    Only used on the initial-setup and workflows channels, where the bot posts nothing else.
    A long doc spans several messages, so matching on the marker alone would miss the later chunks.
    """
    deleted = 0
    async for message in channel.history(limit=100):
        if message.author.id == bot_user_id:
            await message.delete()
            deleted += 1
    return deleted
