"""Optional ONE-TIME desktop sign-in for personal Google Drive accounts.
Run on your own computer, where a browser can open. Never commit the resulting token.
"""
from pathlib import Path
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from app.config import ROOT
from app.google_client import SCOPES


def main():
    client_file=ROOT/'credentials/oauth-client.json'
    token_file=ROOT/'credentials/google-user.json'
    if not client_file.is_file():
        raise SystemExit('Save your Google Desktop OAuth client JSON as credentials/oauth-client.json first. See README.')
    if token_file.exists():
        raise SystemExit('credentials/google-user.json already exists. Move it aside before authorizing again.')
    flow=InstalledAppFlow.from_client_secrets_file(str(client_file),SCOPES)
    credentials=flow.run_local_server(port=0,access_type='offline',prompt='consent')
    descriptor=os.open(token_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as target:
        target.write(credentials.to_json())
    print('Sign-in saved. Set google.credentials_file to credentials/google-user.json in config.yaml.')


if __name__=='__main__':
    main()
