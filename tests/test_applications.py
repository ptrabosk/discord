import json
import unittest
from pathlib import Path

from bot.applications import parse_hours

ROOT = Path(__file__).resolve().parents[1]


class ParseHoursTests(unittest.TestCase):
    def test_accepts_whole_numbers_in_range(self):
        self.assertEqual(parse_hours("1"), 1)
        self.assertEqual(parse_hours(" 40 "), 40)
        self.assertEqual(parse_hours("168"), 168)

    def test_rejects_non_numbers_and_out_of_range(self):
        for value in ["", "0", "169", "1000", "40.5", "forty", "-5", "+5", "40 hours", "1e2"]:
            with self.subTest(value=value):
                self.assertIsNone(parse_hours(value))


class ApplicationChannelConfigTests(unittest.TestCase):
    def test_wg_apps_is_private_to_workflows_and_admins_under_workflows(self):
        structure = json.loads((ROOT / "config" / "server_structure.json").read_text())
        workflows = next(c for c in structure["categories"] if c["name"] == "WORKFLOWS")
        channel = next(c for c in workflows["channels"] if c["name"] == "wg-apps")
        self.assertTrue(channel["private"])
        self.assertEqual(set(channel["roles"]), {"Workflows", "Ops Bot Admin"})
        self.assertNotIn("view_roles", channel)


if __name__ == "__main__":
    unittest.main()
