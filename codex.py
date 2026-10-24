"""Codex mode of the Mini App: an agent that works on a task by itself, like OpenAI Codex.

Two kinds of tasks:
- repo: works in the user's GitHub repository (reads files, prepares one commit); the user reviews the diff in the Mini App
  and creates a branch with a pull request in one tap;
- project: builds a new project from scratch in its own workspace (files kept with the task). The owner's tasks may also
  run commands in a temporary folder to test the code; other people's never run anything on the server.

Tasks live in the connections database, so the HTTP thread reads them directly and they survive restarts.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid

MAX_TASKS = 120  # messages kept per person
MAX_FILES = 60
MAX_FILE = 200_000
MAX_LOG = 80
ROUNDS = 40  # tool rounds for one task
RUN_TIMEOUT = 90
RUN_OUTPUT = 6000

STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_APPLIED, STATUS_STOPPED = "running", "done", "failed", "applied", "stopped"
STATUS_PAUSED = "paused"


class CodexStore:
    def __init__(self, connections):
        self.db, self.lock = connections.db, connections.lock
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS codex_tasks (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                            "data TEXT NOT NULL, created INTEGER NOT NULL)")
            # Tasks that were running when the bot restarted can't continue.
            for tid, raw in self.db.execute("SELECT id, data FROM codex_tasks").fetchall():
                task = json.loads(raw)
                if task.get("status") == "applying":
                    task.update(status=STATUS_DONE, error="Application interrupted; review and retry.")
                    self.db.execute("UPDATE codex_tasks SET data=? WHERE id=?", (json.dumps(task), tid))
                if task.get("status") == STATUS_RUNNING:
                    task.update(status=STATUS_PAUSED, error="restart")
                    self.db.execute("UPDATE codex_tasks SET data=? WHERE id=?", (json.dumps(task), tid))

    def create(self, user_id, mode, prompt, repo="", model="", thread=None, files=None, branch=""):
        """A task is one message of a Codex chat; the chat (thread) is the id of its first task."""
        task_id = uuid.uuid4().hex[:10]
        task = {"id": task_id, "thread": thread or task_id, "user_id": user_id, "mode": mode, "prompt": prompt, "repo": repo,
                "model": model, "branch": branch, "status": STATUS_RUNNING, "step": "", "log": [], "summary": "",
                "files": dict(files or {}), "change": None, "result": {}, "error": "", "created": int(time.time()),
                "updated": int(time.time())}
        with self.lock, self.db:
            self.db.execute("INSERT INTO codex_tasks(id, user_id, data, created) VALUES (?, ?, ?, ?)",
                            (task["id"], user_id, json.dumps(task), task["created"]))
            self.db.execute("DELETE FROM codex_tasks WHERE user_id=? AND id NOT IN (SELECT id FROM codex_tasks WHERE user_id=? "
                            "ORDER BY created DESC LIMIT ?)", (user_id, user_id, MAX_TASKS))
        return task

    def get(self, task_id, user_id=None):
        with self.lock:
            row = self.db.execute("SELECT data FROM codex_tasks WHERE id=?", (task_id,)).fetchone()
        task = json.loads(row[0]) if row else None
        return task if task and (user_id is None or task["user_id"] == user_id) else None

    def save(self, task):
        task["log"] = task["log"][-MAX_LOG:]
        task["updated"] = int(time.time())
        with self.lock, self.db:
            self.db.execute("UPDATE codex_tasks SET data=? WHERE id=?", (json.dumps(task), task["id"]))

    def update(self, task_id, **fields):
        task = self.get(task_id)
        if task:
            task.update(fields)
            self.save(task)
        return task

    def list(self, user_id):
        with self.lock:
            rows = self.db.execute("SELECT data FROM codex_tasks WHERE user_id=? ORDER BY created DESC", (user_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def thread(self, thread, user_id):
        """The messages of one chat, oldest first."""
        return sorted((t for t in self.list(user_id) if t.get("thread", t["id"]) == thread), key=lambda t: (t["created"], t["updated"]))

    def threads(self, user_id):
        """Chats, newest activity first: the first message names the chat, the last one gives its state."""
        chats = {}
        for task in self.list(user_id):
            chats.setdefault(task.get("thread", task["id"]), []).append(task)
        out = []
        for thread, tasks in chats.items():
            tasks.sort(key=lambda t: t["created"])
            first, last = tasks[0], tasks[-1]
            out.append({"thread": thread, "title": first["prompt"][:80], "mode": first["mode"], "repo": first["repo"],
                        "status": last["status"], "step": last.get("step", ""), "count": len(tasks), "updated": last["updated"]})
        return sorted(out, key=lambda c: -c["updated"])

    def delete(self, task_id, user_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM codex_tasks WHERE id=? AND user_id=?", (task_id, user_id))


def public(task, full=False):
    """What the Mini App sees: the list gets a light version, the details everything."""
    keys = ("id", "thread", "mode", "prompt", "repo", "model", "branch", "status", "step", "summary", "result", "error", "created",
            "updated", "checks")
    out = {k: task.get(k) for k in keys}
    out["files_count"] = len(task.get("files") or {}) or len((task.get("change") or {}).get("files") or [])
    if full:
        out["log"] = task.get("log") or []
        out["files"] = {path: text[:MAX_FILE] for path, text in (task.get("files") or {}).items()}
    return out


class TaskLive:
    """Stands in for a chat reply while the agent works: steps go to the task's log, the text to its summary."""

    def __init__(self, store, task_id, language="ru"):
        self.store, self.task_id, self.language = store, task_id, language
        self.text, self.note, self.done, self.offset = "", None, [], 0
        self.saved = 0.0

    def status(self, note):
        if not note or note == self.note:
            return
        self.note = note
        task = self.store.get(self.task_id)
        if task:
            task["step"] = note
            task["log"].append({"t": int(time.time()), "text": note})
            self.store.save(task)

    async def update(self, text):
        self.text = text
        if time.monotonic() - self.saved > 1.5:  # the summary shows up live, without writing on every token
            self.saved = time.monotonic()
            self.store.update(self.task_id, summary=text[-4000:])

    async def stop(self):
        pass

    async def abort(self, note):
        pass


