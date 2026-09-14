import os
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

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

    def get_by_email(self, email):
        email = email.strip().lower()
        for row in self._worksheet().get_all_records():
            if str(row.get("Email", "")).strip().lower() == email:
                return row
        return None

    def all_records(self):
        return self._worksheet().get_all_records()

def truthy(value):
    return str(value).strip().lower() in {"true","1","yes","y","x"}
