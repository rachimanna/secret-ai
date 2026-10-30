"""Running outside Render — on a bot hosting panel (Pterodactyl, e.g. h1cloud) or any server without Docker:

- settings come from a .env file next to bot.py (such panels have no place for arbitrary environment variables);
- the HTTP port is the one the panel gives (SERVER_PORT);
- the Mini App needs HTTPS, which such hosts don't give: a free Cloudflare tunnel provides it (a random
  *.trycloudflare.com address on each start, or your own domain with TUNNEL_TOKEN + PUBLIC_URL);
- the bot updates itself from GitHub: every few minutes it checks main, pulls and restarts in place
  (a private repo needs GITHUB_REPO_TOKEN: a fine-grained token with Contents read-only).
"""
import asyncio
import base64
import json
import logging
import os
import platform
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

LOG = logging.getLogger("secret_ai.hosting")
HERE = Path(__file__).resolve().parent
CLOUDFLARED = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
QUICK_URL = re.compile(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")  # not its api. host in errors
UPDATE_EVERY = 300  # seconds
_tunnel = None


def on_render():
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))


def load_env(path=HERE / ".env"):
    """KEY=value lines into the environment; values already set win. Quotes around values are dropped."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and value and not os.environ.get(name):
            os.environ[name] = value


def default_port():
    return os.environ.get("PORT") or os.environ.get("SERVER_PORT") or "10000"


def _cloudflared():
    binary = HERE / "bin" / "cloudflared"
    if binary.exists():
        return binary
    arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
    binary.parent.mkdir(exist_ok=True)
    LOG.info("Downloading cloudflared for the Mini App tunnel")
    urllib.request.urlretrieve(CLOUDFLARED.format(arch=arch), binary)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def start_tunnel(port):
    """HTTPS for the Mini App. Returns the public https:// address, or "" when not needed or impossible."""
    global _tunnel
    if on_render() or os.environ.get("TUNNEL", "1").strip() == "0":
        return ""
    token = os.environ.get("TUNNEL_TOKEN", "").strip()
    if os.environ.get("PUBLIC_URL") and not token:
        return ""  # an address of its own (a reverse proxy, a domain): nothing to do
    try:
        binary = _cloudflared()
        args = [str(binary), "tunnel", "--no-autoupdate"]
        args += ["run", "--token", token] if token else ["--url", f"http://127.0.0.1:{port}"]
        _tunnel = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception as exc:  # the bot still works in chats; only the Mini App waits
        LOG.warning("Mini App tunnel failed to start: %s %s", type(exc).__name__, str(exc)[:200])
        return ""
    if token:  # a named tunnel: its address is the PUBLIC_URL you set
        threading.Thread(target=_drain, args=(_tunnel.stdout,), daemon=True).start()
        return os.environ.get("PUBLIC_URL", "")
    seen = queue.Queue()
    threading.Thread(target=_drain, args=(_tunnel.stdout, seen), daemon=True).start()
    found, deadline = "", time.monotonic() + 40
    while not found and time.monotonic() < deadline:
        try:
            match = QUICK_URL.search(seen.get(timeout=1))
        except queue.Empty:
            if _tunnel.poll() is not None:
                break
            continue
        found = match.group(0) if match else ""
    if found:
        LOG.info("Mini App address: %s", found)
    else:
        LOG.warning("Mini App tunnel gave no address; the Mini App is off until the next start")
    return found


WORKER_SCRIPT = """export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = new URL(TARGET);
    url.protocol = target.protocol; url.hostname = target.hostname; url.port = "";
    return fetch(new Request(url, request), { redirect: "manual" });
  }
};
"""
CF_API = "https://api.cloudflare.com/client/v4"


def stable_address(target):
    """A permanent https://<name>.<subdomain>.workers.dev in front of the changing tunnel address: a tiny Cloudflare
    Worker that forwards everything to target, rewritten on every start. Needs CF_ACCOUNT_ID and a CF_API_TOKEN with
    "Workers Scripts: Edit" (WORKER_NAME, default "secret-ai"). Returns the address, or "" when it can't be done."""
    import httpx

    account, token = os.environ.get("CF_ACCOUNT_ID", "").strip(), os.environ.get("CF_API_TOKEN", "").strip()
    name = re.sub(r"[^a-z0-9-]", "-", os.environ.get("WORKER_NAME", "secret-ai").strip().lower()) or "secret-ai"
    if not target or not account or not token or os.environ.get("STABLE_URL", "1").strip() == "0":
        return ""
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{CF_API}/accounts/{account}/workers"
    try:
        with httpx.Client(timeout=30, headers=headers) as http:
            sub = http.get(f"{base}/subdomain").json()
            subdomain = ((sub or {}).get("result") or {}).get("subdomain")
            if not subdomain:
                LOG.warning("Stable address: open Workers & Pages in Cloudflare once to pick your workers.dev subdomain")
                return ""
            script = "const TARGET = " + json.dumps(target) + ";\n" + WORKER_SCRIPT
            metadata = {"main_module": "worker.js", "compatibility_date": "2024-09-01"}
            upload = http.put(f"{base}/scripts/{name}", files={
                "metadata": (None, json.dumps(metadata), "application/json"),
                "worker.js": ("worker.js", script, "application/javascript+module")})
            if not upload.json().get("success"):
                LOG.warning("Stable address: the Worker upload was refused (the token needs Workers Scripts: Edit): %s",
                            str(upload.json().get("errors"))[:200])
                return ""
            http.post(f"{base}/scripts/{name}/subdomain", json={"enabled": True, "previews_enabled": False})
    except Exception as exc:
        LOG.warning("Stable address failed: %s %s", type(exc).__name__, str(exc)[:200])
        return ""
    address = f"https://{name}.{subdomain}.workers.dev"
    LOG.info("Stable Mini App address: %s", address)
    return _custom_domain(account, headers, name) or address


