"""Key-free app connections through official remote MCP servers that support OAuth with dynamic client registration.

Flow (MCP authorization spec): find the server's authorization server (RFC 9728 / RFC 8414), register the bot
as an OAuth client once (RFC 7591), send the user to sign in with PKCE, keep the tokens encrypted and refresh them.
The bot owner configures nothing: the user just presses Connect and allows access.
"""
import base64
import hashlib
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.fernet import InvalidToken

SERVICE = "mcpapp"
FLOW_TTL = 15 * 60
TOKEN_MARGIN = 300
UA = {"User-Agent": "secret-ai-bot", "Accept": "application/json"}

# slug: (name, MCP server URL, Russian description, English description, simple-icons slug)
DIRECT_APPS = {
    # Zapier: each user signs in to their own (free) Zapier account and picks any of its 8000+ apps there.
    "zapier": ("Zapier · 8000+ приложений", "https://mcp.zapier.com/api/v1/connect",
               "Gmail, Google Календарь, Таблицы, Slack, Trello и ещё 8000 приложений через ваш Zapier",
               "Gmail, Google Calendar, Sheets, Slack, Trello and 8000 more apps through your Zapier", "zapier"),
    "notion": ("Notion", "https://mcp.notion.com/mcp", "Страницы, базы и заметки", "Pages, databases and notes", "notion"),
    "linear": ("Linear", "https://mcp.linear.app/mcp", "Задачи и проекты команды", "Team issues and projects", "linear"),
    "atlassian": ("Jira и Confluence", "https://mcp.atlassian.com/v1/mcp", "Задачи Jira и страницы Confluence",
                  "Jira issues and Confluence pages", "atlassian"),
    "asana": ("Asana", "https://mcp.asana.com/sse", "Задачи и проекты", "Tasks and projects", "asana"),
    "sentry": ("Sentry", "https://mcp.sentry.dev/mcp", "Ошибки и мониторинг", "Errors and monitoring", "sentry"),
    "canva": ("Canva", "https://mcp.canva.com/mcp", "Дизайны и презентации", "Designs and presentations", "canva"),
    "vercel": ("Vercel", "https://mcp.vercel.com", "Деплои и проекты", "Deployments and projects", "vercel"),
    "huggingface": ("Hugging Face", "https://huggingface.co/mcp", "Модели, датасеты и Spaces", "Models, datasets and Spaces",
                    "huggingface"),
    "stripe": ("Stripe", "https://mcp.stripe.com", "Платежи, клиенты, счета", "Payments, customers, invoices", "stripe"),
    "clickup": ("ClickUp", "https://mcp.clickup.com/mcp", "Задачи, списки и документы", "Tasks, lists and docs", "clickup"),
    "todoist": ("Todoist", "https://ai.todoist.net/mcp", "Задачи и напоминания", "Tasks and reminders", "todoist"),
    "monday": ("monday.com", "https://mcp.monday.com/sse", "Доски, задачи и проекты", "Boards, items and projects", "mondaydotcom"),
    "miro": ("Miro", "https://mcp.miro.com/", "Доски и схемы", "Boards and diagrams", "miro"),
    "figma": ("Figma", "https://mcp.figma.com/mcp", "Макеты и дизайн-системы", "Designs and design systems", "figma"),
    "intercom": ("Intercom", "https://mcp.intercom.com/mcp", "Переписка с клиентами", "Customer conversations", "intercom"),
    "attio": ("Attio", "https://mcp.attio.com/mcp", "CRM: контакты и сделки", "CRM: contacts and deals", "attio"),
    "close": ("Close", "https://mcp.close.com/mcp", "CRM для продаж", "Sales CRM", "close"),
    "paypal": ("PayPal", "https://mcp.paypal.com/mcp", "Платежи, счета и заказы", "Payments, invoices and orders", "paypal"),
    "square": ("Square", "https://mcp.squareup.com/sse", "Продажи, товары и клиенты", "Sales, items and customers", "square"),
    "webflow": ("Webflow", "https://mcp.webflow.com/sse", "Сайты и CMS", "Websites and CMS", "webflow"),
    "wix": ("Wix", "https://mcp.wix.com/mcp", "Сайты, магазины и записи", "Sites, stores and bookings", "wix"),
    "netlify": ("Netlify", "https://netlify-mcp.netlify.app/mcp", "Сайты и деплои", "Sites and deploys", "netlify"),
    "supabase": ("Supabase", "https://mcp.supabase.com/mcp", "Базы данных и проекты", "Databases and projects", "supabase"),
    "neon": ("Neon", "https://mcp.neon.tech/mcp", "Postgres-базы", "Postgres databases", "postgresql"),
    "prisma": ("Prisma Postgres", "https://mcp.prisma.io/mcp", "Базы и миграции", "Databases and migrations", "prisma"),
    "cloudflare": ("Cloudflare", "https://bindings.mcp.cloudflare.com/mcp", "Workers, KV, R2 и D1", "Workers, KV, R2 and D1",
                   "cloudflare"),
    "cloudinary": ("Cloudinary", "https://asset-management.mcp.cloudinary.com/sse", "Фото и видео в облаке",
                   "Images and videos in the cloud", "cloudinary"),
    "box": ("Box", "https://mcp.box.com", "Файлы и папки", "Files and folders", "box"),
    "fireflies": ("Fireflies", "https://api.fireflies.ai/mcp", "Записи и заметки встреч", "Meeting notes and recordings",
                  "fireflies"),
}


