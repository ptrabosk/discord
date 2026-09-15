import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import discord
from database.database import (
    save_code, get_code, increment_attempts, delete_code,
    get_verified_by_email, save_verified_user, audit
)
from integrations.email_verification import send_verification_email
from integrations.google_sheets import truthy
from bot.guild_authorization import require_authorized_interaction
from bot.permissions import sync_member_roles

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
logger = logging.getLogger(__name__)

VERIFICATION_FAILED_MESSAGE = (
    "We couldn't verify this email. Contact Operations if you believe you should have access."
)
VERIFICATION_SERVICE_ERROR = "Verification is temporarily unavailable. Please try again later."


async def _finish_interaction(interaction, content, *, view=None):
    await interaction.edit_original_response(content=content, view=view)

def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()

def _allowed(email):
    domains = {x.strip().lower() for x in os.getenv("ALLOWED_EMAIL_DOMAINS","").split(",") if x.strip()}
    return email.rsplit("@",1)[-1].lower() in domains

def _mask(email):
    local, domain = email.split("@",1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain

class EmailModal(discord.ui.Modal, title="Verify Work Email"):
    email = discord.ui.TextInput(label="Work email", placeholder="name@company.com")

    def __init__(self, sheet):
        super().__init__()
        self.sheet = sheet

    async def on_submit(self, interaction):
        if not await require_authorized_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            email = str(self.email).strip().lower()
            if not EMAIL_RE.match(email) or not _allowed(email):
                await _finish_interaction(interaction, VERIFICATION_FAILED_MESSAGE)
                return

            record = await asyncio.to_thread(self.sheet.get_by_email, email)
            existing = await asyncio.to_thread(get_verified_by_email, email)
            if not record or not truthy(record.get("Active")) or (
                existing and existing[0] != interaction.user.id
            ):
                await asyncio.to_thread(
                    audit,
                    interaction.user.id,
                    email,
                    "VERIFICATION_FAILED",
                    "Email not authorized or already linked",
                )
                await _finish_interaction(interaction, VERIFICATION_FAILED_MESSAGE)
                return

            code = f"{secrets.randbelow(1_000_000):06d}"
            expiry = (datetime.now(timezone.utc) + timedelta(
                minutes=int(os.getenv("VERIFICATION_EXPIRY_MINUTES", "10"))
            )).isoformat()
            await asyncio.to_thread(save_code, interaction.user.id, email, _hash(code), expiry)
            try:
                await asyncio.to_thread(send_verification_email, email, code)
            except Exception:
                await asyncio.to_thread(delete_code, interaction.user.id)
                raise
            await asyncio.to_thread(audit, interaction.user.id, email, "VERIFICATION_SENT")
            await _finish_interaction(
                interaction,
                f"Verification email sent to {_mask(email)}.",
                view=CodeView(self.sheet),
            )
        except Exception:
            logger.exception("Email verification request failed for user_id=%s", interaction.user.id)
            await _finish_interaction(interaction, VERIFICATION_SERVICE_ERROR)

class CodeModal(discord.ui.Modal, title="Enter Verification Code"):
    code = discord.ui.TextInput(label="Verification code", min_length=6, max_length=6)

    def __init__(self, sheet):
        super().__init__()
        self.sheet = sheet

    async def on_submit(self, interaction):
        if not await require_authorized_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            row = await asyncio.to_thread(get_code, interaction.user.id)
            max_attempts = int(os.getenv("VERIFICATION_MAX_ATTEMPTS", "5"))
            if not row:
                await _finish_interaction(interaction, "No active verification request. Start again.")
                return
            email, code_hash, expires_at, attempts, _ = row
            if attempts >= max_attempts or datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                await asyncio.to_thread(delete_code, interaction.user.id)
                await _finish_interaction(interaction, "Verification expired. Start again.")
                return
            if not hmac.compare_digest(_hash(str(self.code).strip()), code_hash):
                await asyncio.to_thread(increment_attempts, interaction.user.id)
                await asyncio.to_thread(
                    audit,
                    interaction.user.id,
                    email,
                    "VERIFICATION_FAILED",
                    "Incorrect code",
                )
                await _finish_interaction(interaction, "Incorrect verification code.")
                return

            # Re-read source of truth immediately before authorization.
            record = await asyncio.to_thread(self.sheet.get_by_email, email)
            if not record or not truthy(record.get("Active")):
                await asyncio.to_thread(delete_code, interaction.user.id)
                await _finish_interaction(interaction, VERIFICATION_FAILED_MESSAGE)
                return

            await asyncio.to_thread(save_verified_user, interaction.user.id, email)
            await asyncio.to_thread(delete_code, interaction.user.id)
            added, removed = await sync_member_roles(interaction.user, record)
            await asyncio.to_thread(
                audit,
                interaction.user.id,
                email,
                "USER_VERIFIED",
                f"Added={added}; Removed={removed}",
            )
            await _finish_interaction(interaction, "Verified. Your authorized access has been applied.")
        except Exception:
            logger.exception("Verification code submission failed for user_id=%s", interaction.user.id)
            await _finish_interaction(interaction, VERIFICATION_SERVICE_ERROR)

class CodeView(discord.ui.View):
    def __init__(self, sheet):
        super().__init__(timeout=600)
        self.sheet = sheet

    @discord.ui.button(label="Enter Verification Code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction, button):
        if not await require_authorized_interaction(interaction):
            return
        await interaction.response.send_modal(CodeModal(self.sheet))

class VerifyView(discord.ui.View):
    def __init__(self, sheet):
        super().__init__(timeout=None)
        self.sheet = sheet

    @discord.ui.button(label="Verify Work Email", style=discord.ButtonStyle.primary, custom_id="opsbot:verify")
    async def verify(self, interaction, button):
        if not await require_authorized_interaction(interaction):
            return
        await interaction.response.send_modal(EmailModal(self.sheet))