def _custom_domain(account, headers, worker):
    """CUSTOM_DOMAIN (say secretai.tech, already added to this Cloudflare account) in front of the Worker. The token
    also needs Zone: Read and Workers Routes: Edit. Returns https://<domain>, or "" to keep the workers.dev one."""
    import httpx

    host = os.environ.get("CUSTOM_DOMAIN", "").strip().lower().removeprefix("https://").strip("/")
    if not host:
        return ""
    try:
        with httpx.Client(timeout=30, headers=headers) as http:
            zone, parts = None, host.split(".")
            for start in range(len(parts) - 1):  # the zone is the domain itself or one of its parents
                found = http.get(f"{CF_API}/zones", params={"name": ".".join(parts[start:])}).json().get("result") or []
                if found:
                    zone = found[0]["id"]
                    break
            if not zone:
                LOG.warning("Custom domain %s: add it to Cloudflare first (Add a site) and switch its nameservers", host)
                return ""
            made = http.put(f"{CF_API}/accounts/{account}/workers/domains",
                            json={"hostname": host, "service": worker, "zone_id": zone, "environment": "production"}).json()
            if not made.get("success"):
                LOG.warning("Custom domain %s refused (the token needs Workers Routes: Edit): %s", host,
                            str(made.get("errors"))[:200])
                return ""
    except Exception as exc:
        LOG.warning("Custom domain failed: %s %s", type(exc).__name__, str(exc)[:200])
        return ""
    LOG.info("Mini App address: https://%s", host)
    return f"https://{host}"


def _drain(stream, lines=None):
    for line in stream:  # keep the pipe from filling up
        if lines is not None and lines.qsize() < 1000:
            lines.put(line)


def stop_tunnel():
    if _tunnel and _tunnel.poll() is None:
        _tunnel.terminate()
        try:
            _tunnel.wait(5)
        except subprocess.TimeoutExpired:
            _tunnel.kill()


def _git_auth():
    """With GITHUB_REPO_TOKEN in .env (a read-only token for a private repo), git sends it to github.com as a header:
    it never lands in .git/config or in the remote's address."""
    token = os.environ.get("GITHUB_REPO_TOKEN", "").strip()
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def _git(*args, timeout=120):
    return subprocess.run(["git", *_git_auth(), *args], cwd=HERE, capture_output=True, text=True, timeout=timeout)


STARTED = time.time()


def version():
    """(short commit, its message, its date) of the running code, or Render's commit, or empty strings."""
    sha = os.environ.get("RENDER_GIT_COMMIT", "")
    try:
        found = _git("log", "-1", "--format=%h|%s|%cd", "--date=format:%d.%m %H:%M", timeout=10)
        if found.returncode == 0 and found.stdout.strip():
            return tuple(found.stdout.strip().split("|", 2))
    except Exception:
        pass
    return (sha[:7], "", "")


def updates_enabled():
    return not on_render() and (HERE / ".git").exists() and os.environ.get("SELF_UPDATE", "1").strip() != "0"


def _compile_errors():
    """The first syntax error in the bot's Python files, or "" when they all compile."""
    for path in sorted(HERE.glob("*.py")):
        try:
            compile(path.read_bytes(), str(path), "exec")
        except (SyntaxError, ValueError) as exc:
            return f"{path.name}: {exc}"
    return ""


