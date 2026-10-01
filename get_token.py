"""Run ONCE on your own computer to get the YouTube refresh token.

1. Put the OAuth 'Desktop app' JSON from Google Cloud next to this file as client_secret.json
2. pip install google-auth-oauthlib
3. python get_token.py  -> sign in with the channel's Google account
4. Copy the three printed values into GitHub repo secrets.
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", scopes=["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
info = json.load(open("client_secret.json"))
info = info.get("installed") or info.get("web")
print("\nYT_CLIENT_ID     =", info["client_id"])
print("YT_CLIENT_SECRET =", info["client_secret"])
print("YT_REFRESH_TOKEN =", creds.refresh_token)
