from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

import discord

from database.database import audit
from forms.domain import (
    ADJUSTMENT_TYPES,
    CATEGORIES,
    EASTERN,
    FINAL_STATUSES,
    FORM_CONFIG,
    START_HOURS,
    FormState,
    allowed_actions,
)

log = logging.getLogger(__name__)


def _date_label(day: date) -> str:
    return f"{day:%A, %b} {day.day}, {day.year}"


def _datetime_label(value: str | None) -> str:
    if not value:
        return "—"
    parsed = datetime.fromisoformat(value).astimezone(EASTERN)
    return f"{parsed:%b} {parsed.day}, {parsed.year} {parsed:%I:%M %p} ET"


async def _ephemeral_error(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float | None = 900):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This form belongs to another user.", ephemeral=True)
        return False

    async def on_error(self, interaction, error, item):
        log.exception("Form interaction failed", exc_info=error)
        await _ephemeral_error(interaction, "The form could not continue. Please try again.")
        await self.service.alert_error("form_interaction_failed", interaction_id=interaction.id, error=error)


class ChoiceSelect(discord.ui.Select):
    def __init__(self, options, placeholder: str):
        super().__init__(options=options, placeholder=placeholder, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await self.view.choose(interaction, self.values[0])


async def open_private_form(interaction: discord.Interaction, service):
    if interaction.guild_id != service.bot.guild_id:
        await interaction.response.send_message(
            "Forms can only be submitted in the authorized server.", ephemeral=True
        )
        return
    email = service.verified_email(interaction.user.id)
    if not email:
        await interaction.response.send_message(
            "You must complete work-email verification before submitting a form.",
            ephemeral=True,
        )
        return
    audit(interaction.user.id, email, "FORM_STARTED")
    await interaction.response.send_message(
        FORM_CONFIG["prompts"]["form_type"],
        view=TopLevelView(service, interaction.user.id),
        ephemeral=True,
    )


class FormLauncherView(discord.ui.View):
    """Keep older posted buttons working; new forms start with /form."""

    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(
        label="Submit a Form",
        style=discord.ButtonStyle.primary,
        custom_id="opsbot:forms:submit",
    )
    async def submit_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_private_form(interaction, self.service)

    async def on_error(self, interaction, error, item):
        log.exception("Form launcher failed", exc_info=error)
        await _ephemeral_error(interaction, "The form could not be opened. Please try again.")
        await self.service.alert_error("form_launcher_failed", interaction_id=interaction.id, error=error)


class TopLevelView(OwnedView):
    def __init__(self, service, owner_id: int):
        super().__init__(owner_id)
        self.service = service
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=label, value=key)
             for key, label in FORM_CONFIG["form_names"].items()],
            "Select a form type",
        ))

    async def choose(self, interaction, value):
        if value == "when_i_work":
            await interaction.response.edit_message(
                content=FORM_CONFIG["prompts"]["when_i_work"],
                view=WhenIWorkView(self.service, self.owner_id),
            )
            return
        state = FormState(form_type=value)
        await interaction.response.edit_message(
            content=FORM_CONFIG["prompts"]["category"],
            view=CategoryView(self.service, self.owner_id, state),
        )


class WhenIWorkView(OwnedView):
    def __init__(self, service, owner_id: int):
        super().__init__(owner_id)
        self.service = service
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=label, value=key)
             for key, label in FORM_CONFIG["when_i_work_names"].items()],
            "Select a When I Work form",
        ))

    async def choose(self, interaction, value):
        state = FormState(form_type="when_i_work", form_subtype=value)
        if value == "timesheet_adjustment":
            await interaction.response.edit_message(
                content=FORM_CONFIG["prompts"]["adjustment_type"],
                view=AdjustmentTypeView(self.service, self.owner_id, state),
            )
            return
        await interaction.response.edit_message(
            content=(
                "**Enter the scheduled shift date and start time in New York / Eastern Time.**\n\n"
                f"{FORM_CONFIG['prompts']['start_date_range']}"
            ),
            view=DateRangeView(self.service, self.owner_id, state, "start"),
        )


class CategoryView(OwnedView):
    def __init__(self, service, owner_id: int, state: FormState):
        super().__init__(owner_id)
        self.service = service
        self.state = state
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=item, value=item) for item in CATEGORIES],
            "Select a category",
        ))

    async def choose(self, interaction, value):
        self.state.category = value
        await interaction.response.send_modal(MessageModal(self.service, self.state))


class AdjustmentTypeView(OwnedView):
    def __init__(self, service, owner_id: int, state: FormState):
        super().__init__(owner_id)
        self.service = service
        self.state = state
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=item, value=item) for item in ADJUSTMENT_TYPES],
            "Select an adjustment type",
        ))

    async def choose(self, interaction, value):
        self.state.adjustment_type = value
        today = datetime.now(EASTERN).date()
        await interaction.response.edit_message(
            content=(
                "**All dates and times must be entered in New York / Eastern Time.**\n\n"
                f"{FORM_CONFIG['prompts']['start_date']}"
            ),
            view=DateChoiceView(
                self.service, self.owner_id, self.state, "start",
                today - timedelta(days=14), today,
            ),
        )


