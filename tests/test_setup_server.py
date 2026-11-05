import unittest
from types import SimpleNamespace

from bot.setup_server import run_setup


class Role:
    def __init__(self, name):
        self.name = name


class FakeGuild:
    """Records the overwrites setup gives each channel; every role, category and channel starts missing."""

    def __init__(self):
        self.default_role = Role("@everyone")
        self.roles, self.categories, self.text_channels = [], [], []
        self.overwrites = {}

    async def create_role(self, name, reason):
        role = Role(name)
        self.roles.append(role)
        return role

    async def create_category(self, name, overwrites, reason):
        category = SimpleNamespace(name=name, text_channels=[])
        self.categories.append(category)
        self.overwrites[name] = overwrites
        return category

    async def create_text_channel(self, name, category, overwrites, reason):
        self.overwrites[f"{category.name}/{name}"] = overwrites


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_qa_sees_every_concierge_channel_including_team_channels(self):
        guild = FakeGuild()
        await run_setup(guild)

        def can_view(key, role_name):
            return any(role.name == role_name and ow.view_channel for role, ow in guild.overwrites[key].items())

        for key in ("CONCIERGE", "CONCIERGE/concierge-announcements", "CONCIERGE/team-nadir",
                    "CONCIERGE/team-josh", "CONCIERGE/team-thaniel", "QA/qa-questions"):
            self.assertTrue(can_view(key, "QA"), key)
        self.assertFalse(can_view("CONCIERGE/team-nadir", "Concierge"))
        self.assertFalse(can_view("WHITE GLOVE/wg-questions", "QA"))

    async def test_hand_built_channels_and_read_only_operations(self):
        guild = FakeGuild()
        await run_setup(guild)

        def access(key, role_name):
            ow = next((ow for role, ow in guild.overwrites[key].items() if role.name == role_name), None)
            return ow and (ow.view_channel, ow.send_messages)

        # OPERATIONS is hidden from people without the Operations role, which verified members get.
        self.assertEqual(access("OPERATIONS/announcements", "@everyone"), (False, None))
        self.assertEqual(access("OPERATIONS/announcements", "Operations"), (True, None))
        self.assertEqual(access("OPERATIONS/announcements", "Leadership"), (True, True))
        self.assertEqual(access("OPERATIONS/announcements", "Workflows"), (True, True))
        self.assertEqual(access("WORKFLOWS/main", "Workflows"), (True, True))
        # shift-releases ignores the CONCIERGE category, so QA does not inherit it.
        self.assertEqual(access("CONCIERGE/shift-releases", "Concierge"), (True, None))
        self.assertIsNone(access("CONCIERGE/shift-releases", "QA"))
        self.assertEqual(access("WORKFLOWS/qa-disputes", "QA"), (True, True))
        self.assertEqual(access("WORKFLOWS/qa-disputes", "Leadership"), (True, None))
        self.assertEqual(access("BOT/errors", "@everyone"), (False, None))
        self.assertIsNone(access("BOT/errors", "Moderator"))
        # SETUP is public, but initial-setup is locked to read-only for everyone.
        self.assertEqual(access("SETUP/initial-setup", "@everyone"), (True, False))
        # WORKFLOWS/main is read-only for everyone, but slash commands such as /workflow still work there.
        self.assertEqual(access("WORKFLOWS/main", "@everyone"), (True, False))
        self.assertTrue(next(ow for role, ow in guild.overwrites["WORKFLOWS/main"].items()
                             if role.name == "@everyone").use_application_commands)


class ReviewerTests(unittest.TestCase):
    def test_leadership_and_admins_can_review_but_others_cannot(self):
        from bot.permissions import can_review

        def member(*roles, admin=False):
            return SimpleNamespace(user=SimpleNamespace(
                roles=[SimpleNamespace(name=r) for r in roles],
                guild_permissions=SimpleNamespace(administrator=admin)))

        self.assertTrue(can_review(member("Leadership")))
        self.assertTrue(can_review(member("Ops Bot Admin")))
        self.assertTrue(can_review(member(admin=True)))
        self.assertFalse(can_review(member("QA", "Workflows")))


if __name__ == "__main__":
    unittest.main()
