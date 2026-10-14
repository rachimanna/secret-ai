"""Running outside Render — on a bot hosting panel (Pterodactyl, e.g. h1cloud) or any server without Docker:

- settings come from a .env file next to bot.py (such panels have no place for arbitrary environment variables);
- the HTTP port is the one the panel gives (SERVER_PORT);
- the Mini App needs HTTPS, which such hosts don't give: a free Cloudflare tunnel provides it (a random
  *.trycloudflare.com address on each start, or your own domain with TUNNEL_TOKEN + PUBLIC_URL);
- the bot updates itself from GitHub: every few minutes it checks main, pulls and restarts in place.
"""
import asyncio
import logging
import os
import platform
import queue
import re
import stat
import subprocess
import sys
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


def _git(*args, timeout=120):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=timeout)


def updates_enabled():
    return not on_render() and (HERE / ".git").exists() and os.environ.get("SELF_UPDATE", "1").strip() != "0"


async def self_update(before_restart):
    """Check GitHub every UPDATE_EVERY seconds; on a new commit pull it, install new requirements and restart."""
    while True:
        await asyncio.sleep(UPDATE_EVERY)
        try:
            fetched = await asyncio.to_thread(_git, "fetch", "-q", "origin", "main")
            if fetched.returncode:
                continue
            head = (await asyncio.to_thread(_git, "rev-parse", "HEAD")).stdout.strip()
            latest = (await asyncio.to_thread(_git, "rev-parse", "origin/main")).stdout.strip()
            if not latest or head == latest:
                continue
            changed = (await asyncio.to_thread(_git, "diff", "--name-only", head, latest)).stdout.split()
            await asyncio.to_thread(_git, "reset", "-q", "--hard", "origin/main")
            if "requirements.txt" in changed:
                target = ["--prefix", str(HERE / ".local")] if (HERE / ".local").exists() else ["--user"]
                await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "pip", "install", "-q", "-U", *target,
                                                         "-r", str(HERE / "requirements.txt")], timeout=600)
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
