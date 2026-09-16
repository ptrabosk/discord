import os

import gspread
from google.oauth2.service_account import Credentials

from app_paths import resolve_app_path


SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

class AccessSheet:
    def __init__(self):
        credentials_path = resolve_app_path(
            os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"), "credentials.json"
        )
        if not credentials_path.is_file():
            raise RuntimeError(
                "Google service-account credentials file is missing; "
                "set GOOGLE_SERVICE_ACCOUNT_FILE to a readable file"
            )
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is required in .env")
        creds = Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
        self.client = gspread.authorize(creds)
        self.sheet_id = sheet_id
        self.sheet_tab = os.getenv("GOOGLE_SHEET_TAB", "Discord Access")

    def _worksheet(self):
        return self.client.open_by_key(self.sheet_id).worksheet(self.sheet_tab)

    def check_access(self):
        """Read worksheet metadata without changing the Sheet."""
        return self._worksheet().title

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
