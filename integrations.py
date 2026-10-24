"""Connected services for Secret AI: encrypted token storage, Mini App auth and model tools (GitHub, MCP)."""
import base64
import difflib
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, quote

from cryptography.fernet import Fernet, InvalidToken

GITHUB_API = "https://api.github.com"
INIT_DATA_MAX_AGE = 24 * 3600
OAUTH_STATE_MAX_AGE = 15 * 60
PENDING_TTL = 3600
MAX_FILE_CHARS = 60_000
MAX_TREE_PATHS = 400
MCP_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ---------- Telegram Mini App auth ----------

def check_init_data(init_data, bot_token, max_age=INIT_DATA_MAX_AGE):
    """Validate Telegram Mini App initData and return the Telegram user dict, or None."""
    try:
        params = dict(parse_qsl(init_data or "", strict_parsing=True))
    except ValueError:
        return None
    received = params.pop("hash", "")
    if not received:
        return None
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    # Newer clients add a "signature" field; accept the hash with or without it in the check string.
    variants = [params, {k: v for k, v in params.items() if k != "signature"}]
    for fields in variants:
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received):
            break
    else:
        return None
    try:
        if time.time() - int(params.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(params["user"])
    except (KeyError, ValueError, TypeError):
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None


def sign_state(secret, user_id):
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{user_id}.{stamp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{stamp}.{signature}"


def read_state(secret, state):
    """Return the user ID from a signed OAuth state, or None if it is forged or expired."""
    try:
        user_id, stamp, signature = (state or "").split(".")
        expected = hmac.new(secret.encode(), f"{user_id}.{stamp}".encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(expected, signature) and time.time() - int(stamp) <= OAUTH_STATE_MAX_AGE:
            return int(user_id)
    except ValueError:
        pass
    return None


# ---------- Encrypted connection storage ----------

class Connections:
    """Per-user service tokens, encrypted at rest. Safe to use from the bot and the HTTP thread."""

    def __init__(self, path, secret, owner=None):
        self.owner = owner  # OWNER_ID: fixed bot owner, survives a wiped database
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.lock = threading.Lock()
        key = hashlib.sha256(("secret-ai-vault:" + secret).encode()).digest()
        self.fernet = Fernet(base64.urlsafe_b64encode(key))
        with self.lock, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS connections (user_id INTEGER NOT NULL, service TEXT NOT NULL, "
                "label TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', secret BLOB NOT NULL, meta TEXT NOT NULL DEFAULT '{}', "
                "PRIMARY KEY (user_id, service, label))")

    def save(self, user_id, service, label, token, url="", meta=None):
        encrypted = self.fernet.encrypt((token or "").encode())
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO connections(user_id, service, label, url, secret, meta) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, service, label) DO UPDATE SET url=excluded.url, secret=excluded.secret, meta=excluded.meta",
                (user_id, service, label, url, encrypted, json.dumps(meta or {})))

    def get(self, user_id, service):
        """Connections of one service with decrypted tokens; entries that can't be decrypted are skipped."""
        with self.lock:
            rows = self.db.execute("SELECT label, url, secret, meta FROM connections WHERE user_id=? AND service=? ORDER BY label",
                                   (user_id, service)).fetchall()
        result = []
        for label, url, secret, meta in rows:
            try:
                token = self.fernet.decrypt(secret).decode()
            except InvalidToken:
                continue  # encryption key changed; the user has to connect again
            result.append({"label": label, "url": url, "token": token, "meta": json.loads(meta)})
        return result

    def public(self, user_id):
        """Connections without secrets, for the Mini App."""
        with self.lock:
            rows = self.db.execute("SELECT service, label, url, meta FROM connections WHERE user_id=? ORDER BY service, label",
                                   (user_id,)).fetchall()
        return [{"service": s, "label": l, "url": u, "meta": json.loads(m)} for s, l, u, m in rows]

    def delete(self, user_id, service, label):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM connections WHERE user_id=? AND service=? AND label=?",
                                   (user_id, service, label)).rowcount

    # ----- bot-wide settings (owner, the GitHub App created in one click) -----

    # ----- websites the bot made: served at /s/<id> -----

    def save_site(self, html):
        site_id = uuid.uuid4().hex[:12]
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS sites (id TEXT PRIMARY KEY, html BLOB NOT NULL, created INTEGER NOT NULL)")
            self.db.execute("INSERT INTO sites(id, html, created) VALUES (?, ?, ?)", (site_id, html, int(time.time())))
            self.db.execute("DELETE FROM sites WHERE id NOT IN (SELECT id FROM sites ORDER BY created DESC LIMIT 300)")
        return site_id

    def site(self, site_id):
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS sites (id TEXT PRIMARY KEY, html BLOB NOT NULL, created INTEGER NOT NULL)")
            row = self.db.execute("SELECT html FROM sites WHERE id=?", (site_id,)).fetchone()
        return bytes(row[0]) if row else None

    def _meta_table(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)")

    def get_meta(self, key, secret=False):
        if key == "owner" and self.owner:
            return self.owner
        with self.lock, self.db:
            self._meta_table()
            row = self.db.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            value = self.fernet.decrypt(row[0]).decode() if secret else row[0]
        except InvalidToken:
            return None
        return json.loads(value)

    def set_meta(self, key, value, secret=False):
        data = json.dumps(value)
        stored = self.fernet.encrypt(data.encode()) if secret else data
        with self.lock, self.db:
            self._meta_table()
            self.db.execute("INSERT INTO bot_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, stored))

    def claim_owner(self, user_id):
        """The bot owner (OWNER_ID, or whoever confirmed the console code with /owner); None until then.
        Nobody becomes the owner just by writing first: that would hand a stranger the admin tools."""
        return self.get_meta("owner")

    def close(self):
        self.db.close()


# ---------- GitHub ----------

class GitHubError(Exception):
    pass


def github_headers(token, raw=False):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "secret-ai-bot",
    }


