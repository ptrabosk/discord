from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from bot.permissions import can_review
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
    working_location_dates,
)
from integrations.forms_sheets import FormsSheet
from integrations.google_sheets import field, full_name, truthy

log = logging.getLogger(__name__)

REVIEW_CHANNEL_ENV = {
    "when_i_work": "WIW_REVIEW_CHANNEL_ID",
    "question": "QUESTION_REVIEW_CHANNEL_ID",
    "concern": "CONCERN_REVIEW_CHANNEL_ID",
    "feedback": "FEEDBACK_REVIEW_CHANNEL_ID",
    "dispute": "DISPUTE_REVIEW_CHANNEL_ID",
    "working_location": "WORKING_LOCATION_REVIEW_CHANNEL_ID",
}
DEFAULT_FORM_ERROR_CHANNEL_ID = 1550994656966344714



def is_timesheet(row: dict) -> bool:
    return row["form_type"] == "when_i_work" and row.get("form_subtype") == "timesheet_adjustment"


def validate_submission(state: FormState, message: str) -> None:
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
    if state.form_type == "dispute":
        if not (state.reference_message_id or "").strip():
            raise ValueError("A message ID is required.")
    elif state.reference_message_id is not None:
        raise ValueError("Message ID is only valid for disputes.")
    if state.form_type == "working_location":
        working_location_dates(state)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def submission_values(
    interaction: discord.Interaction,
    state: FormState,
    message: str,
    email: str,
    employee_name: str,
    submitted_utc: datetime,
    concierge_email: str | None = None,
) -> dict:
    """The form_submissions row for a validated form; When I Work forms carry their start and end times."""
    start_et = start_utc = end_et = end_utc = start_day = end_day = None
    if state.form_type == "when_i_work":
        start_et, start_utc, end_et, end_utc = form_datetimes(state)
    if state.form_type == "working_location":
        start_day, end_day = working_location_dates(state)
    return {
        "interaction_id": str(interaction.id),
        "form_type": state.form_type,
        "form_subtype": state.form_subtype,
        "discord_user_id": interaction.user.id,
        "discord_username": str(interaction.user),
        "discord_display_name": interaction.user.display_name,
        "employee_email": email,
        "employee_name": employee_name,
        "concierge_email": concierge_email,
        "submitted_at_utc": submitted_utc.isoformat(),
        "submitted_at_et": submitted_utc.astimezone(EASTERN).isoformat(),
        "category": state.category,
        "adjustment_type": state.adjustment_type,
        "start_time_et": _iso(start_et),
        "start_time_utc": _iso(start_utc),
        "end_time_et": _iso(end_et),
        "end_time_utc": _iso(end_utc),
        "start_date": start_day,
        "end_date": end_day,
        "reference_message_id": (state.reference_message_id or "").strip() or None,
        "message": message.strip(),
    }


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
        """Send operational failures to a private channel, never to agents."""
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

    async def employee_identity(self, user_id: int) -> tuple[str, str, str | None]:
        email = self.verified_email(user_id)
        if not email:
            raise PermissionError("Complete work-email verification before submitting a form.")
        record = await asyncio.to_thread(self.access_sheet.get_by_email, email)
        if not record or not truthy(record.get("Active")):
            raise PermissionError("Your verified agent record is not currently active.")
        name = full_name(record)
        if not name:
            raise ValueError("Your agent record is missing a name. Contact Operations.")
        return email, name, field(record, "Concierge Email") or None

    async def submit(self, interaction: discord.Interaction, state: FormState, message: str) -> dict:
        if interaction.guild_id != self.bot.guild_id:
            raise PermissionError("Forms can only be submitted in the authorized server.")
        email, employee_name, concierge_email = await self.employee_identity(interaction.user.id)
        validate_submission(state, message)
        values = submission_values(
            interaction, state, message, email, employee_name, datetime.now(timezone.utc), concierge_email,
        )
        async with self._submission_lock:
            row, created = self.repository.create(values)
            if created:
                await self._audit_submission(interaction.user.id, email, state, row)
            row["_sheets_synced"] = await self._deliver_to_sheets(row)
            row["_review_posted"] = await self._deliver_review(row)
            return row

    async def _audit_submission(self, user_id: int, email: str, state: FormState, row: dict) -> None:
        try:
            audit(
                user_id,
                email,
                "FORM_SUBMITTED",
                f"Submission={row['submission_id']}; Type={state.form_type}; "
                f"Subtype={state.form_subtype or ''}",
            )
        except Exception as exc:
            log.exception("Could not audit form submission %s", row["submission_id"])
            await self.alert_error("submission_audit_failed", row=row, error=exc)

    async def _deliver_to_sheets(self, row: dict) -> bool:
        if row["sheets_sync_status"] == "SYNCED":
            return True
        try:
            return await self.sync_one(row)
        except Exception as exc:
            log.exception("Unexpected Sheets delivery error for %s", row["submission_id"])
            await self.alert_error("sheets_delivery_failed", row=row, error=exc)
            return False

    async def _deliver_review(self, row: dict) -> bool:
        if row.get("review_message_id"):
            return True
        try:
            return await self.post_review(row)
        except Exception as exc:
            log.exception("Unexpected review delivery error for %s", row["submission_id"])
            await self.alert_error("discord_review_delivery_failed", row=row, error=exc)
            return False

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
        if interaction.guild_id != self.bot.guild_id or not can_review(interaction):
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

        if is_timesheet(row) and action == "reject":
            from forms.views import TimesheetRejectModal
            await interaction.response.send_modal(TimesheetRejectModal(self, row["submission_id"], interaction.message))
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
        if is_timesheet(row) and new_status == "Approved":
            from forms.views import _datetime_label
            await self._dm_requester(
                row,
                f"Your timesheet adjustment request for {_datetime_label(row['start_time_et'])} has been approved!",
            )
        await interaction.followup.send(
            f"{row['submission_id']} updated to {new_status}.", ephemeral=True
        )

    async def reject_timesheet(self, interaction: discord.Interaction, submission_id: str, reason: str,
                               review_message: discord.Message):
        """Reject a timesheet adjustment with a reason: DM the requester and add the same message to the card's thread."""
        if interaction.guild_id != self.bot.guild_id or not can_review(interaction):
            await interaction.followup.send("Not authorized to review forms.", ephemeral=True)
            return
        if not reason:
            await interaction.followup.send("A reason is required.", ephemeral=True)
            return
        async with self._review_lock:
            row = self.repository.get(submission_id)
            if row["status"] in FINAL_STATUSES:
                await interaction.followup.send("This submission is already complete.", ephemeral=True)
                return
            reviewer = get_verified_user(interaction.user.id)
            reviewed_utc = datetime.now(timezone.utc)
            reviewed_et = reviewed_utc.astimezone(EASTERN)
            row = self.repository.review(
                submission_id, "Rejected", interaction.user.id,
                reviewer[1] if reviewer else None,
                reviewed_utc.isoformat(), reviewed_et.isoformat(),
            )
            audit(
                row["discord_user_id"], row["employee_email"], "FORM_STATUS_CHANGED",
                f"Submission={submission_id}; Reviewer={interaction.user.id}; Status=Rejected",
            )
        await self.sync_one(row)

        from forms.views import ReviewView, review_embed
        try:
            await review_message.edit(embed=review_embed(row), view=ReviewView(self, row["form_type"], row["form_subtype"], disabled=True))
        except discord.HTTPException as exc:
            log.exception("Could not update review message for %s", submission_id)
            await self.alert_error("discord_review_update_failed", row=row, error=exc)

        text = f"Please submit a new Timesheet Adjustment. Reason: {reason}"
        await self._dm_requester(row, text)
        try:
            thread = review_message.thread or await review_message.create_thread(name=f"{submission_id} rejected"[:100])
            await thread.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.exception("Could not post the rejection to the thread for %s", submission_id)
            await self.alert_error("discord_review_thread_failed", row=row, error=exc)
        await interaction.followup.send(f"{submission_id} rejected and the requester was notified.", ephemeral=True)

    async def _dm_requester(self, row: dict, text: str) -> bool:
        try:
            user = self.bot.get_user(row["discord_user_id"]) or await self.bot.fetch_user(row["discord_user_id"])
            await user.send(text, allowed_mentions=discord.AllowedMentions.none())
            audit(row["discord_user_id"], row["employee_email"], "FORM_DM_SENT", f"Submission={row['submission_id']}")
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not DM requester for %s", row["submission_id"])
            audit(row["discord_user_id"], row["employee_email"], "FORM_DM_FAILED", f"Submission={row['submission_id']}")
            return False

    async def dm_target(self, interaction: discord.Interaction) -> dict | None:
        """The Qs & Feedback submission behind a review card, if this reviewer may message its submitter."""
        if interaction.guild_id != self.bot.guild_id or not can_review(interaction):
            await interaction.response.send_message("Not authorized to review forms.", ephemeral=True)
            return None
        row = self.repository.get_by_review_message(interaction.message.id)
        if not row or row["form_type"] != "question":
            await interaction.response.send_message("This submission could not be found.", ephemeral=True)
            return None
        if row["discord_user_id"] == interaction.user.id:
            await interaction.response.send_message("You cannot review your own submission.", ephemeral=True)
            return None
        return row

    async def send_dm(self, interaction: discord.Interaction, submission_id: str, text: str):
        if interaction.guild_id != self.bot.guild_id or not can_review(interaction):
            await interaction.followup.send("Not authorized to review forms.", ephemeral=True)
            return
        row = self.repository.get(submission_id)
        if not text:
            await interaction.followup.send("The message cannot be empty.", ephemeral=True)
            return
        try:
            user = self.bot.get_user(row["discord_user_id"]) or await self.bot.fetch_user(row["discord_user_id"])
            await user.send(
                f"**Message from Operations about your Qs & Feedback submission {row['submission_id']}**\n\n{text}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not DM submitter for %s: %r", row["submission_id"], exc)
            audit(row["discord_user_id"], row["employee_email"], "FORM_DM_FAILED",
                  f"Submission={row['submission_id']}; Reviewer={interaction.user.id}")
            await interaction.followup.send(
                "The DM could not be delivered. The agent may have DMs from server members turned off.",
                ephemeral=True,
            )
            return
        audit(row["discord_user_id"], row["employee_email"], "FORM_DM_SENT",
              f"Submission={row['submission_id']}; Reviewer={interaction.user.id}")
        try:
            await interaction.message.reply(
                f"DM sent to the submitter by <@{interaction.user.id}>:\n>>> {text}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (AttributeError, discord.HTTPException):
            log.warning("Could not note the DM on the review card for %s", row["submission_id"])
        await interaction.followup.send(f"DM sent for {row['submission_id']}.", ephemeral=True)

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
