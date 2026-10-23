"""HTTP server: health checks, the Telegram Mini App and its API (apps, settings), GitHub OAuth callback."""
import html
import base64
import json
import logging
import re
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

import codex
import gentemplates

from apps import PipedreamError, featured
from mcp_oauth import DIRECT_APPS, MCPAuthError, direct_catalog
from media import PACKS
from integrations import GITHUB_API, GitHubError, check_init_data, check_mcp, github_headers, read_state, sign_state

LOG = logging.getLogger("secret_ai.miniapp")
WEBAPP = Path(__file__).with_name("webapp")
PAGE = WEBAPP / "index.html"
LOGO = WEBAPP / "logo.svg"
SITE = WEBAPP / "site.html"
MASCOT = Path(__file__).with_name("assets") / "mascot.png"
MAX_BODY = 64 * 1024
MAX_UPLOAD = 16 * 1024 * 1024  # /api/edit: up to 4 photos (resized in the browser)
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
    """config: token, allowed, store, public_url, github (GitHubApps), bot_name(), bridge, pipedream."""
    pipedream, github, direct = config["pipedream"], config["github"], config["direct"]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json; charset=utf-8", head=False, cache="no-store"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not head:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # A closed WebView cannot receive another response. Never turn this into a 500.
                self.close_connection = True
                LOG.debug("Mini App client disconnected during response")

        def _page(self, title, text, ok=True):
            self._send(200, result_page(title, text, ok), "text/html; charset=utf-8")

        def _redirect(self, url):
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _user(self):
            user = check_init_data(self.headers.get("X-Telegram-Init-Data", ""), config["token"])
            if not user:
                self._send(401, {"error": "auth"})
                return None
            if config["allowed"] and user["id"] not in config["allowed"]:
                self._send(403, {"error": "forbidden"})
                return None
            if config.get("closed") and config["closed"]() and user["id"] != config["store"].get_meta("owner"):
                self._send(503, {"error": "Secret AI сейчас на обновлении — скоро вернусь."})  # /test mode
                return None
            return user

        def _json(self, limit=MAX_BODY):
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise ValueError("too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("object expected")
            return data

        def _apps(self, uid, fresh=False):
            # Directly connected apps (no keys needed) first, then Pipedream accounts if the catalog is set up.
            connected = direct.connected(uid)
            if not pipedream.enabled:
                return {"enabled": False, "connected": connected}
            try:
                return {"enabled": True, "connected": connected + pipedream.accounts(uid, fresh=fresh)}
            except (PipedreamError, httpx.HTTPError, KeyError, ValueError) as exc:
                LOG.warning("Pipedream accounts failed: %s", exc if isinstance(exc, PipedreamError) else type(exc).__name__)
                return {"enabled": True, "connected": connected, "error": True}

        def _state(self, user, fresh=False):
            uid = user["id"]
            items = config["store"].public(uid)
            saved = next((item for item in items if item["service"] == "github"), None)
            return {
                "user": {"first_name": user.get("first_name") or "", "id": uid},
                "github": {"connected": bool(saved), "login": saved["meta"].get("login") if saved else None,
                           "ready": bool(github.creds() and config["public_url"]),
                           "owner": config["store"].claim_owner(uid) == uid},
                "mcp": [{"label": i["label"], "url": i["url"]} for i in items if i["service"] == "mcp"],
                "apps": self._apps(uid, fresh),
                "settings": config["bridge"].settings(uid),
                "gen": config["bridge"].gen_state(uid),
                "secretary": config["bridge"].secretary(uid),
                "groups": config["bridge"].groups(uid),
                "bot": config["bot_name"](),
            }

        def do_HEAD(self):
            path = urlparse(self.path).path
            self._send(200 if path in ("/", "/index.html", "/health", "/app", "/app/", "/app/index.html", "/logo.svg", "/mascot.png", "/play-core.js", "/play-ui.js", "/play.css") else 404, b"", "text/plain", head=True)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                self._send(200, b"Secret AI is running\n", "text/plain; charset=utf-8")
            elif url.path in ("/", "/index.html"):  # the bot's own website
                bot = re.sub(r"[^A-Za-z0-9_]", "", str(config["bot_name"]() or "")) or "bot"
                self._send(200, SITE.read_bytes().replace(b"__BOT__", bot.encode()), "text/html; charset=utf-8",
                           cache="no-store")
            elif url.path == "/mascot.png":
                self._send(200, MASCOT.read_bytes(), "image/png", cache="public, max-age=86400")
            elif url.path in ("/app", "/app/", "/app/index.html"):
                self._send(200, PAGE.read_bytes().replace(b"__LOGO__", LOGO.read_bytes())
                           .replace(b"__TEMPLATES__", gentemplates.page_json().encode())
                           .replace(b"__APPS__", json.dumps({lang: featured(lang) for lang in ("ru", "en")},
                                                           ensure_ascii=False).encode()), "text/html; charset=utf-8")
            elif url.path.startswith("/tpl/") and url.path.endswith(".jpg"):
                image = gentemplates.preview(url.path[len("/tpl/"):-len(".jpg")])
                if image:
                    self._send(200, image, "image/jpeg", cache="public, max-age=86400")
                else:
                    self._send(404, b"", "text/plain")
            elif url.path in ("/play-core.js", "/play-ui.js", "/play.css"):
                content_type = "text/css" if url.path.endswith(".css") else "application/javascript"
                self._send(200, (WEBAPP / url.path[1:]).read_bytes(), content_type + "; charset=utf-8", cache="no-cache")
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
            elif url.path == "/oauth/mcp/callback":
                self._mcp_callback(parse_qs(url.query))
            elif url.path == "/oauth/github/installed":
                self._page("GitHub подключён", "Репозитории выбраны. Вернитесь в Telegram — Secret AI уже может с ними работать.")
            elif url.path == "/setup/github":
                self._setup_start(parse_qs(url.query))
            elif url.path == "/setup/github/done":
                self._setup_done(parse_qs(url.query))
            elif url.path.startswith("/s/") and len(url.path) <= 20 and url.path[3:].isalnum():
                html = config["store"].site(url.path[3:])
                if html is None:
                    self._page("Сайт не найден", "Ссылка устарела. Попросите Secret AI сделать сайт ещё раз.", ok=False)
                    return
                # A sandbox gives the page its own origin: its scripts can't reach the Mini App or its API.
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups allow-modals")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            user = self._user()
            if not user:
                return
            try:
                data = self._json(MAX_UPLOAD if path in ("/api/edit", "/api/live/turn") else MAX_BODY)
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
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            except Exception as exc:  # e.g. Telegram refusing an invoice: answer instead of dropping the connection
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                LOG.warning("Mini App request %s failed: %s at %s:%s", path, type(exc).__name__,
                            Path(frame.filename).name, frame.lineno)
                self._send(500, {"error": "server"})

        def _codex(self, action, user, data):
            """Codex mode: tasks are read straight from the store, the work runs on the bot's loop."""
            bridge, uid, store = config["bridge"], user["id"], config["codex"]()
            task_id = str(data.get("id") or "")[:20]
            if action == "state":
                github = config["store"].get(uid, "github")
                self._send(200, {"chats": store.threads(uid),
                                 "github": github[0]["meta"].get("login", "") if github else None,
                                 "can_run": config["store"].get_meta("owner") == uid and codex.sandbox_ready()})
            elif action == "thread":
                tasks = store.thread(str(data.get("thread") or "")[:20], uid)
                self._send(200, {"tasks": [dict(codex.public(t, full=True), change=codex.change_view(t.get("change"))) for t in tasks]})
            elif action == "task":
                task = store.get(task_id, uid)
                if not task:
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, {"task": dict(codex.public(task, full=True), change=codex.change_view(task.get("change")))})
            elif action == "start":
                mode = "repo" if data.get("mode") == "repo" else "project"
                prompt = str(data.get("prompt") or "").strip()[:4000]
                if not prompt:
                    self._send(400, {"error": "prompt"})
                    return
                task, error = bridge.codex_start(user, mode, prompt, str(data.get("repo") or "")[:120],
                                                 str(data.get("thread") or "")[:20] or None)
                self._send(200 if task else 409, {"task": codex.public(task)} if task else {"error": error})
            elif action == "stop":
                self._send(200, {"ok": bridge.codex_stop(uid, task_id)})
            elif action == "apply":
                try:
                    task = bridge.codex_apply(uid, task_id)
                except GitHubError as exc:
                    self._send(409, {"error": "github", "detail": str(exc)[:200]})
                    return
                self._send(200, {"task": codex.public(task)})
            elif action == "repos":
                try:
                    self._send(200, {"repos": bridge.codex_repos(uid)})
                except GitHubError as exc:
                    self._send(409, {"error": "github", "detail": str(exc)[:200]})
            elif action == "send":
                self._send(200, {"ok": bridge.codex_send(uid, task_id)})
            elif action == "site":
                url = bridge.codex_site(uid, task_id)
                self._send(200 if url else 409, {"url": url} if url else {"error": "no page"})
            elif action == "delete":  # a whole chat
                for task in store.thread(str(data.get("thread") or "")[:20], uid):
                    bridge.codex_stop(uid, task["id"])
                    store.delete(task["id"], uid)
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def _route(self, path, user, data):
            store, uid, bridge = config["store"], user["id"], config["bridge"]
            language = "ru" if (user.get("language_code") or "ru").startswith("ru") else "en"
            if path in ("/api/day/state", "/api/day/task", "/api/day/status"):
                try:
                    if path == "/api/day/state":
                        result = bridge.day_state(uid)
                    else:
                        if type(data.get("id")) is not int or not 0 < data["id"] < 10**12:
                            raise ValueError("id")
                        if path == "/api/day/task":
                            result = bridge.day_task(uid, data["id"])
                        else:
                            status = data.get("status")
                            if status not in ("open", "paused", "done"):
                                raise ValueError("status")
                            result = bridge.day_status(uid, data["id"], status)
                except ValueError:
                    self._send(400, {"error": "bad request"})
                    return
                self._send(200, result)
            elif path == "/api/state":
                self._send(200, self._state(user, fresh=bool(data.get("fresh"))))
            elif path == "/api/catalog":
                query, after = str(data.get("q") or "").strip().lower(), str(data.get("after") or "")
                # Apps that connect without any keys always come first.
                own = [] if after else [a for a in direct_catalog(language) if not query or query in a["name"].lower()]
                if not pipedream.enabled:
                    rest = [a for a in featured(language) if not query or query in a["name"].lower()]
                    cursor, total = None, None
                else:
                    rest, cursor, total = pipedream.catalog(query, after)
                    if not query and not after:  # everyday apps (Gmail, Calendar, Drive…) first, with their icons
                        picks = featured(language)
                        known = {a["slug"] for a in picks}
                        rest = picks + [a for a in rest if a["slug"] not in known]
                taken = {a["slug"] for a in own} | {"linear_app", "jira", "confluence"}  # same services as the direct ones
                self._send(200, {"apps": own + [a for a in rest if a["slug"] not in taken], "next": cursor, "total": total,
                                 "enabled": pipedream.enabled})
            elif path == "/api/apps/connect":
                slug = str(data.get("slug") or "").strip()
                if not slug or len(slug) > 80 or not slug.replace("_", "").replace("-", "").isalnum():
                    self._send(400, {"error": "bad request"})
                elif slug in DIRECT_APPS:
                    if not config["public_url"]:
                        self._send(400, {"error": "no_public_url"})
                        return
                    try:
                        self._send(200, {"url": direct.start(uid, slug)})
                    except (MCPAuthError, httpx.HTTPError, KeyError, ValueError) as exc:
                        LOG.warning("Direct connect to %s failed: %s", slug, exc if isinstance(exc, MCPAuthError) else type(exc).__name__)
                        self._send(502, {"error": "apps"})
                elif not pipedream.enabled:
                    self._send(400, {"error": "apps_disabled"})
                else:
                    redirect = config["public_url"] + "/connected" if config["public_url"] else ""
                    self._send(200, {"url": pipedream.connect_link(uid, slug, redirect)})
            elif path == "/api/apps/disconnect":
                account = str(data.get("id") or "")
                if account.startswith("direct:"):
                    direct.disconnect(uid, account[len("direct:"):])
                else:
                    pipedream.disconnect(uid, account)
                self._send(200, self._state(user, fresh=True))
            elif path == "/api/generate":
                kind, prompt = str(data.get("kind") or ""), str(data.get("prompt") or "").strip()
                if kind not in ("photo", "video") or not prompt or len(prompt) > 1500:
                    self._send(400, {"error": "bad request"})
                    return
                try:
                    seconds = int(data.get("seconds") or 8)
                except (TypeError, ValueError):
                    seconds = 8
                error = bridge.generate(user, kind, prompt, str(data.get("format") or "square"), seconds, data.get("voice") is not False)
                if error:
                    self._send(400, {"error": f"gen_{error}"})
                    return
                self._send(200, {"gen": bridge.gen_state(uid)})
            elif path == "/api/promo":
                ok, text = bridge.promo(user, str(data.get("code") or "")[:64])
                self._send(200 if ok else 400, {"ok": ok, "text": text, "gen": bridge.gen_state(uid)} if ok
                           else {"error": "promo", "detail": text})
            elif path == "/api/live/turn":  # Secret AI Live: one spoken phrase (+ a camera frame) -> the answer as voice
                try:
                    audio = base64.b64decode(str(data.get("audio") or "").split(",", 1)[-1], validate=True)
                    image = base64.b64decode(str(data.get("image")).split(",", 1)[-1], validate=True) if data.get("image") else None
                except ValueError:
                    self._send(400, {"error": "bad request"})
                    return
                if (not audio and not image) or len(audio) > 8_000_000 or (image and len(image) > 4_000_000):
                    self._send(400, {"error": "bad request"})
                    return
                mode = str(data.get("mode") or "") or None
                self._send(200, bridge.live_turn(user, audio, image, mode))
            elif path == "/api/live/end":
                self._send(200, bridge.live_end(user))
            elif path == "/api/edit":
                prompt = str(data.get("prompt") or "").strip()
                raw = data.get("images") if isinstance(data.get("images"), list) else []
                try:
                    images = [base64.b64decode(str(item).split(",", 1)[-1], validate=True) for item in raw[:4]]
                except ValueError:
                    images = []
                if not prompt or len(prompt) > 1500 or not images or any(len(i) > 6_000_000 for i in images):
                    self._send(400, {"error": "bad request"})
                    return
                error = bridge.edit(user, images, prompt, str(data.get("format") or "portrait"))
                if error:
                    self._send(400, {"error": f"gen_{error}"})
                    return
                self._send(200, {"gen": bridge.gen_state(uid)})
            elif path == "/api/gen/buy":
                pack = str(data.get("pack") or "")
                if pack not in PACKS:
                    self._send(400, {"error": "bad request"})
                    return
                self._send(200, {"url": bridge.invoice(user, pack)})
            elif path == "/api/gen/key":
                # Only the bot owner sets the fal.ai key (it spends the owner's money) and the models.
                if store.claim_owner(uid) != uid:
                    self._send(403, {"error": "forbidden"})
                    return
                key = str(data.get("key") or "").strip()
                if key:
                    if len(key) > 200 or not all(c.isalnum() or c in "-_:." for c in key):
                        self._send(400, {"error": "gen_bad_key_format"})
                        return
                    store.set_meta("fal_key", key, secret=True)
                gemini = str(data.get("gemini") or "").strip()
                if gemini:
                    if len(gemini) > 200 or not all(c.isalnum() or c in "-_" for c in gemini):
                        self._send(400, {"error": "gen_bad_key_format"})
                        return
                    store.set_meta("gemini_key", gemini, secret=True)
                hf = str(data.get("hf") or "").strip()
                if hf:
                    if len(hf) > 300 or hf.count(":") != 1 or not all(c.isalnum() or c in "-_:" for c in hf):
                        self._send(400, {"error": "gen_bad_hf_key"})
                        return
                    store.set_meta("hf_key", hf, secret=True)
                models = {kind: str(data.get(kind) or "").strip() for kind in ("photo", "video")}
                if any(len(m) > 120 or (m and not all(c.isalnum() or c in "-_./" for c in m)) for m in models.values()):
                    self._send(400, {"error": "gen_bad_model"})
                    return
                store.set_meta("fal_models", {k: v for k, v in models.items() if v})
                self._send(200, {"gen": bridge.gen_state(uid)})
            elif path == "/api/secretary":
                rules = data.get("rules")
                if rules is not None and len(str(rules)) > 1000:
                    self._send(400, {"error": "bad request"})
                    return
                self._send(200, {"secretary": bridge.set_secretary(
                    uid, enabled=data.get("enabled"), rules=rules, notify=data.get("notify"))})
            elif path == "/api/groups/level":
                try:
                    chat_id = int(data.get("chat_id"))
                except (TypeError, ValueError):
                    self._send(400, {"error": "bad request"})
                    return
                if not bridge.set_group_level(uid, chat_id, str(data.get("level") or "")):
                    self._send(403, {"error": "group_admin"})
                    return
                self._send(200, {"groups": bridge.groups(uid)})
            elif path == "/api/groups/delete":
                try:
                    chat_id = int(data.get("chat_id"))
                except (TypeError, ValueError):
                    self._send(400, {"error": "bad request"})
                    return
                if not bridge.delete_group(uid, chat_id):
                    self._send(403, {"error": "group_admin"})
                    return
                self._send(200, {"groups": bridge.groups(uid)})
            elif path == "/api/groups/model":
                try:
                    chat_id = int(data.get("chat_id"))
                except (TypeError, ValueError):
                    self._send(400, {"error": "bad request"})
                    return
                if not bridge.set_group_model(uid, chat_id, str(data.get("model") or "")):
                    self._send(403, {"error": "group_admin"})
                    return
                self._send(200, {"groups": bridge.groups(uid)})
            elif path.startswith("/api/codex/"):
                self._codex(path[len("/api/codex/"):], user, data)
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
            elif path == "/api/github/connect":
                public_url = config["public_url"]
                if not public_url:
                    self._send(400, {"error": "no_public_url"})
                elif github.creds():
                    self._send(200, {"url": github.authorize_url(public_url, sign_state(config["token"], uid))})
                elif store.claim_owner(uid) == uid:
                    # First connection ever: the owner lets the bot create its GitHub App in one click.
                    self._send(200, {"url": f"{public_url}/setup/github?s={sign_state(config['token'], uid)}"})
                else:
                    self._send(400, {"error": "github_setup"})
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

        def _mcp_callback(self, query):
            try:
                user_id, name = direct.finish((query.get("state") or [""])[0], (query.get("code") or [""])[0])
            except (MCPAuthError, httpx.HTTPError, KeyError, ValueError) as exc:
                LOG.warning("Direct app sign-in failed: %s", exc if isinstance(exc, MCPAuthError) else type(exc).__name__)
                self._page("Не получилось", "Подключение не завершено. Вернитесь в Telegram и попробуйте ещё раз.", ok=False)
                return
            try:
                config["bridge"].notify(user_id, f"✅ {name} подключён. Теперь просто напишите, что сделать.")
            except RuntimeError:
                pass
            self._page("Подключено", f"{name} готов к работе. Вернитесь в Telegram — Secret AI уже видит его.")

        def _owner_state(self, query):
            """User ID from a signed state if it belongs to the bot owner, else None."""
            user_id = read_state(config["token"], (query.get("state") or query.get("s") or [""])[0])
            return user_id if user_id and config["store"].claim_owner(user_id) == user_id else None

        def _setup_start(self, query):
            user_id = self._owner_state(query)
            if not user_id or github.creds():
                self._page("Ссылка устарела", "Откройте Secret AI в Telegram и нажмите «Подключить» у GitHub ещё раз.", ok=False)
                return
            page = github.manifest_page(config["public_url"], sign_state(config["token"], user_id), config["bot_name"]())
            self._send(200, page, "text/html; charset=utf-8")

        def _setup_done(self, query):
            user_id = self._owner_state(query)
            code = (query.get("code") or [""])[0]
            if not user_id or not code:
                self._page("Ссылка устарела", "Откройте Secret AI в Telegram и нажмите «Подключить» у GitHub ещё раз.", ok=False)
                return
            try:
                created = github.creds() or github.finish_setup(code)
            except (httpx.HTTPError, ValueError, KeyError):
                created = None
            if not created:
                self._page("Не получилось", "GitHub не создал приложение. Попробуйте ещё раз из Secret AI.", ok=False)
                return
            # The app exists now: continue straight to signing in.
            self._redirect(github.authorize_url(config["public_url"], sign_state(config["token"], user_id)))

        def _oauth_callback(self, query):
            user_id = read_state(config["token"], (query.get("state") or [""])[0])
            code = (query.get("code") or [""])[0]
            if not user_id or not code or not github.creds():
                self._page("Ссылка устарела", "Откройте Secret AI в Telegram и подключите GitHub ещё раз.", ok=False)
                return
            token, meta = github.exchange(code, config["public_url"])
            login = github_login(token) if token else None
            if not login:
                self._page("Не получилось", "GitHub не выдал доступ. Попробуйте ещё раз из Secret AI.", ok=False)
                return
            config["store"].save(user_id, "github", "github", token, meta={"login": login, **meta})
            try:
                config["bridge"].notify(user_id, f"✅ GitHub подключён: @{login}\nТеперь можно писать, например: «покажи мои репозитории» или «измени README в моём репо …»")
            except RuntimeError:
                pass
            install = github.install_url()
            if install and not github.has_installation(token):
                self._redirect(install)  # let the user pick repositories for the app
                return
            self._page("GitHub подключён", f"Аккаунт @{login}. Вернитесь в Telegram.")

        def log_message(self, *args):
            pass

    return Handler