class GitHub:
    def __init__(self, http, token):
        self.http, self.token = http, token

    async def call(self, method, path, raw=False, allow_404=False, **kwargs):
        response = await self.http.request(method, GITHUB_API + path, headers=github_headers(self.token, raw), timeout=30, **kwargs)
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", "")
            except ValueError:
                detail = ""
            hint = {401: "token is invalid or expired", 403: "token has no permission for this", 404: "not found or no access"}
            raise GitHubError(f"GitHub {response.status_code}: {detail or hint.get(response.status_code, 'error')}")
        if raw:
            return response.content
        return response.json() if response.content else None

    async def branch_sha(self, repo, branch):
        ref = await self.call("GET", f"/repos/{repo}/git/ref/heads/{quote(branch)}", allow_404=True)
        return ref["object"]["sha"] if ref else None

    async def tree(self, repo, commit_sha):
        """Map of path -> (blob sha, mode) for a commit, plus whether GitHub truncated the listing."""
        commit = await self.call("GET", f"/repos/{repo}/git/commits/{commit_sha}")
        tree = await self.call("GET", f"/repos/{repo}/git/trees/{commit['tree']['sha']}", params={"recursive": "1"})
        files = {item["path"]: (item["sha"], item["mode"]) for item in tree["tree"] if item["type"] == "blob"}
        return commit["tree"]["sha"], files, tree.get("truncated", False)

    async def text(self, repo, path, ref):
        data = await self.call("GET", f"/repos/{repo}/contents/{quote(path)}", raw=True, allow_404=True, params={"ref": ref})
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise GitHubError(f"{path} is a binary file") from None


def check_repo(repo):
    repo = (repo or "").strip().strip("/")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):].removesuffix(".git")
    if not REPO_NAME.match(repo):
        raise GitHubError("repo must look like owner/name")
    return repo


def clean_path(path):
    path = (path or "").strip().strip("/")
    if not path or ".." in path.split("/"):
        raise GitHubError(f"invalid path: {path!r}")
    return path


