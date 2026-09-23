from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from bot.permissions import is_admin
from database.database import audit, get_verified_user
from database.forms_repository import FormsRepository
from forms.domain import (
    ADJUSTMENT_TYPES,
    EASTERN,
    FINAL_STATUSES,
    FormState,
    form_datetimes,
    sheet_tab_for,
    status_for_action,
    validate_category,
)
from integrations.forms_sheets import FormsSheet
from integrations.google_sheets import truthy

log = logging.getLogger(__name__)

REVIEW_CHANNEL_ENV = {
    "when_i_work": "WIW_REVIEW_CHANNEL_ID",
    "question": "QUESTION_REVIEW_CHANNEL_ID",
    "concern": "CONCERN_REVIEW_CHANNEL_ID",
    "feedback": "FEEDBACK_REVIEW_CHANNEL_ID",
}
DEFAULT_FORM_ERROR_CHANNEL_ID = 1550994656966344714


class FormsService:
    def __init__(self, bot, access_sheet):
        self.bot = bot
        self.access_sheet = access_sheet
        self.forms_sheet = FormsSheet(access_sheet.client)
        self.repository = FormsRepository()
        self._review_lock = asyncio.Lock()
        # ponytail: form volume is low; one lock keeps retry side effects idempotent.
        self._submission_lock = asyncio.Lock()

    async def alert_error(
        self,
        kind: str,
        *,
        row: dict | None = None,
        interaction_id: int | None = None,
        error: Exception | None = None,
    ):
        """Send operational failures to a private channel, never to employees."""
        reference = row["submission_id"] if row else f"interaction {interaction_id}"
        try:
            channel_id = int(os.getenv("FORM_ERROR_CHANNEL_ID") or DEFAULT_FORM_ERROR_CHANNEL_ID)
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.bot.guild_id:
                raise RuntimeError("error destination is not a text channel in the authorized server")
            if channel.permissions_for(channel.guild.default_role).view_channel:
                raise RuntimeError("error channel is visible to @everyone")
            detail = f" ({type(error).__name__})" if error else ""
            await channel.send(
                f"Form error: {kind}{detail}. Reference: {reference}. Check service logs.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Could not send form error alert for %s", reference)

    def verified_email(self, user_id: int) -> str | None:
        row = get_verified_user(user_id)
        return row[1] if row else None

    async def employee_identity(self, user_id: int) -> tuple[str, str]:
        email = self.verified_email(user_id)
        if not email:
            raise PermissionError("Complete work-email verification before submitting a form.")
        record = await asyncio.to_thread(self.access_sheet.get_by_email, email)
        if not record or not truthy(record.get("Active")):
            raise PermissionError("Your verified employee record is not currently active.")
        name = str(record.get("Name", "")).strip()
        if not name:
            raise ValueError("Your employee record is missing a name. Contact Operations.")
        return email, name

    async def submit(self, interaction: discord.Interaction, state: FormState, message: str) -> dict:
        if interaction.guild_id != self.bot.guild_id:
            raise PermissionError("Forms can only be submitted in the authorized server.")
        email, employee_name = await self.employee_identity(interaction.user.id)
        sheet_tab_for(state.form_type, state.form_subtype)
        if not message.strip():
            raise ValueError("A message or reason is required.")
        if state.form_type in {"question", "concern", "feedback"}:
            validate_category(state.category)
        if state.form_subtype == "timesheet_adjustment":
            if state.adjustment_type not in ADJUSTMENT_TYPES:
                raise ValueError("A valid adjustment type is required.")
        elif state.adjustment_type is not None:
            raise ValueError("Adjustment type is only valid for timesheet adjustments.")

        start_et = start_utc = end_et = end_utc = None
        if state.form_type == "when_i_work":
            start_et, start_utc, end_et, end_utc = form_datetimes(state)

        submitted_utc = datetime.now(timezone.utc)
        submitted_et = submitted_utc.astimezone(EASTERN)
        values = {
            "interaction_id": str(interaction.id),
            "form_type": state.form_type,
            "form_subtype": state.form_subtype,
            "discord_user_id": interaction.user.id,
            "discord_username": str(interaction.user),
            "discord_display_name": interaction.user.display_name,
            "employee_email": email,
            "employee_name": employee_name,
            "submitted_at_utc": submitted_utc.isoformat(),
            "submitted_at_et": submitted_et.isoformat(),
            "category": state.category,
            "adjustment_type": state.adjustment_type,
            "start_time_et": start_et.isoformat() if start_et else None,
            "start_time_utc": start_utc.isoformat() if start_utc else None,
            "end_time_et": end_et.isoformat() if end_et else None,
            "end_time_utc": end_utc.isoformat() if end_utc else None,
            "message": message.strip(),
        }
        async with self._submission_lock:
            row, created = self.repository.create(values)
            if created:
                try:
                    audit(
                        interaction.user.id,
                        email,
                        "FORM_SUBMITTED",
                        f"Submission={row['submission_id']}; Type={state.form_type}; "
                        f"Subtype={state.form_subtype or ''}",
                    )
                except Exception as exc:
                    log.exception("Could not audit form submission %s", row["submission_id"])
                    await self.alert_error("submission_audit_failed", row=row, error=exc)
            if row["sheets_sync_status"] != "SYNCED":
                try:
                    row["_sheets_synced"] = await self.sync_one(row)
                except Exception as exc:
                    log.exception("Unexpected Sheets delivery error for %s", row["submission_id"])
                    await self.alert_error("sheets_delivery_failed", row=row, error=exc)
                    row["_sheets_synced"] = False
            else:
                row["_sheets_synced"] = True
            if row.get("review_message_id"):
                row["_review_posted"] = True
            else:
                try:
                    row["_review_posted"] = await self.post_review(row)
                except Exception as exc:
                    log.exception("Unexpected review delivery error for %s", row["submission_id"])
                    await self.alert_error("discord_review_delivery_failed", row=row, error=exc)
                    row["_review_posted"] = False
            return row

    async def sync_one(self, row: dict):
        try:
            await asyncio.to_thread(self.forms_sheet.sync_submission, row)
            self.repository.mark_synced(row["submission_id"])
            audit(
                row["discord_user_id"], row["employee_email"], "FORM_SHEETS_SYNCED",
                f"Submission={row['submission_id']}; Type={row['form_type']}",
            )
            return True
        except Exception as exc:
            first_failure = not row.get("sheets_last_error")
            self.repository.mark_sync_failed(row["submission_id"], repr(exc))
            audit(
                row["discord_user_id"], row["employee_email"], "FORM_SHEETS_SYNC_FAILED",
                f"Submission={row['submission_id']}; Type={row['form_type']}; Error={type(exc).__name__}",
            )
            log.exception("Google Sheets sync failed for %s", row["submission_id"])
            if first_failure:
                await self.alert_error("sheets_delivery_failed", row=row, error=exc)
            return False

    async def retry_pending(self):
        for row in self.repository.pending_sync():
            await self.sync_one(row)

    async def post_review(self, row: dict):
        from forms.views import ReviewView, review_embed

        env_name = REVIEW_CHANNEL_ENV[row["form_type"]]
        try:
            channel_id = int(os.getenv(env_name, "0"))
        except ValueError:
            channel_id = 0
        if not channel_id:
            self._review_channel_error(row, f"{env_name} is not configured")
            await self.alert_error("review_channel_not_configured", row=row)
            return False
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.bot.guild_id:
                raise RuntimeError("configured channel is not a text channel in the authorized server")
            if channel.permissions_for(channel.guild.default_role).view_channel:
                raise RuntimeError("configured review channel is visible to @everyone")
            message = await channel.send(
                embed=review_embed(row),
                view=ReviewView(self, row["form_type"], row.get("form_subtype")),
            )
            self.repository.set_review_message(row["submission_id"], channel.id, message.id)
            return True
        except Exception as exc:
            self._review_channel_error(row, repr(exc))
            log.exception("Could not post review message for %s", row["submission_id"])
            await self.alert_error("discord_review_delivery_failed", row=row, error=exc)
            return False

    def _review_channel_error(self, row: dict, detail: str):
        audit(
            row["discord_user_id"], row["employee_email"], "FORM_REVIEW_CHANNEL_UNAVAILABLE",
            f"Submission={row['submission_id']}; Error={detail[:300]}",
        )

    async def review(self, interaction: discord.Interaction, action: str):
        if interaction.guild_id != self.bot.guild_id or not is_admin(interaction):
            await interaction.response.send_message("Not authorized to review forms.", ephemeral=True)
            return
        row = self.repository.get_by_review_message(interaction.message.id)
        if not row:
            await interaction.response.send_message("This submission could not be found.", ephemeral=True)
            return
        if row["discord_user_id"] == interaction.user.id:
            await interaction.response.send_message("You cannot review your own submission.", ephemeral=True)
            return

        try:
            new_status = status_for_action(row["form_type"], row.get("form_subtype"), action)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._review_lock:
            row = self.repository.get(row["submission_id"])
            if row["status"] in FINAL_STATUSES:
                await interaction.followup.send("This submission is already complete.", ephemeral=True)
                return
            reviewer = get_verified_user(interaction.user.id)
            reviewed_utc = datetime.now(timezone.utc)
            reviewed_et = reviewed_utc.astimezone(EASTERN)
            row = self.repository.review(
                row["submission_id"], new_status, interaction.user.id,
                reviewer[1] if reviewer else None,
                reviewed_utc.isoformat(), reviewed_et.isoformat(),
            )
            audit(
                row["discord_user_id"], row["employee_email"], "FORM_STATUS_CHANGED",
                f"Submission={row['submission_id']}; Reviewer={interaction.user.id}; Status={new_status}",
            )
            audit(
                row["discord_user_id"], row["employee_email"],
                "FORM_NEEDS_INFO" if new_status == "Needs Information" else "FORM_REVIEWED",
                f"Submission={row['submission_id']}; Reviewer={interaction.user.id}; Status={new_status}",
            )

        await self.sync_one(row)
        from forms.views import ReviewView, review_embed
        try:
            await interaction.message.edit(
                embed=review_embed(row),
                view=ReviewView(
                    self, row["form_type"], row.get("form_subtype"),
                    disabled=row["status"] in FINAL_STATUSES,
                ),
            )
        except discord.HTTPException as exc:
            log.exception("Could not update review message for %s", row["submission_id"])
            await self.alert_error("discord_review_update_failed", row=row, error=exc)

        if new_status == "Needs Information":
            await self._notify_submitter(row)
        await interaction.followup.send(
            f"{row['submission_id']} updated to {new_status}.", ephemeral=True
        )

    async def _notify_submitter(self, row: dict):
        try:
            user = self.bot.get_user(row["discord_user_id"]) or await self.bot.fetch_user(
                row["discord_user_id"]
            )
            await user.send(
                f"Operations needs more information for form {row['submission_id']}. "
                "Please contact an Operations reviewer."
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not DM submitter for %s", row["submission_id"])


class FormsSync:
    def __init__(self, bot, service: FormsService, minutes: int):
        self.bot = bot
        self.service = service
        self.loop.change_interval(minutes=minutes)

    @tasks.loop(minutes=5)
    async def loop(self):
        try:
            await self.service.retry_pending()
        except Exception as exc:
            log.exception("Forms sync loop failed")
            await self.service.alert_error("forms_sync_loop_failed", error=exc)

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
