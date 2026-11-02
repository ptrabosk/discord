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


if __name__ == "__main__":
    unittest.main()