def _date_limits() -> tuple[date, date]:
    try:
        past = max(0, int(os.getenv("FORM_DATE_PAST_DAYS", "30")))
        future = max(0, int(os.getenv("FORM_DATE_FUTURE_DAYS", "30")))
    except ValueError:
        past = future = 30
    today = datetime.now(EASTERN).date()
    return today - timedelta(days=past), today + timedelta(days=future)


class DateRangeView(OwnedView):
    def __init__(self, service, owner_id: int, state: FormState, target: str):
        super().__init__(owner_id)
        self.service = service
        self.state = state
        self.target = target
        first, last = _date_limits()
        ranges = []
        cursor = first
        while cursor <= last:
            end = min(cursor + timedelta(days=13), last)
            ranges.append((cursor, end))
            cursor = end + timedelta(days=1)
        if len(ranges) > 25:
            raise ValueError("Configured form date range is too large; maximum is 350 days.")
        self.ranges = ranges
        self.add_item(ChoiceSelect([
            discord.SelectOption(
                label=f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}",
                value=str(index),
            )
            for index, (start, end) in enumerate(ranges)
        ], f"Select the {target} date range"))

    async def choose(self, interaction, value):
        start, end = self.ranges[int(value)]
        await interaction.response.edit_message(
            content=(
                f"{FORM_CONFIG['prompts'][self.target + '_date']} "
                "All dates and times are New York / Eastern Time."
            ),
            view=DateChoiceView(
                self.service, self.owner_id, self.state, self.target, start, end
            ),
        )


class DateChoiceView(OwnedView):
    def __init__(self, service, owner_id, state, target, first, last):
        super().__init__(owner_id)
        self.service = service
        self.state = state
        self.target = target
        options = []
        cursor = first
        while cursor <= last:
            options.append(discord.SelectOption(label=_date_label(cursor), value=cursor.isoformat()))
            cursor += timedelta(days=1)
        self.add_item(ChoiceSelect(options, f"Select the {target} date"))

    async def choose(self, interaction, value):
        setattr(self.state, f"{self.target}_date", date.fromisoformat(value))
        await interaction.response.edit_message(
            content=(
                f"{FORM_CONFIG['prompts'][self.target + '_hour']} "
                "All dates and times are New York / Eastern Time."
            ),
            view=HourView(self.service, self.owner_id, self.state, self.target),
        )


class HourView(OwnedView):
    def __init__(self, service, owner_id, state, target):
        super().__init__(owner_id)
        self.service = service
        self.state = state
        self.target = target
        options = []
        for hour in START_HOURS if target == "start" else range(24):
            label_hour = hour % 12 or 12
            suffix = "am" if hour < 12 else "pm"
            label = f"{label_hour}{suffix}" if target == "start" else f"{label_hour} {suffix.upper()}"
            options.append(discord.SelectOption(label=label, value=str(hour)))
        self.add_item(ChoiceSelect(options, f"Select the {target} time" if target == "start" else f"Select the {target} hour"))

    async def choose(self, interaction, value):
        setattr(self.state, f"{self.target}_hour", int(value))
        if self.target == "start":
            self.state.start_minute = 0
            if self.state.form_subtype == "timesheet_adjustment":
                await interaction.response.edit_message(
                    content=(
                        "**All dates and times must be entered in New York / Eastern Time.**\n\n"
                        f"{FORM_CONFIG['prompts']['end_hour']}\n"
                        "The end date is the same as the selected start date."
                    ),
                    view=HourView(self.service, self.owner_id, self.state, "end"),
                )
                return
            await interaction.response.send_modal(MessageModal(self.service, self.state))
            return
        await interaction.response.edit_message(
            content=(
                f"{FORM_CONFIG['prompts'][self.target + '_minute']} "
                "All dates and times are New York / Eastern Time."
            ),
            view=MinuteView(self.service, self.owner_id, self.state, self.target),
        )


class MinuteView(OwnedView):
    def __init__(self, service, owner_id, state, target):
        super().__init__(owner_id)
        self.service = service
        self.state = state
        self.target = target
        self.add_item(ChoiceSelect([
            discord.SelectOption(label=f":{minute:02d}", value=str(minute))
            for minute in (0, 15, 30, 45)
        ], f"Select the {target} minutes"))

    async def choose(self, interaction, value):
        setattr(self.state, f"{self.target}_minute", int(value))
        await interaction.response.send_modal(MessageModal(self.service, self.state))