def clean(path):
    path = (path or "").strip().replace("\\", "/").strip("/")
    if not path or ".." in path.split("/") or path.startswith("~") or len(path) > 200:
        raise ValueError(f"invalid path: {path!r}")
    return path


WORKSPACE_TOOLS = [
    {"type": "function", "name": "list_files", "description": "List the project's files with their sizes.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "read_file", "description": "Read a project file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"type": "function", "name": "write_file",
     "description": "Create or overwrite a project file with its complete content (never fragments with '...').",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"], "additionalProperties": False}},
    {"type": "function", "name": "edit_file", "description": "Replace an exact, unique fragment of a project file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "find": {"type": "string"},
                                                     "replace": {"type": "string"}},
                    "required": ["path", "find", "replace"], "additionalProperties": False}},
    {"type": "function", "name": "delete_file", "description": "Delete a project file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
]
RUN_TOOL = {"type": "function", "name": "run",
            "description": ("Run a shell command in a fresh folder holding the project's files (python3, node, bash; no "
                            f"network guarantees; {RUN_TIMEOUT} s limit). Use it to test the code and fix what fails."),
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
                           "additionalProperties": False}}


class Workspace:
    """The files of a project task, kept in the task itself; run() exists only for the owner."""

    def __init__(self, store, task_id, can_run=False, language="ru"):
        self.store, self.task_id, self.can_run, self.language = store, task_id, can_run, language

    def definitions(self):
        return WORKSPACE_TOOLS + ([RUN_TOOL] if self.can_run else [])

    def status(self, name, args):
        ru, path = self.language == "ru", args.get("path") or ""
        labels = {"list_files": ("Смотрю файлы", "Looking at files"), "read_file": (f"Читаю {path}", f"Reading {path}"),
                  "write_file": (f"Пишу {path}", f"Writing {path}"), "edit_file": (f"Правлю {path}", f"Editing {path}"),
                  "delete_file": (f"Удаляю {path}", f"Deleting {path}"),
                  "run": ("Запускаю: " + (args.get("command") or "")[:60], "Running: " + (args.get("command") or "")[:60])}
        pair = labels.get(name, ("Работаю", "Working"))
        return pair[0] if ru else pair[1]

    async def run(self, name, args):
        import asyncio
        task = self.store.get(self.task_id)
        files = task["files"]
        try:
            if name == "list_files":
                return "\n".join(f"{p} ({len(t)} chars)" for p, t in sorted(files.items())) or "No files yet."
            if name == "read_file":
                path = clean(args.get("path"))
                return files[path] if path in files else f"{path} does not exist."
            if name == "write_file":
                path, content = clean(args.get("path")), str(args.get("content") or "")
                if len(content) > MAX_FILE:
                    return f"Too big ({len(content)} chars, the limit is {MAX_FILE}); split it."
                if path not in files and len(files) >= MAX_FILES:
                    return f"Too many files (limit {MAX_FILES})."
                files[path] = content
                self.store.save(task)
                return f"Wrote {path} ({len(content)} chars)."
            if name == "edit_file":
                path = clean(args.get("path"))
                if path not in files:
                    return f"{path} does not exist; use write_file."
                count = files[path].count(args.get("find") or "\0")
                if count != 1:
                    return f"The text to find occurs {count} times in {path}; read the file and use an exact unique fragment."
                files[path] = files[path].replace(args["find"], args.get("replace", ""))
                self.store.save(task)
                return f"Edited {path}."
            if name == "delete_file":
                path = clean(args.get("path"))
                files.pop(path, None)
                self.store.save(task)
                return f"Deleted {path}."
            if name == "run" and self.can_run:
                command = str(args.get("command") or "")
                result = await asyncio.to_thread(run_command, files, command)
                latest = self.store.get(self.task_id)
                if latest:
                    latest.setdefault("checks", []).append({"command": command[:500], "result": result[:6000]})
                    self.store.save(latest)
                return result
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Unknown tool {name}."


_sandbox = {}


