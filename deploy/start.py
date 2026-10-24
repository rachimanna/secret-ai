"""Bootstrap for bot hosting panels without git settings (h1cloud: only "file to run" and "pip packages").

The panel's start file holds one line that fetches and runs this script:
    import urllib.request
import urllib.parse;exec(urllib.request.urlopen("https://raw.githubusercontent.com/rachimanna/secret-ai/main/deploy/start.py").read())
It brings the bot's code from GitHub (git when available, else a zip), installs requirements, makes a .env template
on the first run and starts bot.py — which then keeps itself updated (hosting.py).
"""
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
    return subprocess.run(["git", *args], cwd=HOME, capture_output=True, text=True)


def branch():
    """UPDATE_BRANCH from .env: the main bot can run "stable" while a test bot tries "main"."""
    configured = os.environ.get("UPDATE_BRANCH", "").strip()
    if configured:
        return configured
    try:
        with open(os.path.join(HOME, ".env"), encoding="utf-8") as env:
            for line in env:
                name, _, value = line.strip().partition("=")
                if name.strip() == "UPDATE_BRANCH" and value.strip().strip("\"'"):
                    return value.strip().strip("\"'")
    except OSError:
        pass
    return "main"


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
    data = urllib.request.urlopen(REPO + "/archive/refs/heads/" + urllib.parse.quote(wanted, safe="") + ".zip", timeout=120).read()
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
