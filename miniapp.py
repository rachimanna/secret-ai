"""HTTP server: health checks, the Telegram Mini App and its API (apps, settings), GitHub OAuth callback."""
import html
import json
import logging
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from apps import PipedreamError, featured
from integrations import GITHUB_API, check_init_data, check_mcp, github_headers, read_state, sign_state

LOG = logging.getLogger("secret_ai.miniapp")
WEBAPP = Path(__file__).with_name("webapp")
PAGE = WEBAPP / "index.html"
LOGO = WEBAPP / "logo.svg"
MAX_BODY = 64 * 1024
MAX_PROMPT = 1500


def github_login(token):
    """Return the GitHub login for a token, or None if the token does not work."""
    try:
        response = httpx.get(f"{GITHUB_API}/user", headers=github_headers(token), timeout=15)
    except httpx.HTTPError:
        return None
    return response.json().get("login") if response.status_code == 200 else None


def result_page(title, text, ok=True):
    """Small page shown in the browser after signing in to a service."""
    logo = LOGO.read_text(encoding="utf-8")
    color = "#34c759" if ok else "#ff453a"
    return (
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;box-sizing:border-box;"
        "font:17px/1.4 -apple-system,system-ui,sans-serif;color:#fff;text-align:center;"
        "background:radial-gradient(120% 70% at 50% 0%,#4f5bff 0%,#2a2fb8 35%,#121218 75%)}"
        ".card{max-width:360px;padding:32px 24px;border-radius:32px;background:rgba(255,255,255,.08);"
        "border:1px solid rgba(255,255,255,.18);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);"
        "box-shadow:inset 0 1px 0 rgba(255,255,255,.25),0 20px 60px rgba(0,0,0,.35);animation:in .6s cubic-bezier(.2,.9,.3,1.2)}"
        ".logo{width:96px;height:96px;margin:0 auto 8px}"
        f".mark{{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:{color};margin-right:6px;font-size:16px}}"
        "h1{font-size:24px;margin:8px 0}p{color:rgba(255,255,255,.7);margin:0}"
        "@keyframes in{from{opacity:0;transform:translateY(20px) scale(.95)}}</style></head><body>"
        f"<div class=card><div class=logo>{logo}</div><h1><span class=mark>{'✓' if ok else '!'}</span>{html.escape(title)}</h1>"
        f"<p>{html.escape(text)}</p></div></body></html>")