def sandbox_ready():
    """Commands run only inside a bubblewrap sandbox: no network, no view of the server's files, only the project folder.
    Without bwrap (or where it can't start) the run tool is off. CODEX_RUN=0 turns it off anyway."""
    if "ok" not in _sandbox:
        ok = False
        if os.environ.get("CODEX_RUN", "1").strip() != "0" and shutil.which("bwrap"):
            folder = tempfile.mkdtemp(prefix="codex-check-")
            try:
                ok = subprocess.run(_sandboxed(folder, "true"), capture_output=True, timeout=20, env=_ENV).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        _sandbox["ok"] = ok
    return _sandbox["ok"]


_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


def _sandboxed(folder, command):
    """bwrap: the system's programs read-only, the project folder as /work, fresh /tmp, /proc and /dev, every namespace
    (network included) unshared, no access to the bot's folder, home, database or environment."""
    args = ["bwrap", "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/lib", "/lib64", "/sbin", "/etc/alternatives", "/etc/ld.so.cache", "/etc/ld.so.conf",
                 "/etc/ld.so.conf.d"):
        args += ["--ro-bind-try", path, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", folder, "/work", "--chdir", "/work",
             "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    for key, value in _ENV.items():
        args += ["--setenv", key, value]
    return args + ["bash", "-c", command]


def run_command(files, command):
    """Write the files into a temporary folder and run the command there, inside the sandbox."""
    if not sandbox_ready():
        return "Running commands is not available on this server."
    folder = tempfile.mkdtemp(prefix="codex-")
    try:
        for path, text in files.items():
            target = os.path.realpath(os.path.join(folder, path))
            if not target.startswith(os.path.realpath(folder) + os.sep):
                continue  # a path that leaves the folder is never written
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as out:
                out.write(text)
        try:
            done = subprocess.run(_sandboxed(folder, command), env=_ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT)
            output = (done.stdout + ("\n" + done.stderr if done.stderr else "")).strip()
            result = f"exit code {done.returncode}\n{output}"
        except subprocess.TimeoutExpired:
            result = f"Timed out after {RUN_TIMEOUT} s."
        return result if len(result) <= RUN_OUTPUT else result[:RUN_OUTPUT // 2] + "\n…\n" + result[-RUN_OUTPUT // 2:]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def slug(text):
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:30].strip("-") or "task"


def instructions(mode, repo, branch, can_run, language):
    lang = "Russian" if language == "ru" else "English"
    common = (" You are Codex inside the Secret AI Telegram bot: an autonomous senior software engineer. Work on the task by "
              "yourself until it is done, without asking questions: make reasonable decisions and state them. Write complete, "
              "production-quality code that matches the project's style. When finished, reply with a short report in "
              f"{lang}: what you did, which files changed and how to run or check it. Plain text, no Markdown headings.")
    if mode == "repo":
        return (f"You work in the GitHub repository {repo}. First look at the files (github_list_files) and read everything "
                "relevant (github_read_file); then make all changes with ONE github_propose_changes call, using branch "
                f"'{branch}' and pull_request true. Prefer exact 'edit' changes for existing files. Nothing is committed until "
                "the user presses the button in the Mini App, so say the changes are ready for review, not applied." + common)
    run = (" You can run commands with the run tool: test what you wrote (run scripts, tests, a quick syntax check) and fix "
           "errors before you finish." if can_run else
           " You can't run code here, so re-read your files carefully for mistakes before you finish.")
    return ("You build a new project from scratch in an empty workspace: create the files with write_file (complete files, a "
            "README.md with how to run it). For a website make one self-contained index.html with inline CSS and JavaScript: "
            "modern, beautiful, responsive, with real content." + run + common)


def change_view(change):
    """A repository change for the Mini App: target and a unified diff per file."""
    import difflib
    if not change:
        return None
    files = []
    for f in change["files"]:
        patch = list(difflib.unified_diff(f["old"].splitlines(), (f["new"] or "").splitlines(), lineterm="", n=2))[2:]
        files.append({"path": f["path"], "kind": "delete" if f["new"] is None else "new" if not f["blob"] else "edit",
                      "added": sum(1 for line in patch if line.startswith("+")),
                      "removed": sum(1 for line in patch if line.startswith("-")),
                      "patch": "\n".join(patch)[:30000]})
    return {"repo": change["repo"], "branch": change["branch"], "base": change["default"], "message": change["message"],
            "pull_request": change["pull_request"], "files": files}


def history(earlier):
    """The chat so far for the model: each earlier task and what came of it."""
    items = []
    for task in earlier:
        items.append({"role": "user", "content": task["prompt"]})
        note = task.get("summary") or task.get("error") or "(no report)"
        change = task.get("change")
        if change:
            files = ", ".join(f["path"] for f in change["files"])
            note += (f"\n\n[Changes to {files} on branch {change['branch']}: "
                     + ("committed, pull request opened]" if task["status"] == STATUS_APPLIED else
                        "prepared but NOT committed; if you change the code again, include these changes in full]"))
        items.append({"role": "assistant", "content": note[:6000]})
    return items
