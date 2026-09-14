import os, re, secrets, hashlib, hmac
from datetime import datetime, timedelta, timezone
import discord
from database.database import (
    save_code, get_code, increment_attempts, delete_code,
    get_verified_by_email, save_verified_user, audit
)
from integrations.email_verification import send_verification_email
from integrations.google_sheets import truthy
from bot.permissions import sync_member_roles

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
        email = str(self.email).strip().lower()
        if not EMAIL_RE.match(email) or not _allowed(email):
            await interaction.response.send_message(
                "We couldn't verify this email. Contact Operations if you believe you should have access.",
                ephemeral=True
            )
            return

        record = self.sheet.get_by_email(email)
        existing = get_verified_by_email(email)
        if not record or not truthy(record.get("Active")) or (existing and existing[0] != interaction.user.id):
            audit(interaction.user.id, email, "VERIFICATION_FAILED", "Email not authorized or already linked")
            await interaction.response.send_message(
                "We couldn't verify this email. Contact Operations if you believe you should have access.",
                ephemeral=True
            )
            return

        code = f"{secrets.randbelow(1_000_000):06d}"
        expiry = (datetime.now(timezone.utc) + timedelta(
            minutes=int(os.getenv("VERIFICATION_EXPIRY_MINUTES","10"))
        )).isoformat()
        save_code(interaction.user.id, email, _hash(code), expiry)
        send_verification_email(email, code)
        audit(interaction.user.id, email, "VERIFICATION_SENT")
        await interaction.response.send_message(
            f"Verification email sent to {_mask(email)}.",
            view=CodeView(self.sheet), ephemeral=True
        )

class CodeModal(discord.ui.Modal, title="Enter Verification Code"):
    code = discord.ui.TextInput(label="Verification code", min_length=6, max_length=6)

    def __init__(self, sheet):
        super().__init__()
        self.sheet = sheet

    async def on_submit(self, interaction):
        row = get_code(interaction.user.id)
        max_attempts = int(os.getenv("VERIFICATION_MAX_ATTEMPTS","5"))
        if not row:
            await interaction.response.send_message("No active verification request. Start again.", ephemeral=True)
            return
        email, code_hash, expires_at, attempts, _ = row
        if attempts >= max_attempts or datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
            delete_code(interaction.user.id)
            await interaction.response.send_message("Verification expired. Start again.", ephemeral=True)
            return
        if not hmac.compare_digest(_hash(str(self.code).strip()), code_hash):
            increment_attempts(interaction.user.id)
            audit(interaction.user.id, email, "VERIFICATION_FAILED", "Incorrect code")
            await interaction.response.send_message("Incorrect verification code.", ephemeral=True)
            return

        # Re-read source of truth immediately before authorization.
        record = self.sheet.get_by_email(email)
        if not record or not truthy(record.get("Active")):
            delete_code(interaction.user.id)
            await interaction.response.send_message(
                "We couldn't verify this email. Contact Operations if you believe you should have access.",
                ephemeral=True
            )
            return

        save_verified_user(interaction.user.id, email)
        delete_code(interaction.user.id)
        added, removed = await sync_member_roles(interaction.user, record)
        audit(interaction.user.id, email, "USER_VERIFIED", f"Added={added}; Removed={removed}")
        await interaction.response.send_message("Verified. Your authorized access has been applied.", ephemeral=True)

class CodeView(discord.ui.View):
    def __init__(self, sheet):
        super().__init__(timeout=600)
        self.sheet = sheet

    @discord.ui.button(label="Enter Verification Code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction, button):
        await interaction.response.send_modal(CodeModal(self.sheet))

class VerifyView(discord.ui.View):
    def __init__(self, sheet):
        super().__init__(timeout=None)
        self.sheet = sheet

    @discord.ui.button(label="Verify Work Email", style=discord.ButtonStyle.primary, custom_id="opsbot:verify")
    async def verify(self, interaction, button):
        await interaction.response.send_modal(EmailModal(self.sheet))
