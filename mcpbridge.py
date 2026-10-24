"""Remote MCP servers (connected apps: Gmail via Pipedream, Notion, a user's own MCP…) for models that only know plain
function tools — Claude and others on chat-completions gateways. The OpenAI Responses API calls MCP servers by itself;
for everyone else the bot does it: lists each server's tools, offers them as functions and runs the calls.

MCP over "streamable HTTP": JSON-RPC posted to the server URL, answers as JSON or as server-sent events.
"""
import asyncio
import hashlib
import json
import logging
import re
import time

import httpx

LOG = logging.getLogger("secret_ai.mcp")
PROTOCOL = "2025-03-26"
CACHE_SECONDS = 600
MAX_TOOLS = 60  # all servers together, so the request stays small
MAX_OUTPUT = 12_000
_cache = {}  # (url, headers) -> (expires, session id, [tools])


class McpError(Exception):
    def __init__(self, text, status=None):
        super().__init__(text)
        self.status = status  # the HTTP status, when the server answered with an error


def _not_done(exc):
    """Errors after which the tool surely did NOT run, so calling again can't do anything twice: the connection never
    opened, or the server rejected the session (an expired session: HTTP 404, or an error that names the session).
    A timeout or a lost answer is never one of them: the action may already have happened."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(exc, McpError) and (exc.status == 404 or (exc.status in (400, 401) and "session" in str(exc).lower()))


def _key(server):
    return server["server_url"], json.dumps(server.get("headers") or {}, sort_keys=True)


def _name(label, tool):
    raw = f"{label}__{tool}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return normalized if len(normalized) <= 64 and normalized == raw else normalized[:51] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


class Server:
    def __init__(self, http, server):
        self.http, self.url, self.label = http, server["server_url"], server.get("server_label") or "app"
        self.headers = dict(server.get("headers") or {})
        self.session, self.next_id = None, 0

    async def _post(self, method, params=None, notify=False):
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self.next_id += 1
            body["id"] = self.next_id
        response = await self.http.post(self.url, json=body, headers=headers, timeout=60)
        self.session = response.headers.get("mcp-session-id") or self.session
        if notify:
            return None
        if response.status_code >= 400:
            raise McpError(f"{method}: HTTP {response.status_code} {response.text[:120]}", response.status_code)
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                        if line.startswith("data:") and line[5:].strip().startswith("{")]
        else:
            messages = [response.json()]
        reply = next((m for m in messages if m.get("id") == body["id"]), messages[-1] if messages else {})
        if reply.get("error"):
            raise McpError(f"{method}: {str(reply['error'].get('message') or reply['error'])[:200]}")
        return reply.get("result") or {}

    async def open(self):
        await self._post("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                        "clientInfo": {"name": "secret-ai", "version": "1"}})
        await self._post("notifications/initialized", notify=True)

    async def tools(self):
        found, cursor = [], None
        for _ in range(5):
            result = await self._post("tools/list", {"cursor": cursor} if cursor else {})
            found += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return found

    async def call(self, tool, arguments):
        result = await self._post("tools/call", {"name": tool, "arguments": arguments or {}})
        parts = []
        for item in result.get("content") or []:
            if item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif item.get("type") == "resource":
                parts.append(json.dumps(item.get("resource"), ensure_ascii=False)[:2000])
            else:
                parts.append(f"[{item.get('type')}]")
        text = "\n".join(parts) or json.dumps(result.get("structuredContent") or {}, ensure_ascii=False)
        if result.get("isError"):
            text = "Error: " + text
        return text[:MAX_OUTPUT]


class Bridge:
    """The tools of several MCP servers as chat-completions functions."""

    def __init__(self):
        self.functions, self.routes = [], {}  # function name -> (Server, tool name, app label)

    def handles(self, name):
        return name in self.routes

    def app(self, name):
        return self.routes[name][2]

    async def call(self, name, arguments):
        server, tool, _ = self.routes[name]
        try:
            return await server.call(tool, arguments)
        except Exception as exc:
            if not _not_done(exc):  # it may have run already (a document made, a message sent): never repeat it
                raise
            # the session expired or the connection never opened: open a new one and try once more
            await server.open()
            _cache.pop(_key({"server_url": server.url, "headers": server.headers}), None)
            return await server.call(tool, arguments)


async def load(http, servers):
    """A Bridge over the given MCP tool definitions ({type: mcp, server_label, server_url, headers}). Servers that
    don't answer are skipped (logged), so one broken app never breaks the answer."""
    bridge = Bridge()

    async def one(server_def):
        server = Server(http, server_def)
        cached = _cache.get(_key(server_def))
        if cached and cached[0] > time.monotonic():
            server.session = cached[1]
            return server_def, server, cached[2]
        await server.open()
        tools = await server.tools()
        if len(_cache) >= 256:
            _cache.clear()
        _cache[_key(server_def)] = (time.monotonic() + CACHE_SECONDS, server.session, tools)
        return server_def, server, tools

    results = await asyncio.gather(*(one(s) for s in servers), return_exceptions=True)
    available = []
    for server_def, result in zip(servers, results):
        if isinstance(result, Exception):
            LOG.warning("MCP server %s unavailable: %s %s", server_def.get("server_label"), type(result).__name__,
                        str(result)[:200])
            continue
        _, server, tools = result
        available.append((server, [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"),str) and t["name"]]))
    # Give every connected app a turn before large servers consume the context budget.
    for index in range(max((len(tools) for _, tools in available), default=0)):
        for server, tools in available:
            if len(bridge.functions) >= MAX_TOOLS:
                return bridge
            if index >= len(tools):
                continue
            tool = tools[index]
            name = _name(server.label, tool["name"])
            if name in bridge.routes:
                continue
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
            if not isinstance(schema,dict) or schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            bridge.routes[name] = (server, tool["name"], server.label)
            bridge.functions.append({"type": "function", "function": {
                "name": name, "description": f"[{server.label}] " + str(tool.get("description") or tool["name"])[:900],
                "parameters": schema}})
    return bridge