class MCPAuthError(Exception):
    pass


def direct_catalog(language):
    ru = language == "ru"
    return [{"slug": slug, "name": name, "description": d_ru if ru else d_en, "img": f"https://cdn.simpleicons.org/{icon}",
             "direct": True} for slug, (name, _, d_ru, d_en, icon) in DIRECT_APPS.items()]


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _origin(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}", parsed.path.rstrip("/")


class DirectApps:
    """Thread-safe: used by the Mini App HTTP thread (sign-in) and the bot loop (tokens for answers)."""

    def __init__(self, store, public_url):
        self.store, self.public_url = store, public_url
        self.http = httpx.Client(timeout=httpx.Timeout(10, connect=5), headers=UA, follow_redirects=True)
        self.flows, self.lock = {}, threading.Lock()

    @property
    def redirect_uri(self):
        return self.public_url + "/oauth/mcp/callback"

    # ----- discovery and client registration -----

    def _metadata(self, server_url):
        """Authorization server metadata for an MCP server (RFC 9728 -> RFC 8414 / OpenID discovery)."""
        origin, path = _origin(server_url)
        resource_meta = None
        try:
            probe = self.http.get(server_url)
            match = re.search(r'resource_metadata="([^"]+)"', probe.headers.get("www-authenticate", ""))
            if match:
                resource_meta = match.group(1)
        except httpx.TransportError as exc:
            raise MCPAuthError(f"server unreachable ({type(exc).__name__})") from None  # don't wait on every fallback URL
        except httpx.HTTPError:
            pass
        servers = []
        for url in filter(None, [resource_meta, f"{origin}/.well-known/oauth-protected-resource{path}",
                                 f"{origin}/.well-known/oauth-protected-resource"]):
            try:
                response = self.http.get(url)
                if response.status_code == 200:
                    servers = response.json().get("authorization_servers") or []
                    break
            except (httpx.HTTPError, ValueError):
                continue
        issuer = (servers[0] if servers else origin).rstrip("/")
        iss_origin, iss_path = _origin(issuer)
        for url in (f"{iss_origin}/.well-known/oauth-authorization-server{iss_path}",
                    f"{iss_origin}/.well-known/openid-configuration{iss_path}",
                    f"{issuer}/.well-known/openid-configuration"):
            try:
                response = self.http.get(url)
                if response.status_code == 200:
                    meta = response.json()
                    if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                        return meta
            except (httpx.HTTPError, ValueError):
                continue
        # Older servers without metadata use fixed paths on the server's origin.
        return {"authorization_endpoint": f"{origin}/authorize", "token_endpoint": f"{origin}/token",
                "registration_endpoint": f"{origin}/register"}

    def _client(self, slug):
        """The bot's OAuth client at this service, registered once and kept encrypted."""
        key = f"mcp_client:{slug}"
        saved = self.store.get_meta(key, secret=True)
        if saved and saved.get("redirect_uri") == self.redirect_uri:
            return saved
        meta = self._metadata(DIRECT_APPS[slug][1])
        if not meta.get("registration_endpoint"):
            raise MCPAuthError("no dynamic client registration")
        response = self.http.post(meta["registration_endpoint"], json={
            "client_name": "Secret AI", "redirect_uris": [self.redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        if response.status_code not in (200, 201):
            raise MCPAuthError(f"registration {response.status_code}")
        data = response.json()
        client = {"client_id": data["client_id"], "client_secret": data.get("client_secret") or "",
                  "authorization_endpoint": meta["authorization_endpoint"], "token_endpoint": meta["token_endpoint"],
                  "redirect_uri": self.redirect_uri}
        self.store.set_meta(key, client, secret=True)
        return client

    # ----- sign-in -----

    def start(self, user_id, slug):
        """URL where the user signs in to the app."""
        if slug not in DIRECT_APPS:
            raise MCPAuthError("unknown app")
        client = self._client(slug)
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            for key in [k for k, flow in self.flows.items() if now - flow["created"] > FLOW_TTL]:
                self.flows.pop(key)
            self.flows[state] = {"user_id": user_id, "slug": slug, "verifier": verifier, "created": now}
        query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": self.redirect_uri,
                 "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                 "resource": DIRECT_APPS[slug][1]}
        return client["authorization_endpoint"] + ("&" if "?" in client["authorization_endpoint"] else "?") + urlencode(query)

    def finish(self, state, code):
        """Callback: exchange the code and keep the tokens. Returns (user_id, app name)."""
        with self.lock:
            flow = self.flows.pop(state or "", None)
        if not flow or time.time() - flow["created"] > FLOW_TTL or not code:
            raise MCPAuthError("expired")
        slug = flow["slug"]
        client = self._client(slug)
        data = self._token(client, {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                                    "code_verifier": flow["verifier"], "resource": DIRECT_APPS[slug][1]})
        self._save(flow["user_id"], slug, data)
        return flow["user_id"], DIRECT_APPS[slug][0]

    def _token(self, client, form):
        form = {**form, "client_id": client["client_id"]}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        response = self.http.post(client["token_endpoint"], data=form)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not data.get("access_token"):
            raise MCPAuthError(f"token {response.status_code}")
        return data

    def _save(self, user_id, slug, data, old_refresh=""):
        refresh = data.get("refresh_token") or old_refresh
        meta = {"expires_at": time.time() + int(data["expires_in"]) if data.get("expires_in") else 0}
        if refresh:
            meta["refresh_token"] = self.store.fernet.encrypt(refresh.encode()).decode()
        self.store.save(user_id, SERVICE, slug, data["access_token"], url=DIRECT_APPS[slug][1], meta=meta)

    # ----- tokens for answers -----

    def connected(self, user_id):
        return [{"id": f"direct:{c['label']}", "slug": c["label"], "name": DIRECT_APPS[c["label"]][0],
                 "img": f"https://cdn.simpleicons.org/{DIRECT_APPS[c['label']][4]}", "account": "", "healthy": True}
                for c in self.store.public(user_id) if c["service"] == SERVICE and c["label"] in DIRECT_APPS]

    def disconnect(self, user_id, slug):
        return self.store.delete(user_id, SERVICE, slug)

    def mcp_tools(self, user_id, taken=()):
        """MCP tool definitions for the user's directly connected apps (refreshing tokens that are about to expire)."""
        tools, names, seen = [], [], set(taken)
        for item in self.store.get(user_id, SERVICE):
            slug = item["label"]
            if slug not in DIRECT_APPS or slug in seen:
                continue
            token = self._fresh(user_id, slug, item)
            if not token:
                continue
            seen.add(slug)
            names.append(DIRECT_APPS[slug][0])
            tools.append({"type": "mcp", "server_label": slug, "server_url": DIRECT_APPS[slug][1], "require_approval": "never",
                          "headers": {"Authorization": f"Bearer {token}"}})
        return tools, names

    def _fresh(self, user_id, slug, item):
        meta = item["meta"]
        if not meta.get("expires_at") or time.time() < meta["expires_at"] - TOKEN_MARGIN:
            return item["token"]
        try:
            refresh = self.store.fernet.decrypt(meta["refresh_token"].encode()).decode()
            data = self._token(self._client(slug), {"grant_type": "refresh_token", "refresh_token": refresh,
                                                    "resource": DIRECT_APPS[slug][1]})
        except (KeyError, InvalidToken, MCPAuthError, httpx.HTTPError, ValueError):
            return None  # the user has to connect again
        self._save(user_id, slug, data, old_refresh=refresh)
        return data["access_token"]
