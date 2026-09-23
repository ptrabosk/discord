import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database.forms_repository import FormsRepository
from forms.domain import (
    FormState,
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
from forms.views import AdjustmentTypeView, HourView, MessageModal, MinuteView, _confirmation
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


class FormDateSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timesheet_shows_today_and_previous_fourteen_dates(self):
        class Response:
            async def edit_message(self, **kwargs):
                self.view = kwargs["view"]

        response = Response()
        interaction = SimpleNamespace(response=response)
        view = AdjustmentTypeView(None, 123, FormState("when_i_work", "timesheet_adjustment"))
        await view.choose(interaction, FORM_CONFIG["adjustment_types"][0])
        options = response.view.children[0].options
        today = datetime.now(EASTERN).date()
        self.assertEqual(len(options), 15)
        self.assertEqual(options[0].value, (today - timedelta(days=14)).isoformat())
        self.assertEqual(options[-1].value, today.isoformat())

    async def test_start_time_choices_skip_minutes_for_both_wiw_forms(self):
        class Response:
            async def edit_message(self, **kwargs):
                self.view = kwargs["view"]

            async def send_modal(self, modal):
                self.modal = modal

        expected = ["8am", "10am", "12pm", "2pm", "4pm", "6pm", "8pm", "10pm"]
        self.assertEqual(tuple(range(8, 24, 2)), START_HOURS)
        for subtype in ("timesheet_adjustment", "emergency_shift_release"):
            with self.subTest(subtype=subtype):
                state = FormState("when_i_work", subtype, start_date=date(2026, 9, 19))
                view = HourView(None, 123, state, "start")
                options = view.children[0].options
                self.assertEqual([option.label for option in options], expected)
                self.assertEqual([option.value for option in options], [str(hour) for hour in START_HOURS])
                response = Response()
                await view.choose(SimpleNamespace(response=response), "14")
                self.assertEqual((state.start_hour, state.start_minute), (14, 0))
                if subtype == "timesheet_adjustment":
                    self.assertIsInstance(response.view, HourView)
                    self.assertEqual(response.view.target, "end")
                    self.assertEqual(len(response.view.children[0].options), 24)
                    await response.view.choose(SimpleNamespace(response=response), "16")
                    self.assertIsInstance(response.view, MinuteView)
                else:
                    self.assertIsInstance(response.modal, MessageModal)


if __name__ == "__main__":
    unittest.main()
