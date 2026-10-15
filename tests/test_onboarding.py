import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
