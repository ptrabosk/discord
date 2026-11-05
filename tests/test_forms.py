import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from database.forms_repository import FormsRepository
from forms.domain import (
    FormState,
    CATEGORIES,
    EASTERN,
    FORM_CONFIG,
    START_HOURS,
    form_datetimes,
    load_form_config,
    local_datetime,
    sheet_tab_for,
    status_for_action,
    submission_prefix,
    validate_category,
)
from integrations.forms_sheets import HEADERS, sheet_values
from forms.views import (
    AdjustmentTypeView, CategoryView, MessageModal, TopLevelView, WhenIWorkView, _confirmation, _parse_shift_date,
    register_forms,
)
from forms.service import DEFAULT_FORM_ERROR_CHANNEL_ID, FormsService


def submission_values(interaction_id: str, form_type="question", form_subtype=None):
    return {
        "interaction_id": interaction_id,
        "form_type": form_type,
        "form_subtype": form_subtype,
        "discord_user_id": 123,
        "discord_username": "employee",
        "discord_display_name": "Employee",
        "employee_email": "employee@example.com",
        "employee_name": "Employee Name",
        "submitted_at_utc": "2026-09-19T13:00:00+00:00",
        "submitted_at_et": "2026-09-19T09:00:00-04:00",
        "category": "General" if form_type != "when_i_work" else None,
        "adjustment_type": None,
        "start_time_et": None,
        "start_time_utc": None,
        "end_time_et": None,
        "end_time_utc": None,
        "message": "Test message",
    }


def select_of(view):
    return next(item for item in view.children if isinstance(item, discord.ui.Select))