GITHUB_TOOLS = [
    {"type": "function", "name": "github_list_repos",
     "description": "List the user's GitHub repositories (most recently updated first).",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "github_list_files",
     "description": "List file paths in a repository branch. Use before reading or changing files.",
     "parameters": {"type": "object", "properties": {
         "repo": {"type": "string", "description": "owner/name; a bare name means the user's own repo"},
         "path": {"type": "string", "description": "optional folder prefix"},
         "branch": {"type": "string", "description": "optional; default branch if empty"}},
         "required": ["repo"], "additionalProperties": False}},
    {"type": "function", "name": "github_read_file",
     "description": "Read a text file from a repository.",
     "parameters": {"type": "object", "properties": {
         "repo": {"type": "string"}, "path": {"type": "string"}, "branch": {"type": "string"}},
         "required": ["repo", "path"], "additionalProperties": False}},
    {"type": "function", "name": "github_propose_changes",
     "description": (
         "Prepare a commit that writes, edits or deletes files. Nothing is applied until the user presses "
         "Confirm in Telegram. Read files before editing. Prefer action 'edit' with exact find/replace pairs "
         "for small changes in existing files; use 'write' with the full content for new or rewritten files."),
     "parameters": {"type": "object", "properties": {
         "repo": {"type": "string"},
         "message": {"type": "string", "description": "commit message"},
         "branch": {"type": "string", "description": "optional target branch; created from the default branch if missing"},
         "pull_request": {"type": "boolean", "description": "open a pull request from branch into the default branch"},
         "files": {"type": "array", "items": {"type": "object", "properties": {
             "path": {"type": "string"},
             "action": {"type": "string", "enum": ["write", "edit", "delete"]},
             "content": {"type": "string", "description": "full file content for 'write'"},
             "edits": {"type": "array", "description": "for 'edit': each find must occur exactly once",
                       "items": {"type": "object", "properties": {"find": {"type": "string"}, "replace": {"type": "string"}},
                                 "required": ["find", "replace"]}}},
             "required": ["path", "action"]}}},
         "required": ["repo", "message", "files"], "additionalProperties": False}},
    {"type": "function", "name": "github_delete_repos",
     "description": (
         "Prepare deletion of whole repositories (irreversible). Nothing is deleted until the user confirms twice "
         "in Telegram. Call github_list_repos first and pass exact owner/name values; never include repositories "
         "the user asked to keep."),
     "parameters": {"type": "object", "properties": {
         "repos": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
                   "description": "owner/name of each repository to delete"}},
         "required": ["repos"], "additionalProperties": False}},
]


MEDIA_TOOLS = [
    {"type": "function", "name": "generate_image",
     "description": ("Generate a picture or photo from a description and send it to the user in Telegram. Use it whenever the "
                     "user asks to draw, create, generate or imagine an image. Write a detailed prompt in English."),
     "parameters": {"type": "object", "properties": {
         "prompt": {"type": "string"},
         "format": {"type": "string", "enum": ["square", "portrait", "landscape"]}},
         "required": ["prompt"], "additionalProperties": False}},
    {"type": "function", "name": "generate_video",
     "description": ("Generate a video from a description and send it to the user. Use it only when the user explicitly "
                     "asks for a video or animation. Pass the user's idea in their own words; the bot splits it into scenes "
                     "and people speak the user's language on camera. Default 8 seconds unless the user asks for a length."),
     "parameters": {"type": "object", "properties": {
         "prompt": {"type": "string"},
         "format": {"type": "string", "enum": ["square", "portrait", "landscape"]},
         "seconds": {"type": "integer", "enum": [8, 16, 32, 64]},
         "voiceover": {"type": "boolean", "description": "speech in the video; true unless the user asks for no voice"}},
         "required": ["prompt"], "additionalProperties": False}},
]


