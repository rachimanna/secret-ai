"""Bootstrap for bot hosting panels without git settings (h1cloud: only "file to run" and "pip packages").

The panel's start file holds one line that fetches and runs this script:
    import urllib.request
import urllib.parse;exec(urllib.request.urlopen("https://raw.githubusercontent.com/rachimanna/secret-ai/main/deploy/start.py").read())
It brings the bot's code from GitHub (git when available, else a zip), installs requirements, makes a .env template
on the first run and starts bot.py — which then keeps itself updated (hosting.py).
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

REPO = "https://github.com/rachimanna/secret-ai"
HOME = os.getcwd()
LOCAL = os.path.join(HOME, ".local")


def say(text):
    print(f"[secret-ai] {text}", flush=True)


def git(*args):
    header = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth()}"] if auth() else []
    return subprocess.run(["git", *header, *args], cwd=HOME, capture_output=True, text=True)


def setting(name):
    """A setting from the environment or, on the panel, from .env (the bot itself is not running yet)."""
    configured = os.environ.get(name, "").strip()
    if configured:
        return configured
    try:
        with open(os.path.join(HOME, ".env"), encoding="utf-8") as env:
            for line in env:
                key, _, value = line.strip().partition("=")
                if key.strip().removeprefix("export ").strip() == name and value.strip().strip("\"'"):
                    return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def branch():
    """UPDATE_BRANCH from .env: the main bot can run "stable" while a test bot tries "main"."""
    return setting("UPDATE_BRANCH") or "main"


def auth():
    """GITHUB_REPO_TOKEN from .env for a private repo: sent as a header, never written into .git/config."""
    token = setting("GITHUB_REPO_TOKEN")
    return base64.b64encode(f"x-access-token:{token}".encode()).decode() if token else ""


def fetch_code():
    if shutil.which("git"):
        wanted = branch()
        if not os.path.isdir(os.path.join(HOME, ".git")):
            git("init", "-q")
            git("remote", "add", "origin", REPO + ".git")
        if git("fetch", "-q", "--depth", "50", "origin", wanted).returncode == 0:
            checked = git("checkout", "-q", "-f", "-B", wanted, f"origin/{wanted}")
            if checked.returncode:
                raise RuntimeError("Cannot check out requested version: " + checked.stderr[-300:])
            say("Code updated from GitHub (git).")
            return
        say("git fetch failed, downloading a zip instead")
    wanted = branch()
    if auth():  # a private repo: its archive comes from the API, with the token
        url = "https://api.github.com/repos/" + REPO.split("github.com/")[1] + "/zipball/" + urllib.parse.quote(wanted, safe="")
        request = urllib.request.Request(url, headers={"Authorization": "Basic " + auth()})
    else:
        request = REPO + "/archive/refs/heads/" + urllib.parse.quote(wanted, safe="") + ".zip"
    data = urllib.request.urlopen(request, timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        prefix = archive.namelist()[0].split("/")[0] + "/"
        for name in archive.namelist():
            target = os.path.join(HOME, name[len(prefix):])
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as out:
                    out.write(archive.read(name))
    say("Code downloaded from GitHub (zip; automatic updates need git).")


# Heavy leftovers that don't fit a small (4 GB) disk: the pip download cache and a half-installed local speech model.
JUNK = [os.path.join(HOME, ".cache", "pip"), os.path.join(HOME, "models")]
HEAVY = ("ctranslate2", "onnxruntime", "faster_whisper", "faster-whisper", "av-", "av.libs", "ctranslate2.libs")


def free_space():
    """Delete caches and leftovers of packages the bot no longer uses, and say how much room there is."""
    for path in JUNK:
        shutil.rmtree(path, ignore_errors=True)
    for root, folders, _ in os.walk(LOCAL):
        for name in list(folders):
            if name == "av" or name.lower().startswith(HEAVY):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                folders.remove(name)
    try:
        free = shutil.disk_usage(HOME).free // (1024 * 1024)
        say(f"Free disk space: {free} MB" + (" — very little, delete big files in Files" if free < 300 else ""))
    except OSError:
        pass


def install():
    say("Installing libraries (the first start takes a few minutes)…")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-cache-dir",
                             "--prefix", LOCAL, "-r", os.path.join(HOME, "requirements.txt")])
    if result.returncode:
        raise RuntimeError("Library install failed — see the lines above.")


def main():
    free_space()  # first: a full disk breaks the code update too
    fetch_code()
    if not os.path.exists(os.path.join(HOME, ".env")):
        shutil.copy(os.path.join(HOME, "deploy", "env.example"), os.path.join(HOME, ".env"))
        say("Created .env — open Files → .env, paste your settings from Render → Environment, save and press Start.")
        return
    install()
    say("Starting the bot…")
    paths = [os.path.join(LOCAL, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
    os.execv(sys.executable, [sys.executable, os.path.join(HOME, "bot.py")])


main()