class DatetimeTests(unittest.TestCase):
    def test_winter_and_summer_et_to_utc(self):
        winter_et, winter_utc = local_datetime(date(2026, 1, 15), 9, 0)
        summer_et, summer_utc = local_datetime(date(2026, 7, 15), 9, 0)
        self.assertEqual(winter_et.utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(winter_utc.hour, 14)
        self.assertEqual(summer_et.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(summer_utc.hour, 13)

    def test_end_must_follow_start(self):
        state = FormState(
            form_type="when_i_work",
            form_subtype="timesheet_adjustment",
            start_date=date(2026, 9, 19),
            start_hour=10,
            start_minute=0,
            end_hour=9,
            end_minute=45,
        )
        with self.assertRaisesRegex(ValueError, "after start"):
            form_datetimes(state)

    def test_end_date_is_the_selected_start_date(self):
        state = FormState(
            form_type="when_i_work",
            form_subtype="timesheet_adjustment",
            start_date=date(2026, 9, 19),
            start_hour=8,
            start_minute=0,
            end_hour=17,
            end_minute=15,
        )
        start_et, _, end_et, _ = form_datetimes(state)
        self.assertEqual(start_et.date(), end_et.date())
        self.assertEqual(end_et.hour, 17)

    def test_nonexistent_dst_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "daylight saving"):
            local_datetime(date(2026, 3, 8), 2, 15)

    def test_start_time_must_be_one_of_the_configured_whole_hours(self):
        state = FormState("when_i_work", "emergency_shift_release", start_date=date(2026, 9, 19))
        state.start_hour, state.start_minute = 9, 0
        with self.assertRaisesRegex(ValueError, "available start times"):
            form_datetimes(state)
        state.start_hour, state.start_minute = 8, 15
        with self.assertRaisesRegex(ValueError, "available start times"):
            form_datetimes(state)


class ValidationTests(unittest.TestCase):
    def test_menu_offers_when_i_work_and_one_combined_qs_and_feedback_form(self):
        options = select_of(TopLevelView(None, 1)).options
        self.assertEqual([(o.label, o.value) for o in options], [
            ("When I Work", "when_i_work"), ("Qs & Feedback", "question"),
            ("Disputes", "dispute"), ("Working Location", "working_location"),
        ])
        modal = MessageModal(None, FormState(form_type="question", category="Pay"))
        self.assertEqual(modal.title, "Qs & Feedback")
        self.assertEqual(modal.message_input.label, "Ask anything")
        self.assertEqual(modal.message_input.placeholder, "Please make sure your description is clear.")

    def test_forms_open_with_the_workflow_command(self):
        commands = {}
        bot = SimpleNamespace(add_view=lambda view: None, tree=SimpleNamespace(
            command=lambda name, description: lambda func: commands.setdefault(name, func)))
        register_forms(bot, None)
        self.assertEqual(list(commands), ["workflow"])

    def test_editable_form_config_controls_modal_question(self):
        modal = MessageModal(None, FormState(form_type="question"))
        self.assertEqual(modal.message_input.label, FORM_CONFIG["message_fields"]["question"]["question"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forms.json"
            changed = json.loads(json.dumps(FORM_CONFIG))
            changed["categories"].append("New Category")
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertIn("New Category", load_form_config(path)["categories"])
            changed["categories"] = ["duplicate", "duplicate"]
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unique"):
                load_form_config(path)
            changed["categories"] = FORM_CONFIG["categories"]
            changed["start_hours"] = [8, 8]
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "start_hours"):
                load_form_config(path)

    def test_employee_confirmation_hides_internal_delivery_details(self):
        row = {"submission_id": "Q-20260919-0001", "form_type": "question"}
        for sheets_ok, review_ok in ((True, False), (False, True), (True, True)):
            message = _confirmation(row | {
                "_sheets_synced": sheets_ok,
                "_review_posted": review_ok,
            })
            self.assertNotIn("Google", message)
            self.assertNotIn("retry", message.lower())
            self.assertNotIn("Delivery failed", message)
        message = _confirmation(row | {"_sheets_synced": False, "_review_posted": False})
        self.assertIn("Delivery failed", message)
        self.assertNotIn("Google", message)

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_category("Anything Else")

    def test_sheet_mapping(self):
        self.assertEqual(
            sheet_tab_for("when_i_work", "timesheet_adjustment"),
            "WIW Timesheet Adjustments",
        )
        self.assertEqual(sheet_tab_for("feedback"), "Questions Concerns Feedback")

    def test_sheet_values_match_headers(self):
        row = submission_values("sheet-row") | {
            "submission_id": "Q-20260919-0001",
            "status": "Pending",
            "reviewed_by_email": None,
            "reviewed_by_discord_id": None,
            "reviewed_at_et": None,
            "reviewed_at_utc": None,
        }
        self.assertEqual(
            len(sheet_values(row)), len(HEADERS["Questions Concerns Feedback"])
        )

    def test_review_transitions(self):
        self.assertEqual(status_for_action("question", None, "answered"), "Answered")
        self.assertEqual(
            status_for_action("feedback", None, "follow_up"), "Follow-up Required"
        )
        with self.assertRaises(ValueError):
            status_for_action("question", None, "approve")

    def test_repository_rejects_unknown_status(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = FormsRepository(Path(directory) / "forms.db")
            repository.init()
            row, _ = repository.create(submission_values("review-status"))
            with self.assertRaisesRegex(ValueError, "Invalid review status"):
                repository.review(
                    row["submission_id"], "Arbitrary", 456, None,
                    "2026-09-19T14:00:00+00:00", "2026-09-19T10:00:00-04:00",
                )


class SubmissionIdTests(unittest.TestCase):
    def test_all_prefixes(self):
        self.assertEqual(submission_prefix("when_i_work", "timesheet_adjustment"), "WIW-TA")
        self.assertEqual(
            submission_prefix("when_i_work", "emergency_shift_release"), "WIW-ESR"
        )
        self.assertEqual(submission_prefix("question", None), "Q")
        self.assertEqual(submission_prefix("concern", None), "C")
        self.assertEqual(submission_prefix("feedback", None), "F")
        self.assertEqual(submission_prefix("dispute", None), "D")
        self.assertEqual(submission_prefix("working_location", None), "WL")

    def test_ids_have_prefix_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forms.db"
            repository = FormsRepository(path)
            repository.init()
            first, created = repository.create(submission_values("one"))
            self.assertTrue(created)
            self.assertEqual(first["submission_id"], "Q-20260919-0001")

            reopened = FormsRepository(path)
            reopened.init()
            second, created = reopened.create(
                submission_values("two", "when_i_work", "emergency_shift_release")
            )
            self.assertTrue(created)
            self.assertEqual(second["submission_id"], "WIW-ESR-20260919-0002")

            duplicate, created = reopened.create(submission_values("one"))
            self.assertFalse(created)
            self.assertEqual(duplicate["submission_id"], first["submission_id"])


class ErrorRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_error_goes_to_private_configured_channel_without_exception_text(self):
        class PrivateChannel:
            guild = SimpleNamespace(id=123, default_role=object())

            def permissions_for(self, role):
                return SimpleNamespace(view_channel=False)

            async def send(self, content, **kwargs):
                self.sent = content

        channel = PrivateChannel()
        seen = []

        class Bot:
            guild_id = 123

            def get_channel(self, channel_id):
                seen.append(channel_id)
                return channel

        service = FormsService(Bot(), SimpleNamespace(client=object()))
        with patch.dict(os.environ, {"FORM_ERROR_CHANNEL_ID": ""}), patch(
            "forms.service.discord.TextChannel", PrivateChannel
        ):
            await service.alert_error(
                "sheets_delivery_failed",
                row={"submission_id": "Q-20260919-0001"},
                error=RuntimeError("sensitive details must stay in logs"),
            )
        self.assertEqual(seen, [DEFAULT_FORM_ERROR_CHANNEL_ID])
        self.assertIn("Q-20260919-0001", channel.sent)
        self.assertNotIn("sensitive details", channel.sent)


class Text:
    """Stands in for a filled modal input; the modal reads inputs with str()."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class ShiftInputTests(unittest.IsolatedAsyncioTestCase):
    class Response:
        async def send_modal(self, modal):
            self.modal = modal

    async def test_timesheet_asks_for_adjustment_type_then_opens_the_typed_modal(self):
        response = self.Response()
        await AdjustmentTypeView(None, 123, FormState("when_i_work", "timesheet_adjustment")).choose(
            SimpleNamespace(response=response), FORM_CONFIG["adjustment_types"][0])
        labels = [item.label for item in response.modal.children]
        self.assertEqual(labels[:3], ["Shift date (MM/DD/YYYY)", "Start time (hh:mm AM/PM)", "End time (hh:mm AM/PM)"])

    async def test_emergency_release_opens_the_modal_with_date_and_start_time_only(self):
        response = self.Response()
        await WhenIWorkView(None, 123).choose(SimpleNamespace(response=response), "emergency_shift_release")
        labels = [item.label for item in response.modal.children]
        self.assertEqual(labels, ["Shift date (MM/DD/YYYY)", "Start time (hh:mm AM/PM)", "Release Reason"])

    def test_typed_shift_times_become_the_stored_times(self):
        day = datetime.now(EASTERN).date() + timedelta(days=1)
        state = FormState("when_i_work", "timesheet_adjustment", adjustment_type=FORM_CONFIG["adjustment_types"][0])
        modal = MessageModal(None, state)
        modal.shift_date_input, modal.start_time_input = Text(f"{day:%m/%d/%Y}"), Text("08:00 am")
        modal.end_time_input = Text("10:30 AM")
        modal._apply_inputs()
        start_et, _, end_et, _ = form_datetimes(state)
        self.assertEqual((start_et.date(), start_et.hour, start_et.minute), (day, 8, 0))
        self.assertEqual((end_et.hour, end_et.minute), (10, 30))

    def test_clock_times_must_be_h_mm_with_am_or_pm(self):
        from forms.domain import parse_clock_time
        self.assertEqual(parse_clock_time("12:15 PM", "Start time"), (12, 15))
        self.assertEqual(parse_clock_time("12:00am", "Start time"), (0, 0))
        self.assertEqual(parse_clock_time("9:00 AM", "Start time"), (9, 0))
        self.assertEqual(parse_clock_time("09:30 pm", "Start time"), (21, 30))
        for bad in ["09:00", "13:00 PM", "0:30 PM", "09:60 AM", "09:00 noon"]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "h:mm AM/PM"):
                parse_clock_time(bad, "Start time")

    def test_shift_dates_must_be_real_and_inside_the_form_window(self):
        with self.assertRaisesRegex(ValueError, "MM/DD/YYYY"):
            _parse_shift_date("2026-11-10", "Shift date")
        with self.assertRaisesRegex(ValueError, "between"):
            _parse_shift_date("01/01/2000", "Shift date")


class SubmitTests(unittest.IsolatedAsyncioTestCase):
    """FormsService.submit: validation, the stored row, and delivery to Sheets and the review channel."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FormsService(SimpleNamespace(guild_id=1), SimpleNamespace(client=object()))
        self.service.repository = FormsRepository(Path(self.directory.name) / "forms.db")
        self.service.repository.init()
        self.service.employee_identity = AsyncMock(
            return_value=("employee@example.com", "Employee Name", "concierge@example.com"),
        )
        self.service.sync_one = AsyncMock(return_value=True)
        self.service.post_review = AsyncMock(return_value=True)
        self.service.alert_error = AsyncMock()
        self.audit = patch("forms.service.audit").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.directory.cleanup)

    def interaction(self, interaction_id=1001, guild_id=1):
        user = type("User", (), {"id": 123, "display_name": "Employee", "__str__": lambda self: "employee"})()
        return SimpleNamespace(id=interaction_id, guild_id=guild_id, user=user)

    async def test_rejects_other_servers_empty_messages_and_bad_choices(self):
        with self.assertRaisesRegex(PermissionError, "authorized server"):
            await self.service.submit(self.interaction(guild_id=2), FormState("question", category="Pay"), "hi")
        with self.assertRaisesRegex(ValueError, "message or reason is required"):
            await self.service.submit(self.interaction(), FormState("question", category="Pay"), "   ")
        with self.assertRaisesRegex(ValueError, "Unknown form category"):
            await self.service.submit(self.interaction(), FormState("question", category="Nope"), "hi")
        with self.assertRaisesRegex(ValueError, "valid adjustment type"):
            await self.service.submit(self.interaction(), FormState("when_i_work", "timesheet_adjustment", adjustment_type="Not a type"), "hi")
        with self.assertRaisesRegex(ValueError, "only valid for timesheet adjustments"):
            await self.service.submit(self.interaction(), FormState("question", category="Pay", adjustment_type="Missed clock-in"), "hi")
        with self.assertRaisesRegex(ValueError, "Unknown form type"):
            await self.service.submit(self.interaction(), FormState("nonsense"), "hi")
        self.audit.assert_not_called()

    async def test_stores_the_submission_audits_it_and_delivers_it(self):
        row = await self.service.submit(self.interaction(), FormState("question", category="Pay"), "  Where is my pay?  ")
        self.assertEqual(row["message"], "Where is my pay?")
        self.assertEqual((row["form_type"], row["category"], row["employee_email"], row["employee_name"]), ("question", "Pay", "employee@example.com", "Employee Name"))
        self.assertIsNone(row["start_time_et"])
        self.assertTrue(row["_sheets_synced"])
        self.assertTrue(row["_review_posted"])
        self.audit.assert_called_once()
        self.assertEqual(self.audit.call_args.args[2], "FORM_SUBMITTED")
        self.assertIn(row["submission_id"], self.audit.call_args.args[3])
        self.service.sync_one.assert_awaited_once()
        self.service.post_review.assert_awaited_once()

    async def test_a_repeated_interaction_is_not_audited_twice(self):
        first = await self.service.submit(self.interaction(), FormState("question", category="Pay"), "hi")
        second = await self.service.submit(self.interaction(), FormState("question", category="Pay"), "hi")
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.audit.assert_called_once()

    async def test_timesheet_adjustment_records_both_times(self):
        state = FormState("when_i_work", "timesheet_adjustment", adjustment_type="Missed clock-in",
                          start_date=date(2026, 7, 15), start_hour=START_HOURS[0], start_minute=0, end_hour=START_HOURS[0] + 2, end_minute=30)
        row = await self.service.submit(self.interaction(), state, "Forgot to clock in")
        self.assertEqual(START_HOURS[0], 8)
        self.assertEqual(row["start_time_utc"], "2026-07-15T12:00:00+00:00")
        self.assertEqual(row["end_time_et"], "2026-07-15T10:30:00-04:00")
        self.assertEqual(row["adjustment_type"], "Missed clock-in")

    async def test_dispute_requires_and_records_the_message_id(self):
        with self.assertRaisesRegex(ValueError, "message ID is required"):
            await self.service.submit(self.interaction(), FormState("dispute", reference_message_id="  "), "Wrong")
        with self.assertRaisesRegex(ValueError, "message or reason is required"):
            await self.service.submit(self.interaction(), FormState("dispute", reference_message_id="42"), " ")
        with self.assertRaisesRegex(ValueError, "only valid for disputes"):
            await self.service.submit(self.interaction(), FormState("question", category="Pay", reference_message_id="42"), "hi")
        row = await self.service.submit(self.interaction(), FormState("dispute", reference_message_id=" 42 "), " Not mine ")
        self.assertTrue(row["submission_id"].startswith("D-"))
        self.assertEqual((row["reference_message_id"], row["message"], row["employee_email"]), ("42", "Not mine", "employee@example.com"))
        values = sheet_values(row)
        self.assertEqual(len(values), len(HEADERS["Disputes"]))
        self.assertEqual(HEADERS["Disputes"][:4], ["Timestamp", "Submitted By", "Send ID", "Dispute Reasoning"])
        self.assertEqual(values[HEADERS["Disputes"].index("Send ID")], "42")
        self.assertEqual(values[HEADERS["Disputes"].index("Submitted By")], "concierge@example.com")
        self.assertEqual(values[HEADERS["Disputes"].index("Dispute Reasoning")], "Not mine")
        self.assertEqual(values[0], int(datetime.fromisoformat(row["submitted_at_utc"]).timestamp()))

    async def test_working_location_requires_both_dates_and_records_them_as_typed(self):
        state = FormState("working_location", location_start="11/10/2026", location_end="  ")
        with self.assertRaisesRegex(ValueError, "end date are required"):
            await self.service.submit(self.interaction(), state, "Denver")
        state.location_end = "Nov 14"
        with self.assertRaisesRegex(ValueError, "End date must be in MM/DD/YYYY"):
            await self.service.submit(self.interaction(), state, "Denver")
        state.location_end = " 11/14/2026 "
        with self.assertRaisesRegex(ValueError, "message or reason is required"):
            await self.service.submit(self.interaction(), state, "  ")
        row = await self.service.submit(self.interaction(), state, "Denver, CO")
        self.assertTrue(row["submission_id"].startswith("WL-"))
        self.assertEqual((row["start_date"], row["end_date"], row["message"]), ("11/10/2026", "11/14/2026", "Denver, CO"))
        values = sheet_values(row)
        headers = HEADERS["Working Location"]
        self.assertEqual(len(values), len(headers))
        self.assertEqual(
            [values[headers.index(h)] for h in ("Agent Email", "Start Date", "End Date", "New Location")],
            ["employee@example.com", "11/10/2026", "11/14/2026", "Denver, CO"],
        )

    async def test_delivery_failures_are_alerted_and_reported_on_the_row(self):
        self.service.sync_one.side_effect = RuntimeError("sheets down")
        self.service.post_review.side_effect = RuntimeError("discord down")
        self.audit.side_effect = RuntimeError("audit down")
        row = await self.service.submit(self.interaction(), FormState("feedback", category="Pay"), "hi")
        self.assertFalse(row["_sheets_synced"])
        self.assertFalse(row["_review_posted"])
        kinds = [call.args[0] for call in self.service.alert_error.await_args_list]
        self.assertEqual(kinds, ["submission_audit_failed", "sheets_delivery_failed", "discord_review_delivery_failed"])

    async def test_already_delivered_rows_are_not_sent_again(self):
        row = await self.service.submit(self.interaction(), FormState("question", category="Pay"), "hi")
        self.service.repository.mark_synced(row["submission_id"])
        self.service.repository.set_review_message(row["submission_id"], 77, 555)
        self.service.sync_one.reset_mock()
        self.service.post_review.reset_mock()
        again = await self.service.submit(self.interaction(), FormState("question", category="Pay"), "hi")
        self.assertTrue(again["_sheets_synced"])
        self.assertTrue(again["_review_posted"])
        self.service.sync_one.assert_not_awaited()
        self.service.post_review.assert_not_awaited()


class ReviewDmTests(SubmitTests.__base__):
    """The Send DM button on Qs & Feedback review cards."""

    async def asyncSetUp(self):
        await SubmitTests.asyncSetUp(self)
        self.employee = SimpleNamespace(send=AsyncMock())
        self.service.bot.get_user = lambda user_id: self.employee if user_id == 123 else None
        self.row = await self.service.submit(SubmitTests.interaction(self), FormState("question", category="Pay"), "hi")
        self.service.repository.set_review_message(self.row["submission_id"], 77, 555)
        self.audit.reset_mock()
        patch("forms.service.can_review", side_effect=lambda interaction: interaction.user.id != 999).start()

    def reviewer(self, user_id=456, message_id=555):
        response = SimpleNamespace(send_message=AsyncMock())
        return SimpleNamespace(guild_id=1, user=SimpleNamespace(id=user_id), response=response,
                               followup=SimpleNamespace(send=AsyncMock()),
                               message=SimpleNamespace(id=message_id, reply=AsyncMock()))

    def test_only_qs_and_feedback_cards_get_the_button(self):
        from forms.views import ReviewView
        ids = lambda view: [item.custom_id for item in view.children]
        self.assertIn("opsbot:forms:review:dm", ids(ReviewView(None, "question", disabled=True)))
        self.assertIn("opsbot:forms:review:dm", ids(ReviewView(None)))  # persistent routing after restart
        self.assertNotIn("opsbot:forms:review:dm", ids(ReviewView(None, "when_i_work", "timesheet_adjustment")))

    async def test_sends_the_dm_notes_it_on_the_card_and_audits_it(self):
        interaction = self.reviewer()
        self.assertEqual((await self.service.dm_target(interaction))["submission_id"], self.row["submission_id"])
        await self.service.send_dm(interaction, self.row["submission_id"], "Payroll runs Friday.")
        text = self.employee.send.await_args.args[0]
        self.assertIn(self.row["submission_id"], text)
        self.assertIn("Payroll runs Friday.", text)
        self.assertIn("Payroll runs Friday.", interaction.message.reply.await_args.args[0])
        self.assertEqual(self.audit.call_args.args[2], "FORM_DM_SENT")
        self.assertEqual(self.service.repository.get(self.row["submission_id"])["status"], "Pending")

    async def test_non_admins_and_the_submitter_cannot_send(self):
        for user_id in (999, 123):
            interaction = self.reviewer(user_id=user_id)
            self.assertIsNone(await self.service.dm_target(interaction))
            interaction.response.send_message.assert_awaited_once()
        await self.service.send_dm(self.reviewer(user_id=999), self.row["submission_id"], "hi")
        self.employee.send.assert_not_awaited()

    async def test_closed_dms_are_reported_to_the_reviewer(self):
        import discord
        self.employee.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "closed")
        interaction = self.reviewer()
        await self.service.send_dm(interaction, self.row["submission_id"], "hi")
        self.assertIn("could not be delivered", interaction.followup.send.await_args.args[0])
        self.assertEqual(self.audit.call_args.args[2], "FORM_DM_FAILED")
        interaction.message.reply.assert_not_awaited()


class SheetHeaderTests(unittest.TestCase):
    def worksheet_for(self, headers):
        from integrations.forms_sheets import FormsSheet
        worksheet = SimpleNamespace(row_values=lambda row: list(headers), updates=[])
        worksheet.update = lambda range_name, values: worksheet.updates.append(values)
        spreadsheet = SimpleNamespace(worksheet=lambda tab: worksheet)
        sheet = FormsSheet(None)
        sheet._spreadsheet = lambda: spreadsheet
        return sheet, worksheet

    def test_tabs_with_the_old_employee_headers_are_relabeled_in_place(self):
        tab = "Questions Concerns Feedback"
        old = [{"Agent Email": "Employee Email", "Agent Name": "Employee Name"}.get(h, h) for h in HEADERS[tab]]
        self.assertNotEqual(old, HEADERS[tab])
        sheet, worksheet = self.worksheet_for(old)
        sheet._worksheet(tab)
        self.assertEqual(worksheet.updates, [[HEADERS[tab]]])

    def test_other_header_mismatches_still_stop_the_sync(self):
        sheet, worksheet = self.worksheet_for(["Something else"])
        with self.assertRaisesRegex(RuntimeError, "required headers"):
            sheet._worksheet("Questions Concerns Feedback")
        self.assertEqual(worksheet.updates, [])

    def test_disputes_and_timesheets_show_the_concierge_email_with_work_email_fallback(self):
        base = {
            "submission_id": "X-1", "submitted_at_utc": "2026-11-02T12:00:00+00:00",
            "submitted_at_et": "2026-11-02T08:00:00-04:00", "discord_user_id": 7,
            "employee_email": "work@example.com", "employee_name": "A B", "message": "m", "status": "Pending",
        }
        dispute = dict(base, form_type="dispute", form_subtype=None, reference_message_id="42",
                       concierge_email="concierge@example.com")
        self.assertIn("concierge@example.com", sheet_values(dispute))
        self.assertNotIn("work@example.com", sheet_values(dispute))
        timesheet = dict(base, form_type="when_i_work", form_subtype="timesheet_adjustment", concierge_email=None)
        self.assertEqual(sheet_values(timesheet)[HEADERS["WIW Timesheet Adjustments"].index("Concierge Email")], "work@example.com")
        question = dict(base, form_type="question", form_subtype=None, category="Pay",
                        concierge_email="concierge@example.com")
        self.assertEqual(sheet_values(question)[4], "work@example.com")

    def test_disputes_resync_finds_the_existing_row_by_submission_id_column(self):
        from integrations.forms_sheets import FormsSheet
        tab = "Disputes"
        id_column = HEADERS[tab].index("Submission ID")
        ids = [""] * 3
        ids[2] = "D-EXISTING"
        worksheet = SimpleNamespace(
            row_values=lambda row: list(HEADERS[tab]),
            col_values=lambda column: ids if column == id_column + 1 else [],
            updates=[], appends=[],
        )
        worksheet.update = lambda range_name, values, value_input_option: worksheet.updates.append((range_name, values))
        worksheet.append_row = lambda values, value_input_option: worksheet.appends.append(values)
        sheet = FormsSheet(None)
        sheet._worksheet = lambda tab_name: worksheet
        row = {
            "submission_id": "D-EXISTING", "form_type": "dispute", "form_subtype": None,
            "submitted_at_utc": "2026-11-02T12:00:00+00:00", "submitted_at_et": "2026-11-02T08:00:00-04:00",
            "discord_user_id": 7, "employee_email": "a@example.com", "employee_name": "A B",
            "reference_message_id": "42", "message": "Wrong", "status": "Pending",
        }
        sheet.sync_submission(row)
        self.assertEqual(worksheet.appends, [])
        self.assertEqual(worksheet.updates[0][0], "A3:I3")
        self.assertEqual(worksheet.updates[0][1][0][4], "D-EXISTING")


class RosterNameTests(unittest.TestCase):
    def test_full_name_uses_first_and_last_name_columns(self):
        from integrations.google_sheets import full_name
        self.assertEqual(full_name({"First Name": " Jordan ", "Last Name": "Lee"}), "Jordan Lee")
        self.assertEqual(full_name({"Name": "Old Style"}), "Old Style")
        self.assertEqual(full_name({}), "")


class MigrationTests(unittest.TestCase):
    def test_init_adds_the_new_columns_to_an_existing_table(self):
        import sqlite3
        from database.forms_repository import ADDED_COLUMNS, SCHEMA
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forms.db"
            old_schema = SCHEMA
            for column in ADDED_COLUMNS:
                old_schema = old_schema.replace(f"    {column} TEXT,\n", "")
            with sqlite3.connect(path) as con:
                con.executescript(old_schema)
            repository = FormsRepository(path)
            repository.init()
            repository.init()  # idempotent
            row, _ = repository.create(submission_values("migrated") | {"reference_message_id": "7"})
            self.assertEqual(row["reference_message_id"], "7")


class ConnectionTests(unittest.TestCase):
    def test_with_blocks_close_their_connection(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            repository = FormsRepository(Path(directory) / "forms.db")
            repository.init()
            with repository.connect() as con:
                con.execute("SELECT 1")
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")  # closed
            row, _ = repository.create(submission_values("closing"))
            self.assertEqual(repository.get(row["submission_id"])["interaction_id"], "closing")


class Screen:
    """A fake ephemeral form message: edits replace its content and view; modals are recorded."""

    def __init__(self):
        self.content = self.view = self.modal = None

    async def edit_message(self, **kwargs):
        self.content, self.view = kwargs["content"], kwargs["view"]

    async def send_modal(self, modal):
        self.modal = modal


def back_button(view):
    return next((item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "Back"), None)


class BackButtonTests(unittest.IsolatedAsyncioTestCase):
    async def walk(self, picks):
        """Pick through the form from the top; return the screen and every step shown, with its prompt."""
        screen = Screen()
        screen.view = TopLevelView(None, 123)
        screen.content = screen.view.prompt
        steps = [(type(screen.view), screen.content)]
        for value in picks:
            await screen.view.choose(SimpleNamespace(response=screen), value)
            steps.append((type(screen.view), screen.content))
        return screen, steps

    async def assert_back_retraces(self, picks, expected):
        screen, steps = await self.walk(picks)
        self.assertEqual([step for step, _ in steps], expected)
        self.assertIsNone(back_button(TopLevelView(None, 123)))
        for previous_type, previous_prompt in reversed(steps[:-1]):
            button = back_button(screen.view)
            self.assertIsNotNone(button, f"{type(screen.view).__name__} has no Back button")
            await button.callback(SimpleNamespace(response=screen))
            self.assertIsInstance(screen.view, previous_type)
            self.assertEqual(screen.content, previous_prompt)
        self.assertIsInstance(screen.view, TopLevelView)

    async def test_working_location_opens_a_modal_with_three_required_fields(self):
        screen, _ = await self.walk(["working_location"])
        self.assertIsInstance(screen.modal, MessageModal)
        self.assertEqual([(item.label, item.required) for item in screen.modal.children],
                         [("Start Date (MM/DD/YYYY)", True), ("End Date (MM/DD/YYYY)", True), ("New Location", True)])

    async def test_dispute_opens_a_modal_with_both_required_fields(self):
        screen, _ = await self.walk(["dispute"])
        modal = screen.modal
        self.assertIsInstance(modal, MessageModal)
        self.assertEqual([(item.label, item.required) for item in modal.children],
                         [("Message ID", True), ("Dispute Reasoning", True)])

    async def test_back_from_the_category_returns_to_the_form_types(self):
        await self.assert_back_retraces(["question"], [TopLevelView, CategoryView])

    async def test_a_dismissed_modal_can_be_reopened_after_going_back(self):
        screen, _ = await self.walk(["question", CATEGORIES[0]])
        self.assertIsInstance(screen.modal, MessageModal)
        screen.modal = None
        await back_button(screen.view).callback(SimpleNamespace(response=screen))
        await screen.view.choose(SimpleNamespace(response=screen), "question")
        await screen.view.choose(SimpleNamespace(response=screen), CATEGORIES[0])
        self.assertIsInstance(screen.modal, MessageModal)


class ModalSubmitTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return SimpleNamespace(
            id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def test_success_replaces_the_step_message_so_it_cannot_be_submitted_again(self):
        row = {"submission_id": "Q-20260919-0001", "form_type": "question", "form_subtype": None}
        service = SimpleNamespace(submit=AsyncMock(return_value=row))
        modal = MessageModal(service, FormState(form_type="question", category=CATEGORIES[0]))
        interaction = self.interaction()
        await modal.on_submit(interaction)
        interaction.response.defer.assert_awaited_once_with()
        interaction.edit_original_response.assert_awaited_once_with(content=_confirmation(row), view=None)
        interaction.followup.send.assert_not_awaited()

    async def test_dispute_submit_passes_the_message_id(self):
        row = {"submission_id": "D-20261102-0001", "form_type": "dispute", "form_subtype": None}
        service = SimpleNamespace(submit=AsyncMock(return_value=row))
        modal = MessageModal(service, FormState(form_type="dispute"))
        modal.message_id_input._value = "98765"
        modal.message_input._value = "Not my message"
        await modal.on_submit(self.interaction())
        state, message = service.submit.await_args.args[1:]
        self.assertEqual((state.reference_message_id, message), ("98765", "Not my message"))
        self.assertEqual(_confirmation(row), "Dispute submitted.\nReference: D-20261102-0001")

    async def test_working_location_submit_passes_both_dates(self):
        row = {"submission_id": "WL-20261102-0001", "form_type": "working_location", "form_subtype": None}
        service = SimpleNamespace(submit=AsyncMock(return_value=row))
        modal = MessageModal(service, FormState(form_type="working_location"))
        modal.start_input._value, modal.end_input._value = "11/10/2026", "11/14/2026"
        modal.message_input._value = "Denver, CO"
        await modal.on_submit(self.interaction())
        state, message = service.submit.await_args.args[1:]
        self.assertEqual((state.location_start, state.location_end, message), ("11/10/2026", "11/14/2026", "Denver, CO"))

    async def test_a_rejected_choice_asks_the_requester_to_open_the_form_again(self):
        service = SimpleNamespace(submit=AsyncMock(side_effect=ValueError("End time must be after start time.")))
        modal = MessageModal(service, FormState("when_i_work", "timesheet_adjustment"))
        tomorrow = datetime.now(EASTERN).date() + timedelta(days=1)
        modal.shift_date_input, modal.start_time_input, modal.end_time_input = (
            Text(f"{tomorrow:%m/%d/%Y}"), Text("09:00 AM"), Text("08:00 AM"),
        )
        interaction = self.interaction()
        await modal.on_submit(interaction)
        interaction.edit_original_response.assert_not_awaited()
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("End time must be after start time.", message)
        self.assertIn("Open the form again", message)


class WorkingLocationDateTests(unittest.TestCase):
    def dates(self, start, end):
        from forms.domain import working_location_dates
        return working_location_dates(FormState("working_location", location_start=start, location_end=end))

    def test_accepts_mm_dd_yyyy_and_normalizes_it(self):
        self.assertEqual(self.dates(" 11/10/2026 ", "11/14/2026"), ("11/10/2026", "11/14/2026"))
        self.assertEqual(self.dates("11/14/2026", "11/14/2026"), ("11/14/2026", "11/14/2026"))

    def test_accepts_single_digit_month_and_day(self):
        self.assertEqual(self.dates("1/5/2026", "1/9/2026"), ("01/05/2026", "01/09/2026"))

    def test_rejects_other_formats_and_impossible_dates(self):
        for bad in ["2026-11-10", "11/10/26", "Nov 10", "13/01/2026", "02/30/2026"]:
            with self.assertRaisesRegex(ValueError, "Start date"):
                self.dates(bad, "11/14/2026")

    def test_end_date_cannot_be_before_start_date(self):
        with self.assertRaisesRegex(ValueError, "cannot be before"):
            self.dates("11/14/2026", "11/10/2026")


class TimesheetActionTests(unittest.TestCase):
    def test_timesheets_have_only_approve_and_reject(self):
        from forms.domain import allowed_actions, status_for_action
        self.assertEqual(allowed_actions("when_i_work", "timesheet_adjustment"), ("approve", "reject"))
        self.assertEqual(status_for_action("when_i_work", "timesheet_adjustment", "reject"), "Rejected")
        with self.assertRaisesRegex(ValueError, "not valid"):
            status_for_action("when_i_work", "timesheet_adjustment", "need_info")

    def test_emergency_shift_releases_have_no_review_actions(self):
        from forms.domain import allowed_actions
        self.assertEqual(allowed_actions("when_i_work", "emergency_shift_release"), ())


if __name__ == "__main__":
    unittest.main()
