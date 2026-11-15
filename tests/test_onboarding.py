import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from database import database
from bot.onboarding import DAILY_CODES, DAILY_FAILED_GUESSES, code_request_blocked, guesses_exhausted
from integrations.email_verification import send_verification_email

EMAIL = "agent@example.com"


class VerificationLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = patch.object(database, "DB_PATH", Path(self.tmp.name) / "opsbot.db")
        self.db_path.start()
        database.init_db()

    def tearDown(self):
        self.db_path.stop()
        self.tmp.cleanup()

    def old_audit(self, user_id, email, action, details=""):
        stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with database.connect() as con:
            con.execute(
                "INSERT INTO audit_log(timestamp,discord_user_id,email,action,details) VALUES(?,?,?,?,?)",
                (stamp, user_id, email, action, details),
            )

    def send_code(self, user_id, email, minutes_ago):
        database.save_code(user_id, email, "hash", "2099-01-01T00:00:00+00:00")
        sent = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        with database.connect() as con:
            con.execute("UPDATE verification_codes SET last_sent_at=? WHERE discord_user_id=?", (sent, user_id))
        database.audit(user_id, email, "VERIFICATION_SENT")

    def test_first_request_is_allowed(self):
        self.assertIsNone(code_request_blocked(1, EMAIL))

    def test_resend_within_cooldown_is_refused(self):
        self.send_code(1, EMAIL, minutes_ago=0)
        self.assertEqual(code_request_blocked(1, EMAIL), "resend_cooldown")
        with patch.dict(os.environ, {"VERIFICATION_RESEND_SECONDS": "0"}):
            self.assertIsNone(code_request_blocked(1, EMAIL))

    def test_daily_code_cap_applies_per_user_and_per_email(self):
        for _ in range(DAILY_CODES):
            self.send_code(1, EMAIL, minutes_ago=5)
        self.assertEqual(code_request_blocked(1, "other@example.com"), "daily_code_limit")
        # A second Discord account cannot reset the count for the same address.
        self.assertEqual(code_request_blocked(2, EMAIL), "daily_code_limit")
        self.assertIsNone(code_request_blocked(2, "other@example.com"))

    def test_failed_guesses_are_capped_across_codes_and_accounts(self):
        for attempt in range(DAILY_FAILED_GUESSES):
            database.audit(attempt % 3, EMAIL, "VERIFICATION_FAILED", "Incorrect code")
        self.assertTrue(guesses_exhausted(99, EMAIL))
        self.assertEqual(code_request_blocked(99, EMAIL), "daily_guess_limit")
        self.assertFalse(guesses_exhausted(99, "other@example.com"))

    def test_unauthorized_email_failures_do_not_count_as_guesses(self):
        for _ in range(DAILY_FAILED_GUESSES):
            database.audit(1, EMAIL, "VERIFICATION_FAILED", "Email not authorized or already linked")
        self.assertFalse(guesses_exhausted(1, EMAIL))

    def test_activity_older_than_a_day_is_ignored(self):
        for _ in range(DAILY_FAILED_GUESSES):
            self.old_audit(1, EMAIL, "VERIFICATION_FAILED", "Incorrect code")
        for _ in range(DAILY_CODES):
            self.old_audit(1, EMAIL, "VERIFICATION_SENT")
        self.assertIsNone(code_request_blocked(1, EMAIL))


