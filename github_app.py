"""GitHub sign-in without manual setup.

The first time the bot owner presses Connect, the bot creates its own GitHub App from a manifest
(https://docs.github.com/apps/sharing-github-apps/registering-a-github-app-from-a-manifest): GitHub shows one
"Create" button and hands the app's keys back to the bot. From then on every user just presses Connect, signs in
on GitHub and picks repositories. GITHUB_CLIENT_ID/GITHUB_CLIENT_SECRET of an OAuth App still work instead.
"""
import html
import json
import time
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import InvalidToken

from integrations import GITHUB_API

TOKEN_MARGIN = 600
TOKEN_URL = "https://github.com/login/oauth/access_token"


class GitHubApps:
    def __init__(self, store, env_id="", env_secret=""):
        self.store, self.env_id, self.env_secret = store, env_id, env_secret

    def creds(self):
        """{client_id, client_secret, slug} of the OAuth/GitHub App, or None if GitHub sign-in is not set up yet."""
        if self.env_id and self.env_secret:
            return {"client_id": self.env_id, "client_secret": self.env_secret, "slug": ""}
        return self.store.get_meta("github_app", secret=True)

    # ----- one-click creation of the bot's GitHub App -----

    def manifest_page(self, public_url, state, bot_name):
        manifest = {
            "name": f"Secret AI {bot_name}"[:34].strip(),
            "url": public_url,
            "description": "Secret AI Telegram assistant: reads and edits your repositories after your confirmation.",
            "redirect_url": public_url + "/setup/github/done",
            "callback_urls": [public_url + "/oauth/github/callback"],
            "setup_url": public_url + "/oauth/github/installed",
            "public": True,
            "request_oauth_on_install": False,
            "hook_attributes": {"url": public_url + "/health", "active": False},
            "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
            "default_events": [],
        }
        action = "https://github.com/settings/apps/new?state=" + quote(state)
        return (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>GitHub</title><body style='background:#111114;color:#fff;font-family:system-ui;display:grid;place-items:center;min-height:90vh'>"
            f"<form id=f action='{html.escape(action)}' method=post>"
            f"<input type=hidden name=manifest value='{html.escape(json.dumps(manifest))}'>"
            "<button style='font:600 17px system-ui;padding:14px 22px;border-radius:24px;border:0;background:#fff;color:#000'>"
            "Продолжить на GitHub →</button></form><script>document.getElementById('f').submit()</script>")

    def finish_setup(self, code):
        """Exchange the manifest code for the new app's keys and keep them (encrypted)."""
        response = httpx.post(f"{GITHUB_API}/app-manifests/{quote(code)}/conversions", timeout=20,
                              headers={"Accept": "application/vnd.github+json", "User-Agent": "secret-ai-bot"})
        if response.status_code not in (200, 201):
            return None
        data = response.json()
        creds = {"client_id": data["client_id"], "client_secret": data["client_secret"], "slug": data.get("slug") or ""}
        self.store.set_meta("github_app", creds, secret=True)
        return creds

    # ----- user sign-in -----

    def authorize_url(self, public_url, state):
        creds = self.creds()
        query = {"client_id": creds["client_id"], "state": state, "redirect_uri": public_url + "/oauth/github/callback",
                 "allow_signup": "true"}
        if not creds["slug"]:
            query["scope"] = "repo"  # classic OAuth App; a GitHub App takes its permissions from the app itself
        return "https://github.com/login/oauth/authorize?" + urlencode(query)

    def install_url(self):
        creds = self.creds()
        return f"https://github.com/apps/{creds['slug']}/installations/new" if creds and creds["slug"] else None

    def exchange(self, code, public_url):
        """Code from the callback -> (token, meta with refresh data) or (None, None)."""
        creds = self.creds()
        try:
            response = httpx.post(TOKEN_URL, headers={"Accept": "application/json"}, timeout=15, data={
                "client_id": creds["client_id"], "client_secret": creds["client_secret"], "code": code,
                "redirect_uri": public_url + "/oauth/github/callback"})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None, None
        return data.get("access_token"), self.refresh_meta(data)

    def has_installation(self, token):
        """Whether a GitHub App user token can see any installation (i.e. the user picked repositories)."""
        if not self.creds() or not self.creds()["slug"]:
            return True
        try:
            response = httpx.get(f"{GITHUB_API}/user/installations", timeout=15, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "secret-ai-bot"})
            return response.status_code == 200 and response.json().get("total_count", 0) > 0
        except (httpx.HTTPError, ValueError):
            return True

    async def fresh_token(self, http, user_id):
        """Refresh an expiring GitHub App user token before use; tokens of classic OAuth Apps never expire."""
        saved = self.store.get(user_id, "github")
        if not saved:
            return
        meta = saved[0]["meta"]
        if not meta.get("refresh_token") or time.time() < meta.get("expires_at", 0) - TOKEN_MARGIN:
            return
        creds = self.creds()
        try:
            refresh = self.store.fernet.decrypt(meta["refresh_token"].encode()).decode()
        except (InvalidToken, ValueError):
            return
        if not creds:
            return
        try:
            response = await http.post(TOKEN_URL, headers={"Accept": "application/json"}, timeout=15, data={
                "client_id": creds["client_id"], "client_secret": creds["client_secret"],
                "grant_type": "refresh_token", "refresh_token": refresh})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return
        if data.get("access_token"):
            self.store.save(user_id, "github", "github", data["access_token"], meta={**meta, **self.refresh_meta(data)})

    def refresh_meta(self, data):
        """Expiry data of a GitHub App user token; the refresh token is kept encrypted."""
        if not data.get("refresh_token"):
            return {}
        return {"refresh_token": self.store.fernet.encrypt(data["refresh_token"].encode()).decode(),
                "expires_at": time.time() + int(data.get("expires_in") or 28800)}
