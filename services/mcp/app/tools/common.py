"""Shared HTTP helper for kernel MCP tools."""

from __future__ import annotations

import json

import aiohttp


async def http_request(
    method: str,
    url: str,
    headers: dict | None = None,
    params: dict | None = None,
    body: dict | None = None,
    timeout: int = 10,
) -> str:
    """Generic async HTTP request. Returns formatted JSON string or raw text."""
    _headers = dict(headers) if headers else {}

    try:
        async with aiohttp.ClientSession() as session:
            kwargs: dict = {
                "headers": _headers,
                "timeout": aiohttp.ClientTimeout(total=timeout),
            }
            if params:
                kwargs["params"] = params
            if method in ("POST", "PUT", "PATCH") and body:
                kwargs["json"] = body
                _headers.setdefault("Content-Type", "application/json")

            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                if not (200 <= resp.status < 300):
                    return json.dumps({"error": f"HTTP {resp.status}", "detail": text})
                if not text:
                    return json.dumps({"status": "success", "http_code": resp.status})
                try:
                    return json.dumps(json.loads(text), indent=2)
                except json.JSONDecodeError:
                    return text

    except aiohttp.ClientConnectorError:
        return json.dumps({"error": "Connection refused", "url": url})
    except Exception as e:
        return json.dumps({"error": str(e)})