async def self_update(before_restart):
    """Check GitHub every UPDATE_EVERY seconds; on a new commit pull it, install new requirements and restart."""
    skipped = {}  # commit -> when it failed: tried again only after an hour, not every few minutes
    # The main bot can follow "stable" (only released code) while a test bot follows "main" (everything new).
    branch = os.environ.get("UPDATE_BRANCH", "main").strip() or "main"
    while True:
        await asyncio.sleep(UPDATE_EVERY)
        try:
            fetched = await asyncio.to_thread(_git, "fetch", "-q", "origin", branch)
            if fetched.returncode:
                LOG.warning("Update check failed: %s", (fetched.stderr or "").strip()[-200:] or "git fetch error")
                continue
            head = (await asyncio.to_thread(_git, "rev-parse", "HEAD")).stdout.strip()
            latest = (await asyncio.to_thread(_git, "rev-parse", f"origin/{branch}")).stdout.strip()
            if not latest or head == latest or time.monotonic() - skipped.get(latest, -1e9) < 3600:
                continue
            changed = (await asyncio.to_thread(_git, "diff", "--name-only", head, latest)).stdout.split()
            if "requirements.txt" in changed:
                # The new libraries go in BEFORE the new code: if pip fails, the bot keeps running the old version.
                wanted = (await asyncio.to_thread(_git, "show", f"{latest}:requirements.txt")).stdout
                with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
                    listing.write(wanted)
                target = ["--prefix", str(HERE / ".local")] if (HERE / ".local").exists() else ["--user"]
                try:
                    installed = await asyncio.to_thread(
                        subprocess.run, [sys.executable, "-m", "pip", "install", "-q", "-U", "--no-cache-dir", *target,
                                         "-r", listing.name], capture_output=True, text=True, timeout=600)
                finally:
                    os.unlink(listing.name)
                if installed.returncode:
                    LOG.warning("Update %s skipped: installing its libraries failed, staying on %s: %s", latest[:7], head[:7],
                                (installed.stderr or installed.stdout or "").strip()[-300:])
                    skipped[latest] = time.monotonic()
                    continue
            applied = await asyncio.to_thread(_git, "reset", "-q", "--hard", latest)
            if applied.returncode:
                LOG.warning("Update reset failed; no restart: %s", (applied.stderr or "")[-300:])
                skipped[latest] = time.monotonic()
                continue
            # The new code must at least compile; otherwise go back to the version that was running.
            broken = await asyncio.to_thread(_compile_errors)
            if broken:
                LOG.warning("Update %s skipped: it doesn't compile (%s), staying on %s", latest[:7], broken[:300], head[:7])
                await asyncio.to_thread(_git, "reset", "-q", "--hard", head)
                skipped[latest] = time.monotonic()
                continue
            LOG.info("Updated to %s, restarting", latest[:7])
        except Exception as exc:
            LOG.warning("Self-update check failed: %s %s", type(exc).__name__, str(exc)[:200])
            continue
        try:
            await before_restart()
        except Exception as exc:
            LOG.warning("Before restart: %s", type(exc).__name__)
        stop_tunnel()
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.argv])


TEST_DIR = HERE / "testbot"  # a second checkout of the code, on the newest commits
TEST_PID = HERE / "testbot.pid"


def is_test_bot():
    return os.environ.get("SECRET_AI_TEST", "").strip() == "1"


def _test_alive(pid):
    """True when pid is our test bot (it survives the main bot's restarts: same container, same process id)."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:  # it exited and was our child: reap it
            return False
    except ChildProcessError:  # not our child (the main bot was started again): look at it
        pass
    except OSError:
        return False
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")) == TEST_DIR
    except OSError:
        return False


def _start_test(port):
    if not (TEST_DIR / "bot.py").exists():
        _git("fetch", "-q", "origin", "main")
        made = _git("worktree", "add", "-f", "--detach", str(TEST_DIR), "origin/main")
        if made.returncode:
            LOG.warning("Test bot: could not check out main: %s", (made.stderr or "").strip()[-200:])
            return None
    env = {name: value for name, value in os.environ.items()
           if name not in ("TUNNEL_TOKEN", "PUBLIC_URL", "DATABASE_URL", "PORT", "SERVER_PORT", "CUSTOM_DOMAIN")}  # nothing shared
    libraries = [path for path in sys.path if ".local" in path]  # the libraries the main bot already installed
    env.update(TELEGRAM_TOKEN=os.environ["TEST_TELEGRAM_TOKEN"].strip(), SECRET_AI_TEST="1", UPDATE_BRANCH="main",
               DATABASE_PATH=str(HERE / "test.db"), PORT=os.environ.get("TEST_PORT", "").strip() or str(int(port) + 1),
               WORKER_NAME=(os.environ.get("WORKER_NAME", "secret-ai").strip() or "secret-ai") + "-test",
               LOG_PREFIX="[test] ", PYTHONPATH=os.pathsep.join(libraries + [env.get("PYTHONPATH", "")]).strip(os.pathsep))
    process = subprocess.Popen([sys.executable, "bot.py"], cwd=TEST_DIR, env=env)
    TEST_PID.write_text(str(process.pid))
    LOG.info("Test bot started (branch main, its own database test.db)")
    return process.pid


def start_test_bot(port):
    """With TEST_TELEGRAM_TOKEN in .env, run a test bot next to the main one: it follows main (every new change) while
    the main bot follows stable (UPDATE_BRANCH=stable), so changes are tried before people see them. It keeps running
    across the main bot's restarts and is started again if it falls."""
    if is_test_bot() or not os.environ.get("TEST_TELEGRAM_TOKEN", "").strip() or not (HERE / ".git").exists():
        return

    def watch():
        try:
            pid = int(TEST_PID.read_text())
        except (OSError, ValueError):
            pid = 0
        while True:
            if not pid or not _test_alive(pid):
                try:
                    pid = _start_test(port) or 0
                except Exception as exc:
                    LOG.warning("Test bot failed to start: %s %s", type(exc).__name__, str(exc)[:200])
            time.sleep(60)

    threading.Thread(target=watch, daemon=True).start()
