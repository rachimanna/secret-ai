"""One-tap app connections through Pipedream Connect: a catalog of thousands of apps with managed OAuth.

The user presses Connect, signs in on Pipedream's hosted page and the account becomes available to the model
as a remote MCP server. Tokens of connected accounts stay with Pipedream; the bot only keeps its project keys.
"""
import re
import threading
import time
from urllib.parse import quote

import httpx

API = "https://api.pipedream.com"
TOKEN_MARGIN = 300
CATALOG_TTL = 6 * 3600
ACCOUNTS_TTL = 30
MAX_APP_TOOLS = 12
SLUG = re.compile(r"^[a-z0-9_]{1,80}$")

# Shown before the catalog loads and when Pipedream is not configured.
FEATURED = [
    ("github", "GitHub", "Код, pull request'ы, issues", "Code, pull requests, issues"),
    ("notion", "Notion", "Заметки, базы и документы", "Notes, databases and docs"),
    ("gmail", "Gmail", "Почта: прочитать, найти, ответить", "Mail: read, search, reply"),
    ("google_calendar", "Google Calendar", "Встречи и напоминания", "Meetings and reminders"),
    ("google_drive", "Google Drive", "Файлы и папки", "Files and folders"),
    ("google_sheets", "Google Sheets", "Таблицы и отчёты", "Spreadsheets and reports"),
    ("slack", "Slack", "Каналы и сообщения команды", "Team channels and messages"),
    ("linear_app", "Linear", "Трекер задач для продуктовых команд", "Issue tracking for product teams"),
    ("jira", "Jira", "Проекты и agile-процессы", "Projects and agile workflows"),
    ("gitlab", "GitLab", "Код, CI/CD и DevOps", "Code, CI/CD and DevOps"),
    ("trello", "Trello", "Доски и карточки", "Boards and cards"),
    ("airtable_oauth", "Airtable", "Базы данных без кода", "No-code databases"),
    ("discord", "Discord", "Серверы и сообщения", "Servers and messages"),
    ("dropbox", "Dropbox", "Облачное хранилище", "Cloud storage"),
    ("figma", "Figma", "Дизайн и макеты", "Design and mockups"),
    ("hubspot", "HubSpot", "CRM и маркетинг", "CRM and marketing"),
    ("stripe", "Stripe", "Платежи и подписки", "Payments and subscriptions"),
    ("spotify", "Spotify", "Музыка и плейлисты", "Music and playlists"),
    ("todoist", "Todoist", "Список дел", "To-do lists"),
    ("youtube_data_api", "YouTube", "Видео и каналы", "Videos and channels"),
]
ICONS = {"linear_app": "linear", "airtable_oauth": "airtable", "youtube_data_api": "youtube",
         "google_calendar": "googlecalendar", "google_drive": "googledrive", "google_sheets": "googlesheets"}


def featured(language):
    ru = language == "ru"
    return [{"slug": slug, "name": name, "description": d_ru if ru else d_en,
             "img": f"https://cdn.simpleicons.org/{ICONS.get(slug, slug)}"} for slug, name, d_ru, d_en in FEATURED]


def server_label(slug):
    label = re.sub(r"[^A-Za-z0-9_]", "_", slug)[:32]
    return label if label[:1].isalpha() else "app_" + label[:28]


class PipedreamError(Exception):
    pass