class VerificationEmailTest(unittest.TestCase):
    def test_smtp_verifies_the_certificate_and_has_a_timeout(self):
        env = {"SMTP_FROM": "bot@example.com", "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587"}
        with patch.dict(os.environ, env, clear=False), patch("smtplib.SMTP") as smtp_class:
            send_verification_email(EMAIL, "123456")
        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=15)
        smtp = smtp_class.return_value.__enter__.return_value
        context = smtp.starttls.call_args.kwargs["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        smtp.send_message.assert_called_once()



class TeamLeadRoleTests(unittest.TestCase):
    def test_team_roles_follow_the_team_lead_email(self):
        from bot import permissions

        with patch.dict(permissions.TEAM_LEADS, {"nadir@example.com": "Nadir"}, clear=True):
            base = {"Verified", "Operations", "Casual"}
            # Without Concierge access, a team lead's email grants no team role or team channels.
            agent = {"Active": "TRUE", "Email": "a@example.com", "Team Lead email": "Nadir@Example.com", "QA": "TRUE"}
            self.assertEqual(permissions.desired_roles(agent), base | {"QA"})
            lead = {"Active": "TRUE", "Email": "nadir@example.com", "Team Lead Email": ""}
            self.assertNotIn("Concierge - Nadir", permissions.desired_roles(lead))
            # With Concierge access, the team role follows the team lead email.
            concierge_agent = dict(agent, Concierge="TRUE")
            self.assertEqual(permissions.desired_roles(concierge_agent), base | {"Concierge", "Concierge - Nadir", "QA"})
            concierge_lead = dict(lead, Concierge="TRUE")
            self.assertIn("Concierge - Nadir", permissions.desired_roles(concierge_lead))
            leader = {"Active": "TRUE", "Email": "boss@example.com", "Leadership": "TRUE", "Team Lead Email": ""}
            self.assertEqual(permissions.desired_roles(leader), base | {"Leadership"})
            old_roster = {"Active": "TRUE", "Email": "b@example.com", "Concierge": "TRUE", "Concierge Team": "Josh"}
            self.assertEqual(permissions.desired_roles(old_roster), base | {"Concierge", "Concierge - Josh"})


class ModalAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    """Both modals must defer before slow work, or Discord expires the interaction after 3 seconds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = patch.object(database, "DB_PATH", Path(self.tmp.name) / "opsbot.db")
        self.db_path.start()
        database.init_db()

    def tearDown(self):
        self.db_path.stop()
        self.tmp.cleanup()

    def interaction(self, calls):
        interaction = MagicMock()
        interaction.user.id = 42
        interaction.response.defer = AsyncMock(side_effect=lambda **_: calls.append("defer"))
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock(side_effect=lambda *a, **_: calls.append("followup"))
        return interaction

    async def test_email_modal_defers_before_sheet_lookup_and_email(self):
        from bot import onboarding

        calls = []
        sheet = MagicMock()
        sheet.get_by_email.side_effect = lambda _: calls.append("sheet") or {"Active": "TRUE"}
        modal = onboarding.EmailModal(sheet)
        modal.email = EMAIL
        interaction = self.interaction(calls)
        with patch.object(onboarding, "_allowed", return_value=True), \
                patch.object(onboarding, "send_verification_email", side_effect=lambda *_: calls.append("smtp")):
            await modal.on_submit(interaction)

        self.assertEqual(calls, ["defer", "sheet", "smtp", "followup"])
        interaction.response.send_message.assert_not_called()
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_code_modal_defers_before_sheet_lookup_and_role_sync(self):
        from bot import onboarding

        calls = []
        database.save_code(42, EMAIL, onboarding._hash("123456"), "2099-01-01T00:00:00+00:00")
        sheet = MagicMock()
        sheet.get_by_email.side_effect = lambda _: calls.append("sheet") or {"Active": "TRUE"}
        modal = onboarding.CodeModal(sheet)
        modal.code = "123456"
        interaction = self.interaction(calls)
        sync = AsyncMock(side_effect=lambda *_: calls.append("sync") or ([], []))
        with patch.object(onboarding, "sync_member_roles", sync):
            await modal.on_submit(interaction)

        self.assertEqual(calls, ["defer", "sheet", "sync", "followup"])
        interaction.response.send_message.assert_not_called()
        self.assertIsNotNone(database.get_verified_user(42))

    async def test_a_failed_email_send_is_reported_and_frees_the_resend_cooldown(self):
        from bot import onboarding

        calls = []
        sheet = MagicMock()
        sheet.get_by_email.return_value = {"Active": "TRUE"}
        modal = onboarding.EmailModal(sheet)
        modal.email = EMAIL
        interaction = self.interaction(calls)
        with patch.object(onboarding, "_allowed", return_value=True), \
                patch.object(onboarding, "send_verification_email", side_effect=OSError("smtp down")):
            await modal.on_submit(interaction)

        self.assertIn("couldn't send the email", interaction.followup.send.call_args.args[0])
        self.assertIsNone(database.get_code(42))
        self.assertIsNone(code_request_blocked(42, EMAIL))

    async def test_a_role_failure_after_verifying_still_answers_the_user(self):
        from bot import onboarding

        calls = []
        database.save_code(42, EMAIL, onboarding._hash("123456"), "2099-01-01T00:00:00+00:00")
        sheet = MagicMock()
        sheet.get_by_email.return_value = {"Active": "TRUE"}
        modal = onboarding.CodeModal(sheet)
        modal.code = "123456"
        interaction = self.interaction(calls)
        with patch.object(onboarding, "sync_member_roles", AsyncMock(side_effect=RuntimeError("Forbidden"))):
            await modal.on_submit(interaction)

        self.assertIn("could not be applied yet", interaction.followup.send.call_args.args[0])
        self.assertIsNotNone(database.get_verified_user(42))

    def last_audit(self):
        with database.connect() as con:
            return con.execute("SELECT action, details FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()

    async def test_a_malformed_address_is_audited_and_logged(self):
        from bot import onboarding

        sheet = MagicMock()
        modal = onboarding.EmailModal(sheet)
        modal.email = "not-an-email"
        with self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertEqual(self.last_audit(), ("VERIFICATION_FAILED", "Email format rejected"))
        sheet.get_by_email.assert_not_called()

    async def test_an_address_missing_from_the_roster_is_audited_and_logged(self):
        from bot import onboarding

        sheet = MagicMock()
        sheet.get_by_email.return_value = None
        modal = onboarding.EmailModal(sheet)
        modal.email = EMAIL
        with patch.object(onboarding, "_allowed", return_value=True), self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertEqual(self.last_audit(), ("VERIFICATION_FAILED", "Not in roster or inactive"))

    async def test_an_address_linked_to_another_account_says_so_in_the_audit(self):
        from bot import onboarding

        database.save_verified_user(7, EMAIL)
        sheet = MagicMock()
        sheet.get_by_email.return_value = {"Active": "TRUE"}
        modal = onboarding.EmailModal(sheet)
        modal.email = EMAIL
        with patch.object(onboarding, "_allowed", return_value=True), self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertEqual(self.last_audit(), ("VERIFICATION_FAILED", "Linked to another Discord account"))

    async def test_a_code_with_no_open_request_is_audited_and_logged(self):
        from bot import onboarding

        modal = onboarding.CodeModal(MagicMock())
        modal.code = "123456"
        with self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertEqual(self.last_audit(), ("VERIFICATION_FAILED", "No active request"))

    async def test_an_expired_code_is_audited_and_logged(self):
        from bot import onboarding

        database.save_code(42, EMAIL, onboarding._hash("123456"), "2000-01-01T00:00:00+00:00")
        modal = onboarding.CodeModal(MagicMock())
        modal.code = "123456"
        with self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertEqual(self.last_audit(), ("VERIFICATION_EXPIRED", "Code expired"))
        self.assertIsNone(database.get_code(42))

    async def test_an_agent_dropped_from_the_roster_before_the_code_is_entered_is_audited(self):
        from bot import onboarding

        database.save_code(42, EMAIL, onboarding._hash("123456"), "2099-01-01T00:00:00+00:00")
        sheet = MagicMock()
        sheet.get_by_email.return_value = None
        modal = onboarding.CodeModal(sheet)
        modal.code = "123456"
        with self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertEqual(self.last_audit(), ("VERIFICATION_FAILED", "Not in roster or inactive"))
        self.assertIsNone(database.get_verified_user(42))

    async def test_a_verified_row_with_no_access_cell_to_set_is_logged(self):
        from bot import onboarding

        database.save_code(42, EMAIL, onboarding._hash("123456"), "2099-01-01T00:00:00+00:00")
        sheet = MagicMock()
        sheet.get_by_email.return_value = {"Active": "TRUE"}
        sheet.mark_verified.return_value = False
        modal = onboarding.CodeModal(sheet)
        modal.code = "123456"
        with patch.object(onboarding, "sync_member_roles", AsyncMock(return_value=([], []))), \
                self.assertLogs(onboarding.log, "WARNING"):
            await modal.on_submit(self.interaction([]))
        self.assertIsNotNone(database.get_verified_user(42))

    async def test_the_code_step_offers_a_different_email(self):
        from bot import onboarding

        view = onboarding.CodeView(MagicMock())
        self.assertEqual([item.label for item in view.children], ["Enter Verification Code", "Use a different email"])
        interaction = MagicMock()
        interaction.response.send_modal = AsyncMock()
        await view.children[1].callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.call_args.args[0], onboarding.EmailModal)


class AccessSyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = patch.object(database, "DB_PATH", Path(self.tmp.name) / "opsbot.db")
        self.db_path.start()
        database.init_db()
        for user_id in (1, 2, 3):
            database.save_verified_user(user_id, f"user{user_id}@example.com")

    def tearDown(self):
        self.db_path.stop()
        self.tmp.cleanup()

    def access_sync(self, sheet):
        from bot.sync import AccessSync

        guild = MagicMock()
        guild.get_member.side_effect = lambda user_id: MagicMock(id=user_id)
        bot = MagicMock()
        bot.get_guild.return_value = guild
        return AccessSync(bot, sheet, 1, 15)

    async def test_the_roster_is_read_once_per_pass(self):
        sheet = MagicMock()
        sheet.all_records.return_value = [{"Email": "User2@example.com", "Active": "TRUE"}]
        sync = AsyncMock(return_value=([], []))
        with patch("bot.sync.sync_member_roles", sync):
            await self.access_sync(sheet).sync_all()
        sheet.all_records.assert_called_once()
        sheet.get_by_email.assert_not_called()
        records = {call.args[0].id: call.args[1] for call in sync.await_args_list}
        self.assertEqual(records, {1: None, 2: {"Email": "User2@example.com", "Active": "TRUE"}, 3: None})

    async def test_a_failed_roster_read_removes_nobody(self):
        sheet = MagicMock()
        sheet.all_records.side_effect = RuntimeError("APIError 503")
        sync = AsyncMock(return_value=([], []))
        with patch("bot.sync.sync_member_roles", sync):
            await self.access_sync(sheet).sync_all()
        sync.assert_not_awaited()
        self.assertEqual(database.count_audit_since("SYNC_FAILED", "2000-01-01"), 1)



class RemovedMemberTest(unittest.IsolatedAsyncioTestCase):
    """A member who leaves, is kicked, or is banned loses their verification at once, so the email is free again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = patch.object(database, "DB_PATH", Path(self.tmp.name) / "opsbot.db")
        self.db_path.start()
        database.init_db()
        database.save_verified_user(5, "gone@example.com")

    def tearDown(self):
        self.db_path.stop()
        self.tmp.cleanup()

    async def test_a_verified_member_loses_the_link_and_the_sheet_cell_is_cleared(self):
        from bot.sync import forget_removed_member

        sheet = MagicMock()
        sheet.mark_verified_many.return_value = 1
        self.assertTrue(await forget_removed_member(5, sheet))
        self.assertIsNone(database.get_verified_user(5))
        sheet.mark_verified_many.assert_called_once_with(["gone@example.com"], "FALSE")
        self.assertEqual(
            database.count_audit_since("ACCESS_RESET", "2000-01-01", user_id=5, details="Member left or was removed from the server"),
            1,
        )

    async def test_a_member_who_was_never_verified_changes_nothing(self):
        from bot.sync import forget_removed_member

        sheet = MagicMock()
        self.assertFalse(await forget_removed_member(99, sheet))
        sheet.mark_verified_many.assert_not_called()

    async def test_a_failed_sheet_update_is_logged_but_the_link_is_still_removed(self):
        from bot import sync

        sheet = MagicMock()
        sheet.mark_verified_many.side_effect = RuntimeError("APIError 503")
        with self.assertLogs(sync.log, "ERROR"):
            await sync.forget_removed_member(5, sheet)
        self.assertIsNone(database.get_verified_user(5))
        self.assertEqual(database.count_audit_since("ACCESS_RESET_SHEET_FAILED", "2000-01-01"), 1)


if __name__ == "__main__":
    unittest.main()
