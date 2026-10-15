"""Models through the Cursor Agent CLI (cursor-agent) in headless mode, with the owner's CURSOR_API_KEY.

The CLI is installed once next to the bot (https://cursor.com/install puts it into ~/.local/bin). Each answer runs
`cursor-agent -p --trust --output-format stream-json --model <model>` in an empty folder (--trust skips the workspace
question; no --force: it can't change any files),
the conversation goes in as one prompt, and the streamed assistant text comes back. It is a personal subscription, so
by default only the bot's owner sees these models (CURSOR_PUBLIC=1 opens them to everyone, at the owner's risk).
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

LOG = logging.getLogger("secret_ai.cursor")
PREFIX = "cursor:"
INSTALL = "curl https://cursor.com/install -fsS | bash"
TIMEOUT = 240


class CursorError(Exception):
    pass


def models():
    """The Cursor models offered in the picker (CURSOR_MODELS, comma-separated)."""
    wanted = os.environ.get("CURSOR_MODELS", "grok-4.7-fast").split(",")
    return [PREFIX + m.strip() for m in wanted if m.strip()] if os.environ.get("CURSOR_API_KEY", "").strip() else []


def public():
    return os.environ.get("CURSOR_PUBLIC", "0").strip() == "1"


def binary():
    for name in ("cursor-agent", "agent"):
        found = shutil.which(name)
        if found and (name == "cursor-agent" or "cursor" in Path(found).resolve().as_posix()):
            return found
    local = Path.home() / ".local" / "bin" / "cursor-agent"
    return str(local) if local.exists() else None


async def install():
    """Install the CLI once if it's missing; returns its path or None."""
    if binary():
        return binary()
    LOG.info("Installing the Cursor CLI")
    process = await asyncio.create_subprocess_shell(INSTALL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(process.communicate(), 300)
    except asyncio.TimeoutError:
        process.kill()
        LOG.warning("Cursor CLI install timed out")
        return None
    path = binary()
    if not path:
        LOG.warning("Cursor CLI install failed: %s", (err or b"").decode(errors="replace")[-300:])
    else:
        LOG.info("Cursor CLI ready: %s", path)
    return path


def prompt_from(instructions, items):
    """System instructions and the conversation as one prompt (the CLI takes text only)."""
    lines = [instructions.strip(), "", "Conversation so far (answer the last user message; reply only with your answer):"]
    for item in items:
        if "role" not in item:
            continue
        content = item["content"]
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "input_text") + \
                      (" [a photo was attached, but you can't see it here]" if any(p.get("type") == "input_image" for p in content) else "")
        lines.append(f"{'User' if item['role'] == 'user' else 'Assistant'}: {content}")
    return "\n".join(lines)


async def answer(model, instructions, items, on_text):
    """Run the CLI and stream the assistant's text into on_text(text_so_far); returns the final text."""
    path = binary() or await install()
    if not path:
        raise CursorError("the Cursor CLI is not installed")
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    folder = tempfile.mkdtemp(prefix="cursor-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", folder), "CURSOR_API_KEY": key,
           "LANG": "C.UTF-8", "NO_COLOR": "1"}
    args = [path, "-p", "--trust", "--output-format", "stream-json", "--model", model[len(PREFIX):], prompt_from(instructions, items)]
    process = await asyncio.create_subprocess_exec(*args, cwd=folder, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                   limit=4 * 1024 * 1024)
    text, final, error = "", None, ""
    try:
        async def read():
            nonlocal text, final, error
            async for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "assistant":
                    piece = "".join(p.get("text", "") for p in (event.get("message") or {}).get("content") or [] if isinstance(p, dict))
                    # Events may carry the whole message so far or just the new part.
                    text = piece if piece.startswith(text) else text + piece
                    await on_text(text)
                elif kind == "result":
                    if event.get("is_error"):
                        error = str(event.get("result") or "error")
                    else:
                        final = str(event.get("result") or "")
        await asyncio.wait_for(read(), TIMEOUT)
        await asyncio.wait_for(process.wait(), 10)
    except asyncio.TimeoutError:
        raise CursorError("the Cursor agent took too long") from None
    except asyncio.CancelledError:
        raise
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        shutil.rmtree(folder, ignore_errors=True)
    if error or (process.returncode and not (final or text)):
        detail = error or (await process.stderr.read()).decode(errors="replace")[-300:]
        raise CursorError(detail.strip() or f"exit code {process.returncode}")
    return (final if final is not None and final.strip() else text).strip()