class Toolbox:
    """Tools available to the model for one user and one reply."""

    def __init__(self, http, store, pending, user_id, language="ru", apps=((), ()), media=None, extra=()):
        self.http, self.store, self.pending, self.user_id, self.language = http, store, pending, user_id, language
        github = store.get(user_id, "github")
        self.github = github[0] if github else None
        self.mcp = store.get(user_id, "mcp")
        # Apps connected in one tap (Pipedream Connect): ready MCP tool definitions and their display names.
        self.app_tools, self.app_names = list(apps[0]), list(apps[1])
        # media(kind, prompt, format) -> text for the model; set when photo/video generation is available.
        self.media = media
        # Extra tools: dicts with "definition", "run" (async args -> text), "hint" (instructions) and "status" (ru, en).
        self.extra = {tool["definition"]["name"]: tool for tool in extra}
        self.proposals = []

    def definitions(self):
        tools = list(GITHUB_TOOLS) if self.github else []
        if self.media:
            tools += MEDIA_TOOLS
        for server in self.mcp:
            tool = {"type": "mcp", "server_label": server["label"], "server_url": server["url"], "require_approval": "never"}
            if server["token"]:
                tool["headers"] = {"Authorization": f"Bearer {server['token']}"}
            tools.append(tool)
        return tools + [tool["definition"] for tool in self.extra.values()] + self.app_tools

    def instructions(self):
        parts = []
        if self.github:
            parts.append(
                f"The user connected GitHub as @{self.github['meta'].get('login', '?')}; use the github_* tools for their repositories. "
                "To change a repository, inspect and read the relevant files first, then call github_propose_changes once with all changes. "
                "Changes are applied only after the user presses Confirm, so never say they are already done; "
                "briefly summarize what will change and ask them to confirm below. "
                "You can also delete whole repositories with github_delete_repos: list the repositories first, then "
                "prepare the deletion; the user confirms twice in Telegram. Do not tell the user you cannot delete repositories.")
        if self.mcp:
            parts.append("Connected services (MCP): " + ", ".join(s["label"] for s in self.mcp) + ".")
        if self.media:
            parts.append("You can generate images (generate_image) and short videos (generate_video); the result arrives in "
                         "the chat by itself a bit later, so just say briefly that it is being made.")
        if self.app_names:
            parts.append("Connected apps (use their MCP tools when the request involves them): " + ", ".join(self.app_names) + ".")
        parts += [tool["hint"] for tool in self.extra.values() if tool.get("hint")]
        return " ".join(parts)

    def status(self, name, args):
        ru = self.language == "ru"
        path = args.get("path") or ""
        labels = {
            "github_list_repos": ("Смотрю репозитории", "Looking at repositories"),
            "github_list_files": ("Смотрю файлы", "Looking at files"),
            "github_read_file": (f"Читаю {path}", f"Reading {path}"),
            "github_propose_changes": ("Готовлю изменения", "Preparing changes"),
            "github_delete_repos": ("Готовлю удаление", "Preparing deletion"),
            "generate_image": ("Запускаю генерацию фото", "Starting the image"),
            "generate_video": ("Запускаю генерацию видео", "Starting the video"),
        }
        ru_text, en_text = labels.get(name) or (self.extra[name].get("status") if name in self.extra else None) or ("Работаю", "Working")
        return ru_text if ru else en_text

    async def run(self, name, args):
        """Run a tool and return text for the model; errors are returned, not raised."""
        if name in self.extra:
            return await self.extra[name]["run"](args)
        if name in ("generate_image", "generate_video") and self.media:
            kind = "photo" if name == "generate_image" else "video"
            if kind == "photo":
                return await self.media(kind, args.get("prompt") or "", args.get("format") or "square")
            return await self.media(kind, args.get("prompt") or "", args.get("format") or "portrait",
                                    int(args.get("seconds") or 8), args.get("voiceover", True) is not False)
        if not self.github:
            return "GitHub is not connected. Ask the user to connect it in the Mini App (/app)."
        gh = GitHub(self.http, self.github["token"])
        try:
            if name == "github_list_repos":
                repos = await gh.call("GET", "/user/repos", params={"sort": "updated", "per_page": 50})
                return "\n".join(f"{r['full_name']} ({'private' if r['private'] else 'public'}, default branch {r['default_branch']})"
                                 for r in repos) or "No repositories."
            if name == "github_delete_repos":
                return await self._propose_delete(gh, args.get("repos") or [])
            repo = self._repo(args.get("repo"))
            if name == "github_list_files":
                branch = args.get("branch") or await self._default_branch(gh, repo)
                sha = await gh.branch_sha(repo, branch)
                if not sha:
                    return f"Branch {branch} not found."
                _, files, truncated = await gh.tree(repo, sha)
                prefix = (args.get("path") or "").strip("/")
                paths = [p for p in files if not prefix or p == prefix or p.startswith(prefix + "/")]
                listing = "\n".join(paths[:MAX_TREE_PATHS])
                more = len(paths) > MAX_TREE_PATHS or truncated
                return f"{repo}@{branch}, {len(paths)} files:\n{listing}" + ("\n… (list truncated, pass a folder path)" if more else "")
            if name == "github_read_file":
                branch = args.get("branch") or await self._default_branch(gh, repo)
                path = clean_path(args.get("path"))
                text = await gh.text(repo, path, branch)
                if text is None:
                    return f"{path} not found on {branch}."
                if len(text) > MAX_FILE_CHARS:
                    return text[:MAX_FILE_CHARS] + f"\n… (file truncated at {MAX_FILE_CHARS} characters; use 'edit' for changes)"
                return text
            if name == "github_propose_changes":
                return await self._propose(gh, repo, args)
            return f"Unknown tool {name}."
        except GitHubError as exc:
            return f"Error: {exc}"

    def _repo(self, repo):
        repo = (repo or "").strip()
        if repo and "/" not in repo and self.github["meta"].get("login"):
            repo = f"{self.github['meta']['login']}/{repo}"
        return check_repo(repo)

    async def _default_branch(self, gh, repo):
        return (await gh.call("GET", f"/repos/{repo}"))["default_branch"]

    async def _propose_delete(self, gh, names):
        repos, problems = [], []
        for name in names:
            try:
                repo = self._repo(name)
            except GitHubError as exc:
                problems.append(f"{name}: {exc}")
                continue
            info = await gh.call("GET", f"/repos/{repo}", allow_404=True)
            if info is None:
                problems.append(f"{repo}: not found or no access")
            elif info.get("permissions", {}).get("admin") is False:
                problems.append(f"{repo}: the user is not an admin of this repository, GitHub won't allow deleting it")
            elif info["full_name"] not in repos:
                repos.append(info["full_name"])
        if not repos:
            raise GitHubError("nothing to delete. " + "; ".join(problems))
        pid = uuid.uuid4().hex[:12]
        self.pending[pid] = {"kind": "delete_repos", "user_id": self.user_id, "repos": repos, "created": time.time()}
        self.proposals.append(pid)
        skipped = (" Skipped: " + "; ".join(problems) + ".") if problems else ""
        return (f"Prepared deletion of {len(repos)} repositories, NOT deleted yet: {', '.join(repos)}.{skipped} "
                "The user now sees a Delete button and must confirm twice.")

    async def _propose(self, gh, repo, args):
        default = await self._default_branch(gh, repo)
        branch = (args.get("branch") or default).strip()
        head = await gh.branch_sha(repo, branch)
        base_commit = head or await gh.branch_sha(repo, default)
        _, tree, _ = await gh.tree(repo, base_commit)
        files, seen = [], set()
        for item in args.get("files") or []:
            path, action = clean_path(item.get("path")), item.get("action") or "write"
            if path in seen:
                raise GitHubError(f"{path} is listed twice; merge its changes into one entry")
            seen.add(path)
            blob, mode = tree.get(path, (None, "100644"))
            old = await gh.text(repo, path, base_commit) if blob and action != "delete" else None
            if action == "delete":
                if old is None and not blob:
                    raise GitHubError(f"{path} does not exist")
                new = None
            elif action == "edit":
                if old is None:
                    raise GitHubError(f"{path} does not exist; use action 'write' to create it")
                new = old
                for edit in item.get("edits") or []:
                    count = new.count(edit.get("find") or "\0")
                    if count != 1:
                        raise GitHubError(f"in {path} the text to find occurs {count} times; read the file and use an exact unique fragment")
                    new = new.replace(edit["find"], edit.get("replace", ""))
            else:
                new = item.get("content")
                if new is None:
                    raise GitHubError(f"content is required to write {path}")
            if new == old and action != "delete":
                continue
            files.append({"path": path, "old": old or "", "new": new, "blob": blob, "mode": mode})
        if not files:
            raise GitHubError("these changes do not modify anything")
        pid = uuid.uuid4().hex[:12]
        self.pending[pid] = {
            "user_id": self.user_id, "repo": repo, "branch": branch, "default": default, "base_commit": base_commit,
            "branch_exists": head is not None, "message": (args.get("message") or "Update files").strip(),
            "pull_request": bool(args.get("pull_request")) and branch != default, "files": files, "created": time.time(),
        }
        self.proposals.append(pid)
        summary = ", ".join(f"{'delete' if f['new'] is None else 'create' if not f['blob'] else 'edit'} {f['path']}" for f in files)
        return f"Prepared, not applied yet: {summary} on {repo}@{branch}. The user now sees the diff with Confirm/Cancel buttons."


