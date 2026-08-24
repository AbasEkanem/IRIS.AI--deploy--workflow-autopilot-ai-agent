"""
get_google_refresh_token.py
===========================
Interactive CLI script to generate a long-lived `GOOGLE_REFRESH_TOKEN` for IRIS.AI.

Runs a local web server to handle Google OAuth2 authorization callback, requests offline
access with all required Google Workspace scopes, and outputs the refresh token to copy into `.env`.

Usage:
    python get_google_refresh_token.py
"""

import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("[!] Missing GOOGLE_OAUTH_CLIENT_ID or GOOGLE_OAUTH_CLIENT_SECRET in .env file.")
        print("    Please set both in .env before running this script.")
        return

    # Support Web application client type (and desktop installed)
    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080/", "http://localhost:8080"]
        }
    }

    print("\n--- IRIS.AI Google OAuth2 Refresh Token Generator ---")
    print("Opening browser for Google authorization...\n")

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(host="localhost", port=8080, prompt="consent", access_type="offline")

    print("\n=======================================================")
    print("SUCCESS! Here is your GOOGLE_REFRESH_TOKEN:")
    print("=======================================================\n")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}\n")
    print("Copy the line above into your .env file!")
    print("=======================================================\n")

if __name__ == "__main__":
    main()
