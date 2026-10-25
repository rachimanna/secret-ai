"""Code in answers goes out as files: fenced blocks become documents, short snippets stay in the text."""
import re

FENCE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
MIN_FILE_LINES = 8  # shorter snippets read fine inline
MAX_FILES = 10
EXTENSIONS = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "typescript": ".ts", "ts": ".ts", "tsx": ".tsx",
    "jsx": ".jsx", "html": ".html", "css": ".css", "json": ".json", "yaml": ".yml", "yml": ".yml", "toml": ".toml",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh", "powershell": ".ps1", "ps1": ".ps1", "bat": ".bat",
    "cmd": ".bat", "sql": ".sql", "java": ".java", "kotlin": ".kt", "kt": ".kt", "swift": ".swift", "go": ".go",
    "rust": ".rs", "rs": ".rs", "c": ".c", "cpp": ".cpp", "c++": ".cpp", "csharp": ".cs", "cs": ".cs", "php": ".php",
    "ruby": ".rb", "rb": ".rb", "lua": ".lua", "dart": ".dart", "r": ".r", "xml": ".xml", "dockerfile": "Dockerfile",
    "makefile": "Makefile", "ini": ".ini", "env": ".env", "markdown": ".md", "md": ".md", "vue": ".vue", "svelte": ".svelte",
    "scss": ".scss", "svg": ".svg", "csv": ".csv", "text": ".txt", "txt": ".txt", "luau": ".lua",
}
SAFE_NAME = re.compile(r"^[\w.\-]{1,80}$")


def _name(info, code, taken):
    words = info.split()
    lang = words[0].lower() if words else ""
    name = next((w for w in words[1:] if SAFE_NAME.match(w)), "")
    if not name and words and "." in words[0] and SAFE_NAME.match(words[0]):
        name, lang = words[0], ""
    if not name:
        ext = EXTENSIONS.get(lang, ".txt")
        if ext in ("Dockerfile", "Makefile"):
            name = ext
        elif ext == ".html" or "<html" in code[:500].lower():
            name = "index.html"
        else:
            name = ("main" if ext in (".py", ".js", ".ts", ".go", ".rs", ".c", ".cpp", ".java", ".kt", ".cs") else "code") + ext
    base, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    candidate, number = name, 2
    while candidate in taken:
        candidate = f"{base}-{number}{dot}{ext}" if dot else f"{name}-{number}"
        number += 1
    taken.add(candidate)
    return candidate


def extract_files(answer):
    """Split an answer into (text, [(file name, bytes)]): long or named code blocks become files."""
    files, taken = [], set()

    def replace(match):
        info, code = match.group(1).strip(), match.group(2).rstrip()
        if not code.strip():
            return ""
        words = info.split()
        named = len(words) > 1 or (words and "." in words[0])
        lines = code.count("\n") + 1
        is_site = (words[0].lower() == "html" if words else False) or "<html" in code[:500].lower()
        if len(files) < MAX_FILES and (named or lines >= MIN_FILE_LINES or is_site):
            name = _name(info, code, taken)
            files.append((name, (code + "\n").encode("utf-8")))
            return f"📎 {name}"
        return match.group(0)  # a short snippet stays in the message

    text = FENCE.sub(replace, answer)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, files


def site_file(files):
    """The HTML page among the files (index.html first), or None."""
    pages = [item for item in files if item[0].lower().endswith((".html", ".htm"))]
    pages.sort(key=lambda item: item[0].lower() != "index.html")
    return pages[0] if pages else None
