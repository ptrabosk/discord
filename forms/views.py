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
    FORM_CONFIG,
    FormState,
    allowed_actions,
    parse_clock_time,
    parse_location_date,
    shown_email,
    uses_concierge_email,
)

log = logging.getLogger(__name__)

# Concern and Feedback are retired from the menu; their stored rows still render and review.
MENU_FORMS = ("when_i_work", "question", "dispute", "working_location")


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
    """One private form step. `back` rebuilds the previous step; each step's text is its `prompt`."""

    prompt = ""

    def __init__(self, owner_id: int, *, back=None, timeout: float | None = 900):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.back = back
        if back:
            # Row 1 keeps Back under the step's select, which fills row 0.
            button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)
            button.callback = self.go_back
            self.add_item(button)

    async def go_back(self, interaction: discord.Interaction):
        view = self.back()
        await interaction.response.edit_message(content=view.prompt, view=view)

    async def show(self, interaction: discord.Interaction, view: "OwnedView"):
        await interaction.response.edit_message(content=view.prompt, view=view)

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
    view = TopLevelView(service, interaction.user.id)
    await interaction.response.send_message(view.prompt, view=view, ephemeral=True)


class FormLauncherView(discord.ui.View):
    """Keep older posted buttons working; new forms start with /workflow."""

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
        self.prompt = FORM_CONFIG["prompts"]["form_type"]
        self.again = lambda: TopLevelView(service, owner_id)
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=FORM_CONFIG["form_names"][key], value=key) for key in MENU_FORMS],
            "Select a form type",
        ))

    async def choose(self, interaction, value):
        if value == "when_i_work":
            await self.show(interaction, WhenIWorkView(self.service, self.owner_id, back=self.again))
            return
        state = FormState(form_type=value)
        if value in ("dispute", "working_location"):
            await interaction.response.send_modal(MessageModal(self.service, state))
            return
        await self.show(interaction, CategoryView(self.service, self.owner_id, state, back=self.again))


class WhenIWorkView(OwnedView):
    def __init__(self, service, owner_id: int, *, back=None):
        super().__init__(owner_id, back=back)
        self.service = service
        self.prompt = FORM_CONFIG["prompts"]["when_i_work"]
        self.again = lambda: WhenIWorkView(service, owner_id, back=back)
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=label, value=key)
             for key, label in FORM_CONFIG["when_i_work_names"].items()],
            "Select a When I Work form",
        ))

    async def choose(self, interaction, value):
        state = FormState(form_type="when_i_work", form_subtype=value)
        if value == "timesheet_adjustment":
            await self.show(interaction, AdjustmentTypeView(self.service, self.owner_id, state, back=self.again))
            return
        await interaction.response.send_modal(MessageModal(self.service, state))


class CategoryView(OwnedView):
    def __init__(self, service, owner_id: int, state: FormState, *, back=None):
        super().__init__(owner_id, back=back)
        self.service = service
        self.state = state
        self.prompt = FORM_CONFIG["prompts"]["category"]
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=item, value=item) for item in CATEGORIES],
            "Select a category",
        ))

    async def choose(self, interaction, value):
        self.state.category = value
        await interaction.response.send_modal(MessageModal(self.service, self.state))


class AdjustmentTypeView(OwnedView):
    def __init__(self, service, owner_id: int, state: FormState, *, back=None):
        super().__init__(owner_id, back=back)
        self.service = service
        self.state = state
        self.prompt = FORM_CONFIG["prompts"]["adjustment_type"]
        self.again = lambda: AdjustmentTypeView(service, owner_id, state, back=back)
        self.add_item(ChoiceSelect(
            [discord.SelectOption(label=item, value=item) for item in ADJUSTMENT_TYPES],
            "Select an adjustment type",
        ))

    async def choose(self, interaction, value):
        self.state.adjustment_type = value
        await interaction.response.send_modal(MessageModal(self.service, self.state))


def _date_limits() -> tuple[date, date]:
    try:
        past = max(0, int(os.getenv("FORM_DATE_PAST_DAYS", "30")))
        future = max(0, int(os.getenv("FORM_DATE_FUTURE_DAYS", "30")))
    except ValueError:
        past = future = 30
    today = datetime.now(EASTERN).date()
    return today - timedelta(days=past), today + timedelta(days=future)