class Pipedream:
    """Thread-safe client used from both the HTTP thread and (via asyncio.to_thread) the bot loop."""

    def __init__(self, client_id, client_secret, project_id, environment="production", mcp_url=""):
        self.client_id, self.client_secret, self.project_id = client_id, client_secret, project_id
        self.environment = environment or "production"
        self.mcp_url = mcp_url or "https://remote.mcp.pipedream.net/v3"
        self.http = httpx.Client(base_url=API, timeout=20)
        self.lock = threading.Lock()
        self._token, self._expires = None, 0.0
        self._catalog, self._accounts = {}, {}

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret and self.project_id)

    def access_token(self):
        with self.lock:
            if self._token and time.time() < self._expires - TOKEN_MARGIN:
                return self._token
        response = self.http.post("/v1/oauth/token", json={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
        if response.status_code != 200:
            raise PipedreamError(f"oauth {response.status_code}")
        data = response.json()
        with self.lock:
            self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in") or 3600)
            return self._token

    def _call(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self.access_token()}", "x-pd-environment": self.environment}
        response = self.http.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise PipedreamError(f"{method} {path.split('?')[0]} {response.status_code}")
        return response.json() if response.content else None

    # ----- catalog -----

    def catalog(self, query="", after=""):
        """One page of apps sorted by popularity: (apps, next cursor, total count)."""
        key = (query.strip().lower(), after)
        with self.lock:
            cached = self._catalog.get(key)
            if cached and time.time() - cached[0] < CATALOG_TTL:
                return cached[1]
        params = {"limit": 40, "sort_key": "featured_weight", "sort_direction": "desc"}
        if key[0]:
            params["q"] = key[0][:60]
        if after:
            params["after"] = after
        data = self._call("GET", "/v1/connect/apps", params=params)
        apps = [{"slug": a["name_slug"], "name": a["name"], "description": (a.get("description") or "")[:140],
                 "img": a.get("img_src") or ""} for a in data.get("data") or [] if a.get("name_slug")]
        info = data.get("page_info") or {}
        result = (apps, info.get("end_cursor") if len(apps) == params["limit"] else None, info.get("total_count"))
        with self.lock:
            if len(self._catalog) > 500:
                self._catalog.clear()
            self._catalog[key] = (time.time(), result)
        return result

    # ----- connected accounts -----

    def connect_link(self, user_id, slug, redirect=""):
        body = {"external_user_id": str(user_id)}
        if redirect:
            body.update(success_redirect_uri=f"{redirect}?ok=1&app={quote(slug)}", error_redirect_uri=f"{redirect}?app={quote(slug)}")
        data = self._call("POST", f"/v1/connect/{self.project_id}/tokens", json=body)
        self.forget(user_id)
        return data["connect_link_url"] + "&app=" + quote(slug)

    def accounts(self, user_id, fresh=False):
        with self.lock:
            cached = self._accounts.get(user_id)
            if cached and not fresh and time.time() - cached[0] < ACCOUNTS_TTL:
                return cached[1]
        data = self._call("GET", f"/v1/connect/{self.project_id}/accounts", params={"external_user_id": str(user_id), "limit": 100})
        accounts = []
        for item in data.get("data") or []:
            app = item.get("app") or {}
            if not app.get("name_slug") or item.get("dead"):
                continue
            accounts.append({"id": item["id"], "slug": app["name_slug"], "name": app.get("name") or app["name_slug"],
                             "img": app.get("img_src") or "", "account": item.get("name") or "",
                             "healthy": item.get("healthy") is not False})
        with self.lock:
            self._accounts[user_id] = (time.time(), accounts)
        return accounts

    def disconnect(self, user_id, account_id):
        if not any(a["id"] == account_id for a in self.accounts(user_id, fresh=True)):
            return False  # only the owner can remove an account
        self._call("DELETE", f"/v1/connect/{self.project_id}/accounts/{quote(account_id)}")
        self.forget(user_id)
        return True

    def forget(self, user_id):
        with self.lock:
            self._accounts.pop(user_id, None)

    # ----- model tools -----

    def mcp_tools(self, user_id, taken=()):
        """Remote MCP tool definitions for the user's connected apps, plus their display names."""
        if not self.enabled:
            return [], []
        accounts = [a for a in self.accounts(user_id) if a["healthy"]]
        if not accounts:
            return [], []
        token = self.access_token()
        tools, names, seen = [], [], set(taken)
        for account in accounts:
            label = server_label(account["slug"])
            if label in seen or len(tools) >= MAX_APP_TOOLS:
                continue
            seen.add(label)
            names.append(account["name"])
            tools.append({"type": "mcp", "server_label": label, "server_url": self.mcp_url, "require_approval": "never",
                          "headers": {"Authorization": f"Bearer {token}", "x-pd-project-id": self.project_id,
                                      "x-pd-environment": self.environment, "x-pd-external-user-id": str(user_id),
                                      "x-pd-app-slug": account["slug"]}})
        return tools, names
