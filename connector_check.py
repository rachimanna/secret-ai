"""Read-only MCP connectivity check. Never calls an application tool."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

import mcpbridge
from integrations import check_mcp


async def probe(row):
    label, url = check_mcp(row["label"], row["url"])
    host = urlparse(url).hostname
    # The check accepts public hosts only; authentication and tool results aren't returned.
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Сервер должен иметь публичный HTTPS-адрес.")
    headers = {"Authorization": "Bearer " + row["token"]} if row["token"] else {}
    async with httpx.AsyncClient(follow_redirects=False) as http:
        server = mcpbridge.Server(http, {"server_label":label,"server_url":url,"headers":headers})
        try:
            await server.open()
            tools = await server.tools()
            return {"ok": True, "count": len(tools), "tools": [
                {"name": str(t.get("name") or "")[:128], "description": str(t.get("description") or "")[:240]}
                for t in tools[:60] if isinstance(t, dict)]}
        finally:
            if server.session:
                try:
                    await http.delete(url, headers={**headers, "Mcp-Session-Id":server.session}, timeout=3)
                except httpx.HTTPError:
                    pass


def check(row):
    try:
        return asyncio.run(asyncio.wait_for(probe(row), timeout=20))
    except (httpx.HTTPError, mcpbridge.McpError, OSError, TimeoutError, ValueError, TypeError, KeyError):
        # Don't reflect a remote response: it may contain credentials or private data.
        return {"ok": False, "message": "Не удалось получить инструменты. Проверь адрес, ключ и поддержку MCP Streamable HTTP. Для входа через аккаунт используй каталог приложений."}
