import os
import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

class AccessSheet:
    def __init__(self):
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=SCOPES
        )
        self.client = gspread.authorize(creds)

    def _worksheet(self):
        return self.client.open_by_key(os.environ["GOOGLE_SHEET_ID"]).worksheet(
            os.getenv("GOOGLE_SHEET_TAB", "Discord Access")
        )

    def all_records(self):
        return self._worksheet().get_all_records()

    def get_by_email(self, email):
        email = email.strip().lower()
        for row in self._worksheet().get_all_records():
            if str(row.get("Email", "")).strip().lower() == email:
                return row
        return None

    def mark_verified(self, email):
        """Set the Verified column to TRUE on the row for this email."""
        return self.mark_verified_many([email]) == 1

    def mark_verified_many(self, emails, value="TRUE"):
        """Set the Verified column to TRUE (or FALSE) for these emails in one batch; returns how many rows matched."""
        worksheet = self._worksheet()
        values = worksheet.get_all_values()
        headers = [str(h).strip() for h in values[0]]
        email_column = headers.index("Email")
        verified_column = headers.index("Verified")
        wanted = {email.strip().lower() for email in emails}
        updates = [
            {"range": rowcol_to_a1(row_number, verified_column + 1), "values": [[value]]}
            for row_number, row in enumerate(values[1:], start=2)
            if len(row) > email_column and row[email_column].strip().lower() in wanted
        ]
        if updates:
            worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        return len(updates)

def field(record, name):
    """A roster value by header, ignoring capitalization and spacing ("Team Lead email" == "Team Lead Email")."""
    wanted = name.replace(" ", "").lower()
    for key, value in (record or {}).items():
        if str(key).replace(" ", "").lower() == wanted:
            return str(value).strip()
    return ""

def first_name(record):
    return field(record, "First Name")

def full_name(record):
    """Roster name from First Name + Last Name; older rosters had a single Name column."""
    record = record or {}
    parts = [first_name(record), field(record, "Last Name")]
    return " ".join(p for p in parts if p) or field(record, "Name")

def truthy(value):
    return str(value).strip().lower() in {"true","1","yes","y","x"}