class MessageModal(discord.ui.Modal):
    def __init__(self, service, state: FormState):
        key = state.form_subtype if state.form_type == "when_i_work" else state.form_type
        fields = FORM_CONFIG["message_fields"][key]
        super().__init__(title=fields["title"], timeout=900)
        self.service = service
        self.state = state
        self.message_input = discord.ui.TextInput(
            label=fields["question"],
            placeholder=fields["placeholder"],
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=1000,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.service.submit(interaction, self.state, str(self.message_input))
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except ValueError as exc:
            await interaction.followup.send(f"Form validation failed: {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("Could not persist form submission")
            await self.service.alert_error(
                "submission_processing_failed",
                interaction_id=interaction.id,
                error=exc,
            )
            await interaction.followup.send(
                "We could not confirm your submission. Please contact Operations before trying again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(_confirmation(row), ephemeral=True)


def _confirmation(row: dict) -> str:
    reference = row["submission_id"]
    warning = ""
    if row.get("_review_posted") is False and row.get("_sheets_synced") is False:
        warning = "\n\nDelivery failed. Please contact Operations with this reference."
    if row.get("form_subtype") == "timesheet_adjustment":
        return (
            "Submission received.\n\nType: Timesheet Adjustment\n"
            f"Reference: {reference}\n\nYour request has been submitted for review.{warning}"
        )
    if row.get("form_subtype") == "emergency_shift_release":
        return (
            "Emergency shift release submitted.\n\n"
            f"Reference: {reference}\n\n"
            "Submitting this request does not confirm that the shift has been released. "
            "You remain responsible for the shift until the request is approved or coverage is confirmed."
            f"{warning}"
        )
    label = FORM_CONFIG["form_names"][row["form_type"]]
    return f"{label} submitted.\nReference: {reference}{warning}"


ACTION_BUTTONS = {
    "approve": ("Approve", discord.ButtonStyle.success),
    "reject": ("Reject", discord.ButtonStyle.danger),
    "need_info": ("Need Info", discord.ButtonStyle.secondary),
    "answered": ("Answered", discord.ButtonStyle.success),
    "close": ("Close", discord.ButtonStyle.secondary),
    "reviewed": ("Reviewed", discord.ButtonStyle.success),
    "follow_up": ("Follow-up Required", discord.ButtonStyle.primary),
}


class ReviewButton(discord.ui.Button):
    def __init__(self, action: str, service, disabled: bool = False):
        label, style = ACTION_BUTTONS[action]
        super().__init__(
            label=label,
            style=style,
            custom_id=f"opsbot:forms:review:{action}",
            disabled=disabled,
        )
        self.action = action
        self.service = service

    async def callback(self, interaction: discord.Interaction):
        await self.service.review(interaction, self.action)


class ReviewView(discord.ui.View):
    def __init__(self, service, form_type=None, form_subtype=None, disabled=False):
        super().__init__(timeout=None)
        self.service = service
        actions = (
            ACTION_BUTTONS
            if form_type is None
            else allowed_actions(form_type, form_subtype)
        )
        for action in actions:
            self.add_item(ReviewButton(action, service, disabled=disabled))

    async def on_error(self, interaction, error, item):
        log.exception("Form review interaction failed", exc_info=error)
        await _ephemeral_error(interaction, "The review action failed. Please try again.")
        await self.service.alert_error("form_review_failed", interaction_id=interaction.id, error=error)


def review_embed(row: dict) -> discord.Embed:
    title = (
        FORM_CONFIG["when_i_work_names"][row["form_subtype"]]
        if row["form_type"] == "when_i_work"
        else FORM_CONFIG["form_names"][row["form_type"]]
    )
    embed = discord.Embed(
        title=title,
        color=discord.Color.blue(),
    )
    embed.add_field(name="Submission ID", value=row["submission_id"], inline=False)
    embed.add_field(name="Employee", value=row["employee_name"], inline=True)
    embed.add_field(name="Employee Email", value=row["employee_email"], inline=True)
    embed.add_field(name="Status", value=row["status"], inline=True)
    if row.get("adjustment_type"):
        embed.add_field(name="Adjustment Type", value=row["adjustment_type"], inline=False)
    if row.get("category"):
        embed.add_field(name="Category", value=row["category"], inline=False)
    if row.get("start_time_et"):
        label = "Shift Start ET" if row.get("form_subtype") == "emergency_shift_release" else "Start Time ET"
        embed.add_field(name=label, value=_datetime_label(row["start_time_et"]), inline=False)
    if row.get("end_time_et"):
        embed.add_field(name="End Time ET", value=_datetime_label(row["end_time_et"]), inline=False)
    message_label = {
        "question": "Question",
        "concern": "Concern",
        "feedback": "Feedback",
    }.get(row["form_type"], "Reason")
    embed.add_field(name=message_label, value=row["message"][:1024], inline=False)
    embed.add_field(name="Submitted At", value=_datetime_label(row["submitted_at_et"]), inline=False)
    if row.get("reviewed_by_discord_id"):
        embed.add_field(
            name="Reviewed By",
            value=row.get("reviewed_by_email") or f"Discord user {row['reviewed_by_discord_id']}",
            inline=True,
        )
        embed.add_field(name="Reviewed At", value=_datetime_label(row["reviewed_at_et"]), inline=True)
    return embed


def register_forms(bot, service):
    bot.add_view(FormLauncherView(service))
    bot.add_view(ReviewView(service))

    @bot.tree.command(name="form", description="Open your private employee form")
    async def form(interaction: discord.Interaction):
        try:
            await open_private_form(interaction, service)
        except Exception as exc:
            log.exception("Could not open private form")
            await _ephemeral_error(interaction, "The form could not be opened. Please try again.")
            await service.alert_error("form_open_failed", interaction_id=interaction.id, error=exc)
