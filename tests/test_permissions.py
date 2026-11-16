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



class PlannedRoleChangesTests(unittest.TestCase):
    """What a sync would do to one member, worked out without touching Discord."""

    def holding(self, *roles):
        return SimpleNamespace(roles=[SimpleNamespace(name=r) for r in roles])

    def test_a_member_with_no_roster_row_loses_managed_roles_only(self):
        from bot.permissions import planned_role_changes
        add, remove = planned_role_changes(self.holding("Verified", "QA", "Server Booster"), None)
        self.assertEqual((add, remove), (set(), {"Verified", "QA"}))

    def test_an_active_member_gains_what_is_missing_and_keeps_the_rest(self):
        from bot.permissions import planned_role_changes
        add, remove = planned_role_changes(self.holding("Verified"), ACTIVE | {"QA": "TRUE"})
        self.assertEqual(remove, set())
        self.assertEqual(add, desired_roles(ACTIVE | {"QA": "TRUE"}) - {"Verified"})


if __name__ == "__main__":
    unittest.main()
