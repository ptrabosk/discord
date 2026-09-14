import json
from pathlib import Path
import discord
from integrations.google_sheets import truthy

ROOT = Path(__file__).resolve().parents[1]
ROLE_CONFIG = json.loads((ROOT/"config"/"role_mapping.json").read_text())

MANAGED_ROLES = set(ROLE_CONFIG["managed_roles"])

def desired_roles(record):
    if not record or not truthy(record.get("Active")):
        return set()

    desired = {"Verified"}

    for column, role in ROLE_CONFIG["sheet_columns"].items():
        if truthy(record.get(column)):
            desired.add(role)

    if truthy(record.get("Concierge")):
        desired.add("Concierge")
        raw = str(record.get("Concierge Team", "")).strip()
        teams = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
        for team in teams:
            role = ROLE_CONFIG["concierge_teams"].get(team)
            if role:
                desired.add(role)

    return desired

async def sync_member_roles(member: discord.Member, record):
    desired = desired_roles(record)
    current = {r.name for r in member.roles if r.name in MANAGED_ROLES}

    add_names = desired - current
    remove_names = current - desired

    add_roles = [discord.utils.get(member.guild.roles, name=n) for n in add_names]
    remove_roles = [discord.utils.get(member.guild.roles, name=n) for n in remove_names]
    add_roles = [r for r in add_roles if r]
    remove_roles = [r for r in remove_roles if r]

    if remove_roles:
        await member.remove_roles(*remove_roles, reason="Ops Bot access synchronization")
    if add_roles:
        await member.add_roles(*add_roles, reason="Ops Bot access synchronization")

    return sorted(add_names), sorted(remove_names)
