"""The model's light Markdown as Telegram formatting: (plain text, MessageEntity list), never parse_mode.

Entities instead of HTML/MarkdownV2 parse modes: no character in an answer can make Telegram refuse the message.

    # Heading / **bold** / __bold__  -> bold          *italic* / _italic_ -> italic
    `code`                         -> code          ```lang ... ```     -> pre (with its language)
    ~~strike~~                     -> strikethrough ||spoiler||         -> spoiler
    [text](https://…)              -> a link on the words
    - item / * item / + item       -> • item        > quote lines       -> a quote (long ones fold)
    --- on its own line            -> a thin rule

partial=True is for a reply still being typed: an unclosed marker is hidden instead of flashing as raw ** or `.
"""
import re

from telegram import MessageEntity

QUOTE_FOLD_LINES = 6  # a longer quote is shown folded, Telegram's "show more"
RULE = "────────"

FENCE = re.compile(r"```([^\n`]*)\n?([\s\S]*?)(?:```|\Z)")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
RULE_LINE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
URL = re.compile(r"https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"»]")
LINK = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")
# marker -> entity type; the longest markers first so ** is not read as two *
PAIRS = [("**", MessageEntity.BOLD), ("__", MessageEntity.BOLD), ("~~", MessageEntity.STRIKETHROUGH),
         ("||", MessageEntity.SPOILER), ("*", MessageEntity.ITALIC), ("_", MessageEntity.ITALIC)]


def utf16_len(text):
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


class _Out:
    """Text being built with its entities; offsets and lengths in UTF-16 units, as Telegram counts them."""

    def __init__(self):
        self.parts, self.size, self.spans = [], 0, []

    def add(self, text):
        self.parts.append(text)
        self.size += utf16_len(text)

    def wrap(self, kind, start, **extra):
        if self.size > start:
            self.spans.append((kind, start, self.size - start, extra))

    def text(self):
        return "".join(self.parts)


def _closing(text, index, marker):
    """Where the marker opened at index closes on this line, or -1. Emphasis hugs words: no "2 * 3 * 4"."""
    start = index + len(marker)
    if start >= len(text) or text[start].isspace():
        return -1
    if marker == "_" and index > 0 and text[index - 1].isalnum():
        return -1  # snake_case_names stay as they are
    search = start
    while True:
        end = text.find(marker, search)
        if end < 0:
            return -1
        if end > start and not text[end - 1].isspace() and text[end:end + len(marker) * 2] != marker * 2 \
                and not (marker == "_" and end + 1 < len(text) and text[end + 1].isalnum()):
            return end
        search = end + 1


def _inline(out, text, partial):
    index = 0
    plain = []

    def flush():
        if plain:
            out.add("".join(plain))
            plain.clear()

    while index < len(text):
        char = text[index]
        if char == "`":
            end = text.find("`", index + 1)
            if end > index + 1:
                flush()
                start = out.size
                out.add(text[index + 1:end])
                out.wrap(MessageEntity.CODE, start)
                index = end + 1
                continue
            if end < 0 and partial:  # the code is still being typed
                flush()
                start = out.size
                out.add(text[index + 1:])
                out.wrap(MessageEntity.CODE, start)
                return
        if char == "[":
            link = LINK.match(text, index)
            if link:
                flush()
                start = out.size
                _inline(out, link.group(1), partial)
                out.wrap(MessageEntity.TEXT_LINK, start, url=link.group(2))
                index = link.end()
                continue
        if char == "h":
            url = URL.match(text, index)
            if url and (index == 0 or not text[index - 1].isalnum()):
                plain.append(url.group(0))  # never read _ or * inside an address as formatting
                index = url.end()
                continue
        for marker, kind in PAIRS:
            if text.startswith(marker, index):
                end = _closing(text, index, marker)
                if end > 0:
                    flush()
                    start = out.size
                    _inline(out, text[index + len(marker):end], partial)
                    out.wrap(kind, start)
                    index = end + len(marker)
                    break
                if partial and marker in ("**", "__", "~~", "||") and text.find(marker, index + len(marker)) < 0:
                    # The closing half hasn't arrived yet: no raw marker, the words already look as they will.
                    flush()
                    start = out.size
                    _inline(out, text[index + len(marker):], partial)
                    out.wrap(kind, start)
                    return
        else:
            plain.append(char)
            index += 1
    flush()


def _lines(out, block, partial):
    lines = block.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        quote = QUOTE.match(line)
        if quote:
            start = out.size
            count = 0
            while index < len(lines) and (match := QUOTE.match(lines[index])):
                if count:
                    out.add("\n")
                _inline(out, match.group(1), partial)
                count += 1
                index += 1
            kind = MessageEntity.EXPANDABLE_BLOCKQUOTE if count > QUOTE_FOLD_LINES else MessageEntity.BLOCKQUOTE
            out.wrap(kind, start)
            if index < len(lines):
                out.add("\n")
            continue
        heading = HEADING.match(line)
        bullet = BULLET.match(line)
        if heading:
            start = out.size
            _inline(out, heading.group(1).strip("*_ "), partial)
            out.wrap(MessageEntity.BOLD, start)
        elif RULE_LINE.match(line):
            out.add(RULE)
        elif bullet:
            depth = len(bullet.group(1).replace("\t", "  ")) // 2
            out.add("  " * depth + ("◦ " if depth else "• "))
            _inline(out, bullet.group(2), partial)
        else:
            _inline(out, line, partial)
        index += 1
        if index < len(lines):
            out.add("\n")


def render(text, partial=False):
    """(plain text, [MessageEntity]) for Telegram from the model's Markdown."""
    out = _Out()
    position = 0
    for fence in FENCE.finditer(text or ""):
        if fence.start() > position:
            _lines(out, text[position:fence.start()], partial)
        closed = fence.group(0).endswith("```") and len(fence.group(0)) > 3
        if not closed and not partial:
            _lines(out, fence.group(0), partial)  # a stray ``` in a finished answer stays as it was
        else:
            code = fence.group(2).rstrip("\n")
            start = out.size
            out.add(code or " ")
            language = fence.group(1).strip().split(" ")[0]
            out.wrap(MessageEntity.PRE, start, **({"language": language} if language else {}))
        position = fence.end()
    if position < len(text or ""):
        _lines(out, text[position:], partial)
    plain = out.text()
    entities = [MessageEntity(kind, offset, length, **extra) for kind, offset, length, extra in out.spans]
    return plain, sorted(entities, key=lambda e: (e.offset, -e.length))


def strip(text):
    """Only the words: for voice replies, history notes and anything that can't carry formatting."""
    return render(text)[0]
