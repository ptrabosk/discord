import logging
import re

import discord
from discord import app_commands

log = logging.getLogger(__name__)

CHANNEL_NAME = "wg-apps"
MAX_HOURS_PER_WEEK = 168


def parse_hours(value: str) -> int | None:
    """Whole number of hours per week, 1 to 168. Anything else (decimals, words, signs) is rejected."""
    text = value.strip()
    if not re.fullmatch(r"\d{1,3}", text):
        return None
    hours = int(text)
    return hours if 1 <= hours <= MAX_HOURS_PER_WEEK else None


class ApplicationModal(discord.ui.Modal, title="Application"):
    name_input = discord.ui.TextInput(label="Name", max_length=100)
    hours_input = discord.ui.TextInput(
        label="Available hours",
        placeholder="How many hours are you able to work per week?",
        max_length=3,
    )
    experience_input = discord.ui.TextInput(
        label="Previous experience",
        placeholder="Not required, for informational purposes only.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        hours = parse_hours(self.hours_input.value)
        if hours is None:
            await interaction.response.send_message(
                "Available hours must be a whole number of hours per week, from 1 to 168. "
                "Please submit the application again.",
                ephemeral=True,
            )
            return

        channel = discord.utils.get(interaction.guild.text_channels, name=CHANNEL_NAME)
        if channel is None:
            log.error("Application channel #%s not found in guild %s", CHANNEL_NAME, interaction.guild.id)
            await interaction.response.send_message(
                "The applications channel is not set up yet. Please tell a Workflows member.",
                ephemeral=True,
            )
            return

        experience = self.experience_input.value.strip() or "None given"
        body = (
            f"**New application**\n"
            f"**Agent:** {interaction.user.display_name} (@{interaction.user.name})\n"
            f"**Name:** {self.name_input.value.strip()}\n"
            f"**Available hours per week:** {hours}\n"
            f"**Previous experience:** {experience}"
        )
        await channel.send(body, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message("Your application was submitted.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Application form failed", exc_info=error)
        await interaction.response.send_message(
            "The application could not be submitted. Please try again.", ephemeral=True
        )


def register_applications(bot):
    @bot.tree.command(name="apply", description="Submit an application to the Workflows team")
    @app_commands.guild_only()
    async def apply(interaction: discord.Interaction):
        if interaction.guild_id != bot.guild_id:
            await interaction.response.send_message("This command only works in the authorized server.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal())
