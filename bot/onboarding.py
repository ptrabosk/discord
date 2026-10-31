import asyncio
import logging
import os, re, secrets, hashlib, hmac
from datetime import datetime, timedelta, timezone
import discord
from database.database import (
    save_code, get_code, increment_attempts, delete_code,
    get_verified_by_email, save_verified_user, audit, count_audit_since
)
from integrations.email_verification import send_verification_email
from integrations.google_sheets import truthy
from bot.permissions import sync_member_roles

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Per Discord user and per email, over 24 hours. A new code resets the per-code attempt
# count, so these caps are what bound the total number of guesses against one address.
DAILY_CODES = 5
DAILY_FAILED_GUESSES = 10
THROTTLED = "Too many verification attempts. Try again later or contact Operations."
TRY_AGAIN = "Verification could not continue because of a technical problem. Please try again in a few minutes."

async def _reply(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

def _day_ago():
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

def guesses_exhausted(user_id, email):
    since = _day_ago()
    return max(
        count_audit_since("VERIFICATION_FAILED", since, user_id=user_id, details="Incorrect code"),
        count_audit_since("VERIFICATION_FAILED", since, email=email, details="Incorrect code"),
    ) >= DAILY_FAILED_GUESSES

def code_request_blocked(user_id, email):
    """Why a new code must not be sent now, or None."""
    pending = get_code(user_id)
    resend_seconds = int(os.getenv("VERIFICATION_RESEND_SECONDS", "60"))
    if pending and datetime.now(timezone.utc) - datetime.fromisoformat(pending[4]) < timedelta(seconds=resend_seconds):
        return "resend_cooldown"
    since = _day_ago()
    if max(
        count_audit_since("VERIFICATION_SENT", since, user_id=user_id),
        count_audit_since("VERIFICATION_SENT", since, email=email),
    ) >= DAILY_CODES:
        return "daily_code_limit"
    if guesses_exhausted(user_id, email):
        return "daily_guess_limit"
    return None

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
        # Discord expires an unacknowledged interaction after 3 seconds; the sheet lookup and SMTP send can take longer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        email = str(self.email).strip().lower()
        if not EMAIL_RE.match(email) or not _allowed(email):
            await interaction.followup.send(
                "We couldn't verify this email. Contact Operations if you believe you should have access.",
                ephemeral=True
            )
            return

        # Checked before the sheet lookup so the answer does not reveal whether the email is authorized.
        blocked = code_request_blocked(interaction.user.id, email)
        if blocked:
            audit(interaction.user.id, email, "VERIFICATION_THROTTLED", blocked)
            await interaction.followup.send(THROTTLED, ephemeral=True)
            return

        record = await asyncio.to_thread(self.sheet.get_by_email, email)
        existing = get_verified_by_email(email)
        if not record or not truthy(record.get("Active")) or (existing and existing[0] != interaction.user.id):
            audit(interaction.user.id, email, "VERIFICATION_FAILED", "Email not authorized or already linked")
            await interaction.followup.send(
                "We couldn't verify this email. Contact Operations if you believe you should have access.",
                ephemeral=True
            )
            return

        code = f"{secrets.randbelow(1_000_000):06d}"
        expiry = (datetime.now(timezone.utc) + timedelta(
            minutes=int(os.getenv("VERIFICATION_EXPIRY_MINUTES","10"))
        )).isoformat()
        save_code(interaction.user.id, email, _hash(code), expiry)
        try:
            await asyncio.to_thread(send_verification_email, email, code)
        except Exception as exc:
            # Without the email the saved code is useless, and it would hold the resend cooldown.
            delete_code(interaction.user.id)
            log.exception("Could not send the verification email")
            audit(interaction.user.id, email, "VERIFICATION_EMAIL_FAILED", type(exc).__name__)
            await interaction.followup.send(
                "We couldn't send the email right now. Please try again in a few minutes.", ephemeral=True
            )
            return
        audit(interaction.user.id, email, "VERIFICATION_SENT")
        await interaction.followup.send(
            f"Verification email sent to {_mask(email)}.",
            view=CodeView(self.sheet), ephemeral=True
        )

    async def on_error(self, interaction, error):
        log.exception("Verification email step failed", exc_info=error)
        await _reply(interaction, TRY_AGAIN)

class CodeModal(discord.ui.Modal, title="Enter Verification Code"):
    code = discord.ui.TextInput(label="Verification code", min_length=6, max_length=6)

    def __init__(self, sheet):
        super().__init__()
        self.sheet = sheet

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = get_code(interaction.user.id)
        max_attempts = int(os.getenv("VERIFICATION_MAX_ATTEMPTS","5"))
        if not row:
            await interaction.followup.send("No active verification request. Start again.", ephemeral=True)
            return
        email, code_hash, expires_at, attempts, _ = row
        if guesses_exhausted(interaction.user.id, email):
            delete_code(interaction.user.id)
            audit(interaction.user.id, email, "VERIFICATION_THROTTLED", "daily_guess_limit")
            await interaction.followup.send(THROTTLED, ephemeral=True)
            return
        if attempts >= max_attempts or datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
            delete_code(interaction.user.id)
            await interaction.followup.send("Verification expired. Start again.", ephemeral=True)
            return
        if not hmac.compare_digest(_hash(str(self.code).strip()), code_hash):
            increment_attempts(interaction.user.id)
            audit(interaction.user.id, email, "VERIFICATION_FAILED", "Incorrect code")
            await interaction.followup.send("Incorrect verification code.", ephemeral=True)
            return

        # Re-read source of truth immediately before authorization.
        record = await asyncio.to_thread(self.sheet.get_by_email, email)
        if not record or not truthy(record.get("Active")):
            delete_code(interaction.user.id)
            await interaction.followup.send(
                "We couldn't verify this email. Contact Operations if you believe you should have access.",
                ephemeral=True
            )
            return

        save_verified_user(interaction.user.id, email)
        delete_code(interaction.user.id)
        try:
            added, removed = await sync_member_roles(interaction.user, record)
        except Exception as exc:
            # The user stays verified; the periodic access sync retries the roles.
            log.exception("Could not apply roles after verification")
            audit(interaction.user.id, email, "USER_VERIFIED", "Roles not applied")
            audit(interaction.user.id, email, "ROLE_SYNC_FAILED", repr(exc)[:300])
            await interaction.followup.send(
                "Verified, but your access could not be applied yet. It will be retried automatically; "
                "contact Operations if it does not appear within 30 minutes.", ephemeral=True
            )
            return
        audit(interaction.user.id, email, "USER_VERIFIED", f"Added={added}; Removed={removed}")
        await interaction.followup.send("Verified. Your authorized access has been applied.", ephemeral=True)

    async def on_error(self, interaction, error):
        log.exception("Verification code step failed", exc_info=error)
        await _reply(interaction, TRY_AGAIN)

class CodeView(discord.ui.View):
    def __init__(self, sheet):
        super().__init__(timeout=600)
        self.sheet = sheet

    @discord.ui.button(label="Enter Verification Code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction, button):
        await interaction.response.send_modal(CodeModal(self.sheet))

    @discord.ui.button(label="Use a different email", style=discord.ButtonStyle.secondary)
    async def change_email(self, interaction, button):
        # The resend cooldown and daily caps still apply to the new address.
        await interaction.response.send_modal(EmailModal(self.sheet))

class VerifyView(discord.ui.View):
    def __init__(self, sheet):
        super().__init__(timeout=None)
        self.sheet = sheet

    @discord.ui.button(label="Verify Work Email", style=discord.ButtonStyle.primary, custom_id="opsbot:verify")
    async def verify(self, interaction, button):
        await interaction.response.send_modal(EmailModal(self.sheet))
