"""
The Woods — QBO OAuth Authorization (Manual Flow)
=================================================
Run ONCE to authorize the shared Intuit developer app against THE WOODS' QBO
company (its own realm — separate from Bridge). Manual copy-paste flow, no
local server.

Improvement over the Bridge version: the realm/company ID is parsed from the
redirect URL's `realmId` parameter automatically — no QBO_COMPANY_ID env var
needed. IMPORTANT: when the Intuit consent screen asks which company to
connect, pick THE WOODS, not The Bridge.

Usage:
    python3 qbo_auth.py
"""

import os
import json
import base64
import requests
from urllib.parse import urlencode, urlparse, parse_qs

# ── .env autoload ──────────────────────────────────────────────────────────
def _load_env_file(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[len("export "):]
            k, _, v = s.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_env_file()

CLIENT_ID     = os.environ.get("QBO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
REDIRECT_URI  = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
TOKEN_FILE    = "qbo_tokens.json"

SCOPE     = "com.intuit.quickbooks.accounting"
AUTH_URL  = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


def exchange_code_for_tokens(auth_code):
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Accept":        "application/json",
        "Authorization": f"Basic {encoded}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type":   "authorization_code",
        "code":         auth_code,
        "redirect_uri": REDIRECT_URI,
    }
    response = requests.post(TOKEN_URL, headers=headers, data=data)
    if response.status_code == 200:
        return response.json()
    print(f"\n✗ Token exchange failed: {response.status_code}")
    print(response.text)
    return None


def save_tokens(token_data, company_id):
    tokens = {
        "access_token":  token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "company_id":    company_id,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"\n  ✓ Tokens saved to {TOKEN_FILE} (realm {company_id})")
    print("  Keep this file in your woods-finance folder. Never commit it.")
    print("  The first connector run migrates them into Supabase qbo_tokens.")


def main():
    print("=" * 60)
    print("The Woods — QBO OAuth Authorization")
    print("=" * 60)

    if not CLIENT_ID or not CLIENT_SECRET:
        print("\n⚠  Missing QBO_CLIENT_ID / QBO_CLIENT_SECRET (set them in .env).")
        return

    params = {
        "client_id":     CLIENT_ID,
        "response_type": "code",
        "scope":         SCOPE,
        "redirect_uri":  REDIRECT_URI,
        "state":         "woods_finance",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print("\n" + "─" * 60)
    print("STEP 1 — Open this URL in your browser:")
    print("─" * 60)
    print(f"\n{auth_url}\n")
    print("─" * 60)
    print("\nSTEP 2 — Log in to QBO and, when asked which company to connect,")
    print("  select THE WOODS (not The Bridge). Click Authorize/Connect.")
    print("\nSTEP 3 — The browser redirects to a page that may look blank or")
    print("  show an error. That's fine. Copy the ENTIRE URL from the address")
    print("  bar — it contains ?code=...&realmId=...")
    print("\nSTEP 4 — Paste it below.")
    print("─" * 60)

    redirected_url = input("\nPaste the full redirect URL here: ").strip()

    auth_code, realm_id = None, None
    try:
        parsed = urlparse(redirected_url)
        q = parse_qs(parsed.query)
        auth_code = q.get("code", [None])[0]
        realm_id  = q.get("realmId", [None])[0]
    except Exception:
        pass

    if not auth_code:
        print("\n✗ Could not find authorization code in the URL.")
        print("  Make sure you copied the full URL from your browser.")
        return
    if not realm_id:
        realm_id = input("No realmId in URL — paste The Woods' company ID: ").strip()

    print("\n  ✓ Authorization code found.")
    print("Exchanging code for access tokens...")
    token_data = exchange_code_for_tokens(auth_code)
    if not token_data:
        return

    save_tokens(token_data, realm_id)
    print("\n✓ Authorization complete.")
    print("  You can now run: python3 qbo_gl_accounts_connector.py")


if __name__ == "__main__":
    main()