class MessageModal(discord.ui.Modal):
    def __init__(self, service, state: FormState):
        key = state.form_subtype if state.form_type == "when_i_work" else state.form_type
        fields = FORM_CONFIG["message_fields"][key]
        super().__init__(title=fields["title"], timeout=900)
        self.service = service
        self.state = state
        self.message_id_input = self.start_input = self.end_input = None
        self.shift_date_input = self.start_time_input = self.end_time_input = None
        if state.form_type == "dispute":
            self.message_id_input = self._short_input("Message ID", "The ID of the message you are disputing", 100)
        if state.form_type == "working_location":
            self.start_input = self._short_input("Start Date (MM/DD/YYYY)", "Example: 11/10/2026", 10)
            self.end_input = self._short_input("End Date (MM/DD/YYYY)", "Example: 11/14/2026", 10)
        if state.form_subtype in ("timesheet_adjustment", "emergency_shift_release"):
            self.shift_date_input = self._short_input("Shift date (MM/DD/YYYY)", "Example: 11/10/2026 (ET)", 10)
            self.start_time_input = self._short_input("Start time (hh:mm AM/PM)", "Example: 09:00 AM (ET)", 8)
        if state.form_subtype == "timesheet_adjustment":
            self.end_time_input = self._short_input("End time (hh:mm AM/PM)", "Example: 05:30 PM (ET)", 8)
        self.message_input = discord.ui.TextInput(
            label=fields["question"],
            placeholder=fields["placeholder"],
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=1000,
        )
        self.add_item(self.message_input)

    def _short_input(self, label: str, placeholder: str, max_length: int) -> discord.ui.TextInput:
        field = discord.ui.TextInput(
            label=label, placeholder=placeholder, required=True, min_length=1, max_length=max_length,
        )
        self.add_item(field)
        return field

    def _apply_inputs(self):
        if self.message_id_input is not None:
            self.state.reference_message_id = str(self.message_id_input)
        if self.start_input is not None:
            self.state.location_start, self.state.location_end = str(self.start_input), str(self.end_input)
        if self.shift_date_input is not None:
            self.state.start_date = _parse_shift_date(str(self.shift_date_input), "Shift date")
        if self.start_time_input is not None:
            self.state.start_hour, self.state.start_minute = parse_clock_time(str(self.start_time_input), "Start time")
        if self.end_time_input is not None:
            self.state.end_hour, self.state.end_minute = parse_clock_time(str(self.end_time_input), "End time")

    async def on_submit(self, interaction: discord.Interaction):
        # A deferred update of the step message: on success it becomes the confirmation with no
        # controls, so the form cannot be submitted twice; on a rejected choice it keeps its Back button.
        await interaction.response.defer()
        try:
            self._apply_inputs()
            row = await self.service.submit(interaction, self.state, str(self.message_input))
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except ValueError as exc:
            await interaction.followup.send(f"Form validation failed: {exc} Open the form again to retry.", ephemeral=True)
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
        try:
            await interaction.edit_original_response(content=_confirmation(row), view=None)
        except discord.HTTPException:
            # Opened from the workflows buttons there is no step message to edit.
            await interaction.followup.send(_confirmation(row), ephemeral=True)


def _parse_shift_date(value: str, label: str) -> date:
    day = parse_location_date(value, label)
    first, last = _date_limits()
    if not first <= day <= last:
        raise ValueError(f"{label} must be between {first:%m/%d/%Y} and {last:%m/%d/%Y}.")
    return day


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
    label = FORM_CONFIG["message_fields"][row["form_type"]]["title"]
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


class SendDmButton(discord.ui.Button):
    def __init__(self, service):
        super().__init__(label="Send DM", style=discord.ButtonStyle.primary, custom_id="opsbot:forms:review:dm")
        self.service = service

    async def callback(self, interaction: discord.Interaction):
        row = await self.service.dm_target(interaction)
        if row:
            await interaction.response.send_modal(SendDmModal(self.service, row["submission_id"]))


class SendDmModal(discord.ui.Modal, title="Send DM to submitter"):
    message = discord.ui.TextInput(
        label="Message",
        placeholder="Sent privately to the agent who submitted this form",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1500,
    )

    def __init__(self, service, submission_id: str):
        super().__init__(timeout=900)
        self.service = service
        self.submission_id = submission_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.service.send_dm(interaction, self.submission_id, str(self.message).strip())

    async def on_error(self, interaction, error):
        log.exception("Form DM failed", exc_info=error)
        await _ephemeral_error(interaction, "The DM could not be sent. Please try again.")
        await self.service.alert_error("form_dm_failed", interaction_id=interaction.id, error=error)


class TimesheetRejectModal(discord.ui.Modal, title="Reject timesheet adjustment"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Sent to the requester, who will need to submit a new Timesheet Adjustment",
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1000,
    )

    def __init__(self, service, submission_id: str, review_message):
        super().__init__(timeout=900)
        self.service = service
        self.submission_id = submission_id
        self.review_message = review_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.service.reject_timesheet(
            interaction, self.submission_id, str(self.reason).strip(), self.review_message,
        )

    async def on_error(self, interaction, error):
        log.exception("Timesheet rejection failed", exc_info=error)
        await _ephemeral_error(interaction, "The rejection could not be completed. Please try again.")
        await self.service.alert_error("form_review_failed", interaction_id=interaction.id, error=error)


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
        # Qs & Feedback reviewers can message the submitter at any time, even after closing it.
        if form_type in (None, "question"):
            self.add_item(SendDmButton(service))

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
    embed.add_field(name="Agent", value=row["employee_name"], inline=True)
    concierge = uses_concierge_email(row["form_type"], row.get("form_subtype"))
    embed.add_field(
        name="Concierge Email" if concierge else "Agent Email",
        value=shown_email(row),
        inline=True,
    )
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
    if row.get("reference_message_id"):
        embed.add_field(name="Message ID", value=row["reference_message_id"], inline=False)
    if row.get("start_date"):
        embed.add_field(name="Start Date", value=row["start_date"], inline=True)
    if row.get("end_date"):
        embed.add_field(name="End Date", value=row["end_date"], inline=True)
    message_label = {
        "question": "Message",
        "concern": "Concern",
        "feedback": "Feedback",
        "dispute": "Dispute Reasoning",
        "working_location": "New Location",
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

    @bot.tree.command(name="workflow", description="Open your private agent form")
    async def workflow(interaction: discord.Interaction):
        try:
            await open_private_form(interaction, service)
        except Exception as exc:
            log.exception("Could not open private form")
            await _ephemeral_error(interaction, "The form could not be opened. Please try again.")
            await service.alert_error("form_open_failed", interaction_id=interaction.id, error=exc)
