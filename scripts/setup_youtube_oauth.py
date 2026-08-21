"""
One-Time YouTube OAuth Setup Helper Script.
Run this script locally to generate your YOUTUBE_REFRESH_TOKEN for GitHub Secrets.

Usage:
1. Create a project in Google Cloud Console with YouTube Data API v3 enabled.
2. Create OAuth 2.0 Client ID (Desktop Application).
3. Download client secrets JSON as `client_secret.json` in this project directory.
4. Run: `python scripts/setup_youtube_oauth.py`
"""

import sys
import json
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow
import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def main():
    client_secrets_file = config.BASE_DIR / "client_secret.json"
    
    if not client_secrets_file.exists():
        print(f"Error: Could not find '{client_secrets_file}'.")
        print("Please download your OAuth Desktop Client Secret file from Google Cloud Console and save it as client_secret.json in the project root directory.")
        sys.exit(1)

    print("Starting YouTube OAuth Authorization Flow...")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    creds = flow.run_local_server(port=8080)

    print("\n" + "=" * 70)
    print("YOUTUBE OAUTH SETUP COMPLETED SUCCESSFULLY!")
    print("=" * 70)
    print("Copy the following values into your GitHub Secrets (.env for local test):")
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 70 + "\n")

    token_data = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri
    }
    
    token_file = config.BASE_DIR / "token.json"
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)
    print(f"Saved OAuth tokens to {token_file}")

if __name__ == "__main__":
    main()
