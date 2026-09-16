import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app_paths import APP_ROOT
from bot.commands import register_commands
from bot.guild_authorization import load_authorized_guild_id
from bot.health import (
    check_google_sheet,
    format_timestamp,
    format_uptime,
    get_bot_version,
    smtp_configuration_status,
)
from bot.instance_lock import InstanceLock
from database import database


class ProductionRuntimeTests(unittest.TestCase):
    def test_database_path_can_be_relocated_without_changing_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "persistent" / "opsbot.db"
            with patch.object(database, "DB_PATH", database_path):
                database.init_db()
                database.save_verified_user(42, "person@example.test")
                database.init_db()
                saved_user = database.get_verified_user(42)
                ok, detail = database.check_database()

            self.assertTrue(ok, detail)
            self.assertTrue(database_path.is_file())
            self.assertEqual(saved_user[1], "person@example.test")

    def test_database_health_rejects_a_non_database_file(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "opsbot.db"
            database_path.write_bytes(b"not a sqlite database")

            with patch.object(database, "DB_PATH", database_path):
                ok, detail = database.check_database()

            self.assertFalse(ok)
            self.assertEqual(detail, "DatabaseError")

    def test_instance_lock_rejects_a_second_local_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "opsbot.db.lock"
            first = InstanceLock(lock_path)
            second = InstanceLock(lock_path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "Another bot instance"):
                    second.acquire()
            finally:
                first.release()

    def test_smtp_health_only_checks_configuration(self):
        configured = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_FROM": "bot@example.test",
            "SMTP_USERNAME": "bot",
            "SMTP_PASSWORD": "not-logged-or-used",
        }
        with patch.dict("os.environ", configured, clear=True):
            self.assertEqual(
                smtp_configuration_status(),
                (True, "Configured (not connection-tested)"),
            )

    def test_health_time_formatting(self):
        started_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
        now = started_at + timedelta(days=1, hours=2, minutes=3, seconds=4)
        with patch("bot.health.utc_now", return_value=now):
            self.assertEqual(format_uptime(started_at), "1d 2h 3m 4s")
        self.assertEqual(
            format_timestamp(started_at),
            "2026-09-15 00:00:00 UTC",
        )

    def test_version_fallback_allows_the_root_owned_app_directory(self):
        completed = SimpleNamespace(stdout="abc123\n")
        with (
            patch.dict("os.environ", {"BOT_VERSION": ""}, clear=True),
            patch("bot.health.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(get_bot_version(), "abc123")

        run.assert_called_once_with(
            [
                "git",
                "-c",
                f"safe.directory={APP_ROOT}",
                "rev-parse",
                "--short",
                "HEAD",
            ],
            cwd=APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )


class GoogleHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_health_is_read_only_and_reports_success(self):
        class Sheet:
            def check_access(self):
                return "Discord Access"

        self.assertEqual(await check_google_sheet(Sheet()), (True, "OK"))

    async def test_google_health_redacts_exception_details(self):
        class Sheet:
            def check_access(self):
                raise RuntimeError("sensitive provider response")

        self.assertEqual(
            await check_google_sheet(Sheet()),
            (False, "RuntimeError"),
        )


class HealthCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _register_health_command(*, is_admin=True):
        commands = {}

        class Tree:
            def command(self, name, description):
                def decorator(function):
                    commands[name] = function
                    return function

                return decorator

        sync_loop = Mock()
        sync_loop.is_running.return_value = True
        sync_loop.failed.return_value = False
        bot = SimpleNamespace(
            tree=Tree(),
            started_at=datetime.now(timezone.utc),
            version="test-version",
            access_sync=SimpleNamespace(
                loop=sync_loop,
                last_successful_sync=None,
                last_failure=None,
            ),
            get_guild=Mock(return_value=object()),
        )
        interaction = Mock()
        interaction.guild_id = 123456789
        interaction.user.roles = (
            [SimpleNamespace(name="Ops Bot Admin")] if is_admin else []
        )
        interaction.user.guild_permissions.administrator = False
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        with patch.dict("os.environ", {"DISCORD_GUILD_ID": "123456789"}):
            load_authorized_guild_id()
        register_commands(bot, Mock())
        return commands["health"], interaction

    async def test_health_is_ephemeral_and_does_not_send_email(self):
        health, interaction = self._register_health_command()

        with (
            patch("bot.commands.check_database", return_value=(True, "OK")),
            patch("bot.commands.check_google_sheet", AsyncMock(return_value=(True, "OK"))),
            patch(
                "bot.commands.smtp_configuration_status",
                return_value=(True, "Configured (not connection-tested)"),
            ),
        ):
            await health(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        response = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("Bot status: Online", response)
        self.assertIn("Google Sheets: OK", response)
        self.assertIn("Background sync task: Running", response)

    async def test_health_rejects_non_admins_ephemerally(self):
        health, interaction = self._register_health_command(is_admin=False)

        await health(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "Not authorized.", ephemeral=True
        )
        interaction.response.defer.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
