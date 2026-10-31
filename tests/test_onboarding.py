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
            agent = {"Active": "TRUE", "Email": "a@example.com", "Team Lead email": "Nadir@Example.com", "QA": "TRUE"}
            self.assertEqual(permissions.desired_roles(agent), {"Verified", "Concierge - Nadir", "QA"})
            lead = {"Active": "TRUE", "Email": "nadir@example.com", "Team Lead Email": ""}
            self.assertIn("Concierge - Nadir", permissions.desired_roles(lead))
            leader = {"Active": "TRUE", "Email": "boss@example.com", "Leadership": "TRUE", "Team Lead Email": ""}
            self.assertEqual(permissions.desired_roles(leader), {"Verified", "Leadership"})
            old_roster = {"Active": "TRUE", "Email": "b@example.com", "Concierge": "TRUE", "Concierge Team": "Josh"}
            self.assertEqual(permissions.desired_roles(old_roster), {"Verified", "Concierge", "Concierge - Josh"})


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


if __name__ == "__main__":
    unittest.main()