def make_handler(config):
    """config: token, allowed, store, public_url, oauth_id, oauth_secret, bridge, pipedream."""
    pipedream = config["pipedream"]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json; charset=utf-8", head=False, cache="no-store"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _page(self, title, text, ok=True):
            self._send(200, result_page(title, text, ok), "text/html; charset=utf-8")

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

        def _apps(self, uid, fresh=False):
            if not pipedream.enabled:
                return {"enabled": False, "connected": []}
            try:
                return {"enabled": True, "connected": pipedream.accounts(uid, fresh=fresh)}
            except (PipedreamError, httpx.HTTPError, KeyError, ValueError) as exc:
                LOG.warning("Pipedream accounts failed: %s", exc if isinstance(exc, PipedreamError) else type(exc).__name__)
                return {"enabled": True, "connected": [], "error": True}

        def _state(self, user, fresh=False):
            uid = user["id"]
            items = config["store"].public(uid)
            github = next((item for item in items if item["service"] == "github"), None)
            return {
                "user": {"first_name": user.get("first_name") or "", "id": uid},
                "github": {"connected": bool(github), "login": github["meta"].get("login") if github else None,
                           "oauth": bool(config["oauth_id"] and config["public_url"])},
                "mcp": [{"label": i["label"], "url": i["url"]} for i in items if i["service"] == "mcp"],
                "apps": self._apps(uid, fresh),
                "settings": config["bridge"].settings(uid),
            }

        def do_HEAD(self):
            path = urlparse(self.path).path
            self._send(200 if path in ("/", "/health", "/app", "/logo.svg") else 404, b"", "text/plain", head=True)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/health"):
                self._send(200, b"Secret AI is running\n", "text/plain; charset=utf-8")
            elif url.path == "/app":
                self._send(200, PAGE.read_bytes().replace(b"__LOGO__", LOGO.read_bytes()), "text/html; charset=utf-8")
            elif url.path == "/logo.svg":
                self._send(200, LOGO.read_bytes(), "image/svg+xml", cache="public, max-age=86400")
            elif url.path == "/connected":
                query = parse_qs(url.query)
                app = (query.get("app") or [""])[0][:60]
                if (query.get("ok") or [""])[0] == "1":
                    self._page("Подключено", f"{app or 'Приложение'} готово к работе. Вернитесь в Telegram — Secret AI уже видит его.")
                else:
                    self._page("Не получилось", "Подключение не завершено. Вернитесь в Telegram и попробуйте ещё раз.", ok=False)
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
            try:
                self._route(path, user, data)
            except (PipedreamError, httpx.HTTPError) as exc:
                LOG.warning("Apps request failed: %s", exc if isinstance(exc, PipedreamError) else type(exc).__name__)
                self._send(502, {"error": "apps"})
            except (RuntimeError, TimeoutError):
                self._send(503, {"error": "busy"})

        def _route(self, path, user, data):
            store, uid, bridge = config["store"], user["id"], config["bridge"]
            language = "ru" if (user.get("language_code") or "ru").startswith("ru") else "en"
            if path == "/api/state":
                self._send(200, self._state(user, fresh=bool(data.get("fresh"))))
            elif path == "/api/catalog":
                if not pipedream.enabled:
                    query = str(data.get("q") or "").strip().lower()
                    apps = [a for a in featured(language) if not query or query in a["name"].lower()]
                    self._send(200, {"apps": apps, "next": None, "total": None, "enabled": False})
                    return
                apps, cursor, total = pipedream.catalog(str(data.get("q") or ""), str(data.get("after") or ""))
                self._send(200, {"apps": apps, "next": cursor, "total": total, "enabled": True})
            elif path == "/api/apps/connect":
                slug = str(data.get("slug") or "").strip()
                if not pipedream.enabled:
                    self._send(400, {"error": "apps_disabled"})
                    return
                if not slug or len(slug) > 80 or not slug.replace("_", "").replace("-", "").isalnum():
                    self._send(400, {"error": "bad request"})
                    return
                redirect = config["public_url"] + "/connected" if config["public_url"] else ""
                self._send(200, {"url": pipedream.connect_link(uid, slug, redirect)})
            elif path == "/api/apps/disconnect":
                pipedream.disconnect(uid, str(data.get("id") or ""))
                self._send(200, self._state(user, fresh=True))
            elif path == "/api/settings":
                bridge.update(uid, persona=data.get("persona"), model=data.get("model"))
                self._send(200, {"settings": bridge.settings(uid)})
            elif path == "/api/reset":
                bridge.reset(uid)
                self._send(200, {"settings": bridge.settings(uid)})
            elif path == "/api/ask":
                text = str(data.get("text") or "").strip()
                if not text or len(text) > MAX_PROMPT:
                    self._send(400, {"error": "bad request"})
                    return
                bridge.ask(user, text)
                self._send(200, {"ok": True})
            elif path == "/api/github/token":
                token = str(data.get("token") or "").strip()
                login = github_login(token) if token else None
                if not login:
                    self._send(400, {"error": "github_token"})
                    return
                store.save(uid, "github", "github", token, meta={"login": login})
                self._send(200, self._state(user))
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
                self._send(200, self._state(user))
            elif path == "/api/disconnect":
                store.delete(uid, str(data.get("service") or ""), str(data.get("label") or ""))
                self._send(200, self._state(user))
            else:
                self._send(404, {"error": "not found"})

        def _oauth_callback(self, query):
            user_id = read_state(config["token"], (query.get("state") or [""])[0])
            code = (query.get("code") or [""])[0]
            if not user_id or not code or not config["oauth_id"]:
                self._page("Ссылка устарела", "Откройте Secret AI в Telegram и подключите GitHub ещё раз.", ok=False)
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
                self._page("Не получилось", "GitHub не выдал доступ. Попробуйте ещё раз из Secret AI.", ok=False)
                return
            config["store"].save(user_id, "github", "github", token, meta={"login": login})
            try:
                config["bridge"].notify(user_id, f"✅ GitHub подключён: @{login}\nТеперь можно писать, например: «покажи мои репозитории» или «измени README в моём репо …»")
            except RuntimeError:
                pass
            self._page("GitHub подключён", f"Аккаунт @{login}. Вернитесь в Telegram.")

        def log_message(self, *args):
            pass

    return Handler
