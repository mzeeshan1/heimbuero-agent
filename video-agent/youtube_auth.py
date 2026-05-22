"""
Run this ONCE on your Mac to authenticate with YouTube.
It will open a browser, ask you to log in, then save a token.json file.
Upload that token.json content to Railway as YOUTUBE_TOKEN_JSON variable.

Install dependencies first:
pip3 install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
"""

import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

CLIENT_CONFIG = {
    "installed": {
        "client_id":     os.environ.get("YOUTUBE_CLIENT_ID", ""),
        "client_secret": os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        "redirect_uris": ["http://localhost"],
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token"
    }
}

def main():
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
            creds = flow.run_local_server(port=8080)

        with open("token.json", "w") as f:
            f.write(creds.to_json())

    print("\n✅ Authentication successful!")
    print("\nYour token.json content (copy this to Railway as YOUTUBE_TOKEN_JSON):")
    print("=" * 60)
    with open("token.json") as f:
        content = f.read()
        print(content)
    print("=" * 60)
    print("\nAdd this as YOUTUBE_TOKEN_JSON in Railway Variables.")

if __name__ == "__main__":
    main()
