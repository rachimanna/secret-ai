"""HTTP server: health checks, the Telegram Mini App and its API (connections), GitHub OAuth callback."""
import html
import json
import logging
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from integrations import GITHUB_API, check_init_data, check_mcp, github_headers, read_state, sign_state

LOG = logging.getLogger("secret_ai.miniapp")
PAGE = Path(__file__).with_name("webapp") / "index.html"
MAX_BODY = 64 * 1024


def github_login(token):
    """Return the GitHub login for a token, or None if the token does not work."""
    try:
        response = httpx.get(f"{GITHUB_API}/user", headers=github_headers(token), timeout=15)
    except httpx.HTTPError:
        return None
    return response.json().get("login") if response.status_code == 200 else None


def make_handler(config):
    """config: token, allowed, store, public_url, oauth_id, oauth_secret, notify(user_id, text)."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json; charset=utf-8", head=False, extra=None):
            data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _page(self, title, text):
            body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                    f"<title>{html.escape(title)}</title><body style='font-family:system-ui;background:#0e0b16;color:#eee;"
                    f"display:grid;place-items:center;min-height:90vh;text-align:center;padding:16px'>"
                    f"<div><h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></div>")
            self._send(200, body, "text/html; charset=utf-8")

        def _user(self):
            user = check_init_data(self.headers.get("X-Telegram-Init-Data", ""), config["token"])
            if not user:
                self._send(401, {"error": "auth"})
                return None
            if config["allowed"] and user["id"] not in config["allowed"]:
                self._send(403, {"error": "forbidden"})
                return None
            return user

        def _json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("object expected")
            return data

        def _state(self, user_id):
            items = config["store"].public(user_id)
            github = next((item for item in items if item["service"] == "github"), None)
            return {
                "github": {"connected": bool(github), "login": github["meta"].get("login") if github else None,
                           "oauth": bool(config["oauth_id"] and config["public_url"])},
                "mcp": [{"label": i["label"], "url": i["url"]} for i in items if i["service"] == "mcp"],
            }

        def do_HEAD(self):
            path = urlparse(self.path).path
            self._send(200 if path in ("/", "/health", "/app") else 404, b"", "text/plain", head=True)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/health"):
                self._send(200, b"Secret AI is running\n", "text/plain; charset=utf-8")
            elif url.path == "/app":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/oauth/github/callback":
                self._oauth_callback(parse_qs(url.query))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            user = self._user()
            if not user:
                return
            try:
                data = self._json()
            except ValueError:
                self._send(400, {"error": "bad request"})
                return
            store, uid = config["store"], user["id"]
            if path == "/api/state":
                self._send(200, self._state(uid))
            elif path == "/api/github/token":
                token = str(data.get("token") or "").strip()
                login = github_login(token) if token else None
                if not login:
                    self._send(400, {"error": "github_token"})
                    return
                store.save(uid, "github", "github", token, meta={"login": login})
                self._send(200, self._state(uid))
            elif path == "/api/github/oauth":
                if not (config["oauth_id"] and config["public_url"]):
                    self._send(400, {"error": "oauth_disabled"})
                    return
                query = urlencode({"client_id": config["oauth_id"], "scope": "repo", "state": sign_state(config["token"], uid),
                                   "redirect_uri": config["public_url"] + "/oauth/github/callback", "allow_signup": "true"})
                self._send(200, {"url": "https://github.com/login/oauth/authorize?" + query})
            elif path == "/api/mcp":
                try:
                    label, server_url = check_mcp(data.get("label"), data.get("url"))
                except ValueError as exc:
                    self._send(400, {"error": f"mcp_{exc}"})
                    return
                store.save(uid, "mcp", label, str(data.get("token") or "").strip(), url=server_url)
                self._send(200, self._state(uid))
            elif path == "/api/disconnect":
                store.delete(uid, str(data.get("service") or ""), str(data.get("label") or ""))
                self._send(200, self._state(uid))
            else:
                self._send(404, {"error": "not found"})

        def _oauth_callback(self, query):
            user_id = read_state(config["token"], (query.get("state") or [""])[0])
            code = (query.get("code") or [""])[0]
            if not user_id or not code or not config["oauth_id"]:
                self._page("Ссылка устарела", "Откройте Secret AI в Telegram и подключите GitHub ещё раз.")
                return
            try:
                response = httpx.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"}, timeout=15, data={
                    "client_id": config["oauth_id"], "client_secret": config["oauth_secret"], "code": code,
                    "redirect_uri": config["public_url"] + "/oauth/github/callback"})
                token = response.json().get("access_token")
            except (httpx.HTTPError, ValueError):
                token = None
            login = github_login(token) if token else None
            if not login:
                self._page("Не получилось", "GitHub не выдал доступ. Попробуйте ещё раз из Secret AI.")
                return
            config["store"].save(user_id, "github", "github", token, meta={"login": login})
            config["notify"](user_id, f"✅ GitHub подключён: @{login}\nТеперь можно писать, например: «покажи мои репозитории» или «измени README в моём репо …»")
            self._page("GitHub подключён ✅", f"Аккаунт @{login}. Вернитесь в Telegram.")

        def log_message(self, *args):
            pass

    return Handler
