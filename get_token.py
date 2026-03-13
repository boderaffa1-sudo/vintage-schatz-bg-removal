#!/usr/bin/env python3
"""
One-time helper: run locally to get a Google OAuth2 refresh token.

Usage:
  1. Create OAuth2 Desktop credentials in GCP Console
  2. Download the client_secret JSON file
  3. Run:  python get_token.py path/to/client_secret_xxx.json
  4. Browser opens → authorize with your Google account
  5. Copy the printed GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  6. Paste them into Railway → Variables
"""
import sys
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python get_token.py <path-to-client-secret.json>")
        sys.exit(1)

    client_secret_file = sys.argv[1]

    with open(client_secret_file) as f:
        client_config = json.load(f)

    # Extract client_id and client_secret
    key = "installed" if "installed" in client_config else "web"
    client_id = client_config[key]["client_id"]
    client_secret = client_config[key]["client_secret"]

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    creds = flow.run_local_server(port=8090, open_browser=True)

    print("\n" + "=" * 60)
    print("SUCCESS! Copy these into Railway → Variables:")
    print("=" * 60)
    print(f"\nGOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print("\n" + "=" * 60)
    print("You can now close this window.")


if __name__ == "__main__":
    main()
