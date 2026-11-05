import unittest

from bot.permissions import desired_roles

ACTIVE = {"Active": "TRUE", "Email": "agent@example.com", "Team Lead Email": ""}


class DesiredRolesTests(unittest.TestCase):
    def test_active_member_gets_base_roles_on_top_of_their_own(self):
        roles = desired_roles(ACTIVE | {"Workflows": "TRUE"})
        self.assertTrue({"Verified", "Operations", "Casual", "Workflows"} <= roles)

    def test_inactive_or_missing_record_gets_nothing(self):
        self.assertEqual(desired_roles(None), set())
        self.assertEqual(desired_roles(ACTIVE | {"Active": "FALSE"}), set())


if __name__ == "__main__":
    unittest.main()
