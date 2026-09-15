import os
import unittest
from unittest.mock import patch

from bot import guild_authorization


class GuildAuthorizationTests(unittest.TestCase):
    def tearDown(self):
        guild_authorization._authorized_guild_id = None

    def test_missing_guild_id_fails_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "required"):
                guild_authorization.load_authorized_guild_id()

    def test_non_numeric_guild_id_fails_validation(self):
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": "not-a-number"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "positive numeric"):
                guild_authorization.load_authorized_guild_id()

    def test_only_configured_guild_is_authorized(self):
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": "123456789"}, clear=True):
            guild_authorization.load_authorized_guild_id()

        guild = type("Guild", (), {"id": 123456789})()
        self.assertTrue(guild_authorization.is_authorized_guild(guild))
        self.assertTrue(guild_authorization.is_authorized_guild(123456789))
        self.assertFalse(guild_authorization.is_authorized_guild(987654321))
        self.assertFalse(guild_authorization.is_authorized_guild(None))


if __name__ == "__main__":
    unittest.main()
