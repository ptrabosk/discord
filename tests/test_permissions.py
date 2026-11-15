import unittest

from types import SimpleNamespace

from bot.permissions import desired_roles, has_workflow_access

ACTIVE = {"Active": "TRUE", "Email": "agent@example.com", "Team Lead Email": ""}


def member(*roles, admin=False):
    return SimpleNamespace(user=SimpleNamespace(
        roles=[SimpleNamespace(name=r) for r in roles],
        guild_permissions=SimpleNamespace(administrator=admin)))


class WorkflowAccessTests(unittest.TestCase):
    def test_workflows_members_and_admins_can_poll_reachouts(self):
        self.assertTrue(has_workflow_access(member("Workflows")))
        self.assertTrue(has_workflow_access(member("Ops Bot Admin")))
        self.assertTrue(has_workflow_access(member(admin=True)))

    def test_other_roles_cannot_poll_reachouts(self):
        self.assertFalse(has_workflow_access(member("QA", "Leadership", "Concierge")))


class DesiredRolesTests(unittest.TestCase):
    def test_active_member_gets_base_roles_on_top_of_their_own(self):
        roles = desired_roles(ACTIVE | {"Workflows": "TRUE"})
        self.assertTrue({"Verified", "Operations", "Casual", "Workflows"} <= roles)

    def test_inactive_or_missing_record_gets_nothing(self):
        self.assertEqual(desired_roles(None), set())
        self.assertEqual(desired_roles(ACTIVE | {"Active": "FALSE"}), set())


if __name__ == "__main__":
    unittest.main()
