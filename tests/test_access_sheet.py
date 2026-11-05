import unittest

from integrations.google_sheets import AccessSheet


class FakeWorksheet:
    def __init__(self, values):
        self.values = values
        self.batches = []

    def get_all_values(self):
        return self.values

    def batch_update(self, updates, value_input_option):
        self.batches.append((updates, value_input_option))


def sheet_with(values):
    worksheet = FakeWorksheet(values)
    sheet = AccessSheet.__new__(AccessSheet)
    sheet._worksheet = lambda: worksheet
    return sheet, worksheet


class MarkVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.values = [
            ["Email", "Attentive Email", "Concierge Email", "First Name", "Verified"],
            ["a@example.com", "", "", "A", ""],
            ["B@Example.com ", "", "", "B", "FALSE"],
            ["c@example.com", "", "", "C", ""],
        ]

    def test_marks_one_row_true_matching_case_and_spacing_insensitively(self):
        sheet, worksheet = sheet_with(self.values)
        self.assertTrue(sheet.mark_verified("b@example.com"))
        updates, option = worksheet.batches[0]
        self.assertEqual(updates, [{"range": "E3", "values": [["TRUE"]]}])
        self.assertEqual(option, "USER_ENTERED")

    def test_unknown_email_writes_nothing(self):
        sheet, worksheet = sheet_with(self.values)
        self.assertFalse(sheet.mark_verified("nobody@example.com"))
        self.assertEqual(worksheet.batches, [])

    def test_reset_writes_false_to_the_matching_row(self):
        sheet, worksheet = sheet_with(self.values)
        self.assertEqual(sheet.mark_verified_many(["b@example.com"], value="FALSE"), 1)
        self.assertEqual(worksheet.batches[0][0], [{"range": "E3", "values": [["FALSE"]]}])

    def test_batch_marks_every_listed_email_in_one_request(self):
        sheet, worksheet = sheet_with(self.values)
        self.assertEqual(sheet.mark_verified_many(["a@example.com", "c@example.com", "missing@example.com"]), 2)
        self.assertEqual(len(worksheet.batches), 1)
        self.assertEqual([u["range"] for u in worksheet.batches[0][0]], ["E2", "E4"])


if __name__ == "__main__":
    unittest.main()
