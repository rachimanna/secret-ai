import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hosting

ROOT = Path(__file__).resolve().parent.parent


def load_start(home):
    """deploy/start.py without its last line (main()), which would fetch code and start the bot."""
    source = (ROOT / "deploy" / "start.py").read_text(encoding="utf-8")
    body, call = source.rstrip().rsplit("\n", 1)
    assert call == "main()"
    namespace = {"__name__": "start_under_test"}
    with patch("os.getcwd", return_value=home):
        exec(compile(body, "deploy/start.py", "exec"), namespace)
    return namespace


def header_token(args):
    """The token a git command line carries in its github.com header, or None."""
    for index, arg in enumerate(args):
        if arg == "-c" and args[index + 1].startswith("http.https://github.com/.extraheader=AUTHORIZATION: basic "):
            encoded = args[index + 1].rsplit(" ", 1)[1]
            return base64.b64decode(encoded).decode().split(":", 1)[1]
    return None


class HostingTokenTest(unittest.TestCase):
    def run_git(self, environ):
        with patch.dict(os.environ, environ, clear=False), patch("hosting.subprocess.run") as run:
            if "GITHUB_REPO_TOKEN" not in environ:
                os.environ.pop("GITHUB_REPO_TOKEN", None)
            hosting._git("fetch", "-q", "origin", "main")
        return run.call_args.args[0]

    def test_token_goes_into_a_header(self):
        args = self.run_git({"GITHUB_REPO_TOKEN": " github_pat_example "})
        self.assertEqual(header_token(args), "github_pat_example")
        self.assertEqual(args[-4:], ["fetch", "-q", "origin", "main"])
        self.assertFalse(any("github_pat_example" in arg for arg in args))  # only base64 in the header

    def test_without_token_git_is_unchanged(self):
        self.assertEqual(self.run_git({}), ["git", "fetch", "-q", "origin", "main"])


class StartTokenTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GITHUB_REPO_TOKEN", None)
        os.environ.pop("UPDATE_BRANCH", None)

    def write_env(self, text):
        Path(self.home, ".env").write_text(text, encoding="utf-8")

    def test_token_and_branch_come_from_env_file(self):
        self.write_env('UPDATE_BRANCH=stable\nexport GITHUB_REPO_TOKEN="github_pat_file"\n')
        start = load_start(self.home)
        self.assertEqual(start["branch"](), "stable")
        with patch("subprocess.run") as run:
            start["git"]("fetch", "-q", "origin", "stable")
        self.assertEqual(header_token(run.call_args.args[0]), "github_pat_file")

    def test_without_token_branch_defaults_and_git_is_unchanged(self):
        start = load_start(self.home)
        self.assertEqual(start["branch"](), "main")
        self.assertEqual(start["auth"](), "")
        with patch("subprocess.run") as run:
            start["git"]("status")
        self.assertEqual(run.call_args.args[0], ["git", "status"])

    def test_private_zip_comes_from_the_api_with_the_token(self):
        self.write_env("GITHUB_REPO_TOKEN=github_pat_zip\n")
        start = load_start(self.home)
        with patch("shutil.which", return_value=None), \
                patch("urllib.request.urlopen", side_effect=RuntimeError("stop")) as opened:
            with self.assertRaises(RuntimeError):
                start["fetch_code"]()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/rachimanna/secret-ai/zipball/main")
        self.assertEqual(base64.b64decode(request.get_header("Authorization").split()[1]).decode(),
                         "x-access-token:github_pat_zip")


if __name__ == "__main__":
    unittest.main()