def prune_pending(pending):
    for pid in [pid for pid, item in pending.items() if time.time() - item["created"] > PENDING_TTL]:
        pending.pop(pid, None)


def describe(change, language="ru"):
    """Short summary and unified diff of a pending change."""
    ru = language == "ru"
    lines, diff = [], []
    for f in change["files"]:
        old_lines = f["old"].splitlines(keepends=True)
        new_lines = (f["new"] or "").splitlines(keepends=True)
        source = f"a/{f['path']}" if f["blob"] else "/dev/null"
        patch = list(difflib.unified_diff(old_lines, new_lines, source, f"b/{f['path']}" if f["new"] is not None else "/dev/null"))
        added = sum(1 for line in patch if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in patch if line.startswith("-") and not line.startswith("---"))
        icon = "🗑" if f["new"] is None else "🆕" if not f["blob"] else "✏️"
        lines.append(f"{icon} {f['path']}  +{added} −{removed}")
        diff.extend(line if line.endswith("\n") else line + "\n" for line in patch)
    target = f"{change['repo']} → {change['branch']}"
    if change["pull_request"]:
        target += " (pull request)" if not ru else " (через pull request)"
    head = ("📝 Изменения готовы, но ещё не применены\n" if ru else "📝 Changes are ready but not applied yet\n")
    text = f"{head}{target}\n💬 {change['message']}\n\n" + "\n".join(lines)
    return text, "".join(diff)


