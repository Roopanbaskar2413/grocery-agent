"""A minimal MCP client over streamable HTTP (JSON-RPC 2.0 `tools/call`), used by the real Instamart
adapter. The wire shape follows the general MCP spec; Swiggy's own reference docs only show each
tool's name and a one-line summary, not the raw JSON envelope, so the exact response shape
(`structuredContent` vs a JSON-encoded text block) is unverified against the real server. Tested here
only against a scripted stand-in. See DRYRUN.md.
"""
import itertools
import json
import logging
from typing import Callable, Optional

import httpx

log = logging.getLogger("agent.mcp_client")


class McpError(RuntimeError):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class McpClient:
    def __init__(self, base_url: str, token_provider: Callable[[], str], http: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self.base_url = base_url
        self.token_provider = token_provider
        self.http = http or httpx.Client(timeout=timeout)
        self._ids = itertools.count(1)

    def call(self, tool_name: str, **arguments) -> dict:
        token = self.token_provider()
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}}
        r = self.http.post(self.base_url, json=body,
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:
            err = payload["error"]
            raise McpError(err.get("message", "MCP error"), code=err.get("code"), data=err.get("data"))
        result = payload.get("result", {})
        if result.get("isError"):
            raise McpError(_first_text(result) or f"{tool_name} reported an error")
        return _tool_output(result)


def _first_text(result: dict) -> str:
    for item in result.get("content", []):
        if item.get("type") == "text":
            return item["text"]
    return ""


def _tool_output(result: dict) -> dict:
    if "structuredContent" in result:
        return result["structuredContent"]
    text = _first_text(result)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
    return {}
