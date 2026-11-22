import asyncio
import unittest
from unittest.mock import MagicMock, patch

from bot.sync import AccessSync


class SyncAllSummaryTests(unittest.TestCase):
    def make_sync(self, records):
        bot = MagicMock()
        bot.get_guild.return_value = object()
        sheet = MagicMock()
        # sync.py calls sheet.all_records(), so the rows are its return value.
        sheet.all_records.return_value = records
        return AccessSync(bot, sheet, 1, 15)

    def test_an_empty_roster_is_skipped_and_says_so(self):
        sync = self.make_sync([])
        with patch("bot.sync.audit"):
            result = asyncio.run(sync.sync_all())
        self.assertEqual(result, "Skipped: the roster has no rows with an Email value.")

    def test_an_unreadable_roster_is_skipped_and_says_so(self):
        sync = self.make_sync([])
        sync.sheet.all_records.side_effect = RuntimeError("sheet unavailable")
        with patch("bot.sync.audit"):
            result = asyncio.run(sync.sync_all())
        self.assertEqual(result, "Skipped: the roster could not be read. Details are in the audit log.")

    def test_a_missing_guild_is_skipped_and_says_so(self):
        sync = self.make_sync([{"Email": "a@example.com"}])
        sync.bot.get_guild.return_value = None
        result = asyncio.run(sync.sync_all())
        self.assertEqual(result, "Skipped: the bot is not connected to the server yet.")


if __name__ == "__main__":
    unittest.main()