async def apply_change(http, store, change, checkpoint=None):
    """Commit a confirmed change in one commit; return (commit url, pull request url or None)."""
    github = store.get(change["user_id"], "github")
    if not github:
        raise GitHubError("GitHub is not connected anymore")
    gh = GitHub(http, github[0]["token"])
    repo, branch = change["repo"], change["branch"]
    if change.get("applied_commit"):
        commit = change["applied_commit"]
    else:
        head = await gh.branch_sha(repo, branch)
        if head is None:
            await gh.call("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": change["base_commit"]})
            head = change["base_commit"]
        tree_sha, tree, _ = await gh.tree(repo, head)
        if head != change["base_commit"]:
            for f in change["files"]:
                if tree.get(f["path"], (None,))[0] != f["blob"]:
                    raise GitHubError(f"{f['path']} changed after the proposal; ask again")
        entries = []
        for f in change["files"]:
            if f["new"] is None:
                if f["path"] in tree:
                    entries.append({"path": f["path"], "mode": tree[f["path"]][1], "type": "blob", "sha": None})
            else:
                entries.append({"path": f["path"], "mode": tree.get(f["path"], (None, f["mode"]))[1], "type": "blob", "content": f["new"]})
        new_tree = await gh.call("POST", f"/repos/{repo}/git/trees", json={"base_tree": tree_sha, "tree": entries})
        commit = await gh.call("POST", f"/repos/{repo}/git/commits", json={"message": change["message"], "tree": new_tree["sha"], "parents": [head]})
        await gh.call("PATCH", f"/repos/{repo}/git/refs/heads/{quote(branch)}", json={"sha": commit["sha"]})
        change["applied_commit"] = {"sha": commit["sha"], "html_url": commit["html_url"]}
        if checkpoint:
            checkpoint(change)
    pull = None
    if change["pull_request"]:
        title = change["message"].splitlines()[0][:120]
        existing = await gh.call("GET", f"/repos/{repo}/pulls", params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"})
        pull = existing[0] if existing else await gh.call(
            "POST", f"/repos/{repo}/pulls", json={"title": title, "head": branch, "base": change["default"], "body": change["message"]})
    return commit["html_url"], pull["html_url"] if pull else None


def describe_deletion(change, language="ru"):
    names = "\n".join(f"• {repo}" for repo in change["repos"])
    if language == "ru":
        return (f"🗑 Удаление репозиториев: {len(change['repos'])}\n{names}\n\n"
                "⚠️ Это необратимо: код, issues, pull request'ы и история пропадут навсегда. Пока ничего не удалено.")
    return (f"🗑 Deleting repositories: {len(change['repos'])}\n{names}\n\n"
            "⚠️ This is irreversible: code, issues, pull requests and history are gone for good. Nothing is deleted yet.")


async def delete_repos(http, store, change):
    """Delete confirmed repositories one by one; return (deleted, [(repo, reason)])."""
    github = store.get(change["user_id"], "github")
    if not github:
        raise GitHubError("GitHub is not connected anymore")
    gh = GitHub(http, github[0]["token"])
    deleted, failed = [], []
    for repo in change["repos"]:
        try:
            await gh.call("DELETE", f"/repos/{repo}")
            deleted.append(repo)
        except GitHubError as exc:
            failed.append((repo, str(exc)))
    return deleted, failed


def check_mcp(label, url):
    label = (label or "").strip().replace(" ", "_").replace("-", "_")
    url = (url or "").strip()
    if not MCP_LABEL.match(label):
        raise ValueError("label")
    if not url.startswith("https://") or len(url) > 500:
        raise ValueError("url")
    return label, url
