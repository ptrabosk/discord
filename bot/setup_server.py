import json
from pathlib import Path
import discord

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT/"config"/"server_structure.json").read_text())

async def ensure_role(guild, name):
    role = discord.utils.get(guild.roles, name=name)
    if not role:
        role = await guild.create_role(name=name, reason="Ops Bot server setup")
    return role

async def run_setup(guild):
    role_map = {}
    for role_name in CONFIG["roles"]:
        role_map[role_name] = await ensure_role(guild, role_name)

    everyone = guild.default_role

    for item in CONFIG["categories"]:
        category = discord.utils.get(guild.categories, name=item["name"])
        public = item.get("public", False)
        category_role = role_map.get(item.get("role"))

        overwrites = {
            everyone: discord.PermissionOverwrite(
                view_channel=True if public else False,
                send_messages=True if public else None,
                read_message_history=True if public else None
            )
        }
        if category_role:
            overwrites[category_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        if not category:
            category = await guild.create_category(
                item["name"], overwrites=overwrites, reason="Ops Bot server setup"
            )
        else:
            await category.edit(overwrites=overwrites, reason="Ops Bot server setup")

        for ch_cfg in item["channels"]:
            name = ch_cfg["name"]
            channel = discord.utils.get(category.text_channels, name=name)

            # Channel permissions start from category permissions.
            channel_overwrites = dict(overwrites)

            # Public channel override, e.g. WORKFLOWS/main.
            if ch_cfg.get("public"):
                channel_overwrites[everyone] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

            # Subteam channel override: hide from parent category role and show only subteam.
            sub_role = role_map.get(ch_cfg.get("role"))
            if sub_role:
                if category_role:
                    channel_overwrites[category_role] = discord.PermissionOverwrite(view_channel=False)
                channel_overwrites[sub_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

            if not channel:
                await guild.create_text_channel(
                    name, category=category, overwrites=channel_overwrites,
                    reason="Ops Bot server setup"
                )
            else:
                await channel.edit(overwrites=channel_overwrites, reason="Ops Bot server setup")
