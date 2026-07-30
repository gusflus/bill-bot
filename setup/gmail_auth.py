"""Local OAuth for the setup wizard only.

This is separate from the bot itself: Main.gs uses Apps Script's built-in
GmailApp, which needs no external OAuth setup. This module is just for the
wizard running on your machine to read sample emails while you're picking a
regex for a new sender.

Requires a Google Cloud OAuth client (Desktop app type) downloaded as
credentials.json in the repo root - see README.md.
"""
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_PATH = "credentials.json"
TOKEN_PATH = ".setup-token.json"


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise SystemExit(
                    f"Missing {CREDENTIALS_PATH}. Download an OAuth client (Desktop app) "
                    "from Google Cloud Console and save it there - see README.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)
