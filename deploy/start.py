"""Bootstrap for bot hosting panels without git settings (h1cloud: only "file to run" and "pip packages").

The panel's start file holds one line that fetches and runs this script:
    import urllib.request;exec(urllib.request.urlopen("https://raw.githubusercontent.com/rachimanna/secret-ai/main/deploy/start.py").read())
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


def fetch_code():
    if shutil.which("git"):
        if not os.path.isdir(os.path.join(HOME, ".git")):
            git("init", "-q")
            git("remote", "add", "origin", REPO + ".git")
        if git("fetch", "-q", "--depth", "50", "origin", "main").returncode == 0:
            git("checkout", "-q", "-f", "-B", "main", "origin/main")
            say("Code updated from GitHub (git).")
            return
        say("git fetch failed, downloading a zip instead")
    data = urllib.request.urlopen(REPO + "/archive/refs/heads/main.zip", timeout=120).read()
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


def install():
    say("Installing libraries (the first start takes a few minutes)…")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                             "--prefix", LOCAL, "-r", os.path.join(HOME, "requirements.txt")])
    if result.returncode:
        say("Library install failed — see the lines above.")


def main():
    fetch_code()
    if not os.path.exists(os.path.join(HOME, ".env")):
        shutil.copy(os.path.join(HOME, "deploy", "env.example"), os.path.join(HOME, ".env"))
        say("Created .env — open Files → .env, paste your settings from Render → Environment, save and press Start.")
        return
    install()
    say("Starting the bot…")
    os.execv(sys.executable, [sys.executable, os.path.join(HOME, "bot.py")])


main()
