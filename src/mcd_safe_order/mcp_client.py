"""麦当劳 MCP 客户端。

主路径使用官方 `mcp` SDK；若环境中未安装，则回退到内置的 Streamable HTTP
JSON-RPC 实现（仅依赖标准库），保证脚本在最小环境也能真实调用 MCP。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "mcd-allergy-safe-advisor", "version": "1.0.0"}


class McpError(RuntimeError):
    """MCP 调用失败。"""


@dataclass
class ToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> list[str]:
        schema = self.input_schema or {}
        req = schema.get("required")
        return list(req) if isinstance(req, list) else []


@dataclass
class ToolResult:
    content: list[dict[str, Any]] = field(default_factory=list)
    structured: Any = None
    raw: Any = None

    def as_text(self) -> str:
        parts: list[str] = []
        for block in self.content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
        if self.structured is not None:
            return json.dumps(self.structured, ensure_ascii=False, indent=2)
        return json.dumps(self.raw, ensure_ascii=False, indent=2) if self.raw is not None else ""

    def as_json(self) -> Any:
        """尽量把文本载荷解析成 JSON 结构。"""
        if self.structured is not None:
            return self.structured
        text = self.as_text().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
            if fenced:
                try:
                    return json.loads(fenced.group(1))
                except json.JSONDecodeError:
                    pass
            return text


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }


# 常见的 MCP 客户端配置文件，按优先级查找
_CONFIG_CANDIDATES = (
    Path.home() / ".minimax" / "mcp.json",
    Path.home() / ".claude.json",
    Path.home() / ".cursor" / "mcp.json",
    Path.cwd() / "mcp-config.json",
)

_SERVER_NAME_CANDIDATES = ("mcd", "mcd-mcp", "mcd-mcp-server", "mcdchina")


def token_from_config() -> str:
    """从本机 MCP 客户端配置里读取麦当劳 MCP Token。

    优先级：环境变量 MCD_MCP_TOKEN > 本机 MCP 配置文件。
    """
    env_token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if env_token:
        return env_token

    for path in _CONFIG_CANDIDATES:
        try:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        for name in _SERVER_NAME_CANDIDATES:
            entry = servers.get(name)
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "")
            if url and "mcd" not in url.lower() and name not in ("mcd", "mcd-mcp"):
                continue
            auth = (entry.get("headers") or {}).get("Authorization", "")
            token = auth.replace("Bearer ", "").strip()
            if token and "REPLACE" not in token.upper():
                return token
    return ""


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """从 SSE 响应体中抽出所有 JSON 负载。"""
    payloads: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", text):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return payloads


class McdMcpClient:
    """麦当劳 MCP 的最小可用客户端（同步接口）。"""

    def __init__(
        self,
        token: str | None = None,
        url: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.token = token or token_from_config()
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_MCP_URL)
        self.timeout = timeout
        self._session_id: str | None = None
        self._request_id = 0
        if not self.token:
            raise McpError(
                "缺少 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
                "或参考 README 用 scripts/configure_mcp.py 写入配置。"
            )

    # -- 传输层 ----------------------------------------------------------
    def _post(self, payload: dict[str, Any], notify: bool = False) -> list[dict[str, Any]]:
        headers = _headers(self.token)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # pragma: no cover - 网络异常路径
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise McpError(f"MCP HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover
            raise McpError(f"MCP 连接失败: {exc.reason}") from exc
        if notify:
            return []
        return _parse_sse(body) or ([json.loads(body)] if body.strip().startswith("{") else [])

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        for msg in self._post(payload):
            if "error" in msg:
                err = msg["error"]
                raise McpError(f"MCP {method} 失败: {err.get('message', err)}")
            if "result" in msg:
                return msg["result"]
        raise McpError(f"MCP {method} 未返回有效结果")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._post(payload, notify=True)
        except McpError:
            pass  # 通知失败不阻断主流程

    # -- MCP 语义层 ------------------------------------------------------
    def initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[ToolInfo]:
        result = self._request("tools/list")
        tools: list[ToolInfo] = []
        for entry in result.get("tools", []) or []:
            tools.append(
                ToolInfo(
                    name=entry.get("name", ""),
                    description=entry.get("description", "") or "",
                    input_schema=entry.get("inputSchema") or entry.get("input_schema") or {},
                )
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return ToolResult(
            content=result.get("content") or [],
            structured=result.get("structuredContent"),
            raw=result,
        )

    def close(self) -> None:
        if self._session_id:
            self._notify("notifications/cancelled", {"sessionId": self._session_id})
            self._session_id = None


# -- 官方 SDK 路径（可选） -------------------------------------------------


class SdkMcdMcpClient(McdMcpClient):
    """使用官方 mcp SDK 的实现（需要 pip install mcp）。"""

    def __init__(self, token: str | None = None, url: str | None = None, timeout: int = 60) -> None:
        super().__init__(token=token, url=url, timeout=timeout)
        try:
            import mcp  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise McpError("未安装官方 mcp SDK") from exc

    def _run(self, coro):  # pragma: no cover - 依赖 SDK
        return asyncio.run(coro)

    def initialize(self) -> dict[str, Any]:  # pragma: no cover
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _do() -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {self.token}"}
            async with streamablehttp_client(self.url, headers=headers) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self._session_id = getattr(session, "_session_id", None)
                    return init.model_dump() if hasattr(init, "model_dump") else dict(init)

        return self._run(_do())

    def list_tools(self) -> list[ToolInfo]:  # pragma: no cover
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _do() -> list[ToolInfo]:
            headers = {"Authorization": f"Bearer {self.token}"}
            async with streamablehttp_client(self.url, headers=headers) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    return [
                        ToolInfo(
                            name=t.name,
                            description=t.description or "",
                            input_schema=getattr(t, "inputSchema", {}) or {},
                        )
                        for t in listing.tools
                    ]

        return self._run(_do())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:  # pragma: no cover
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _do() -> ToolResult:
            headers = {"Authorization": f"Bearer {self.token}"}
            async with streamablehttp_client(self.url, headers=headers) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(name, arguments or {})
                    blocks = []
                    for block in getattr(res, "content", []) or []:
                        blocks.append(
                            {
                                "type": getattr(block, "type", "text"),
                                "text": getattr(block, "text", "") or "",
                            }
                        )
                    structured = getattr(res, "structuredContent", None)
                    raw = res.model_dump() if hasattr(res, "model_dump") else None
                    return ToolResult(content=blocks, structured=structured, raw=raw)

        return self._run(_do())


def create_client(token: str | None = None, url: str | None = None, timeout: int = 60) -> McdMcpClient:
    """优先使用官方 SDK，不可用时回退到标准库实现。"""
    try:
        return SdkMcdMcpClient(token=token, url=url, timeout=timeout)
    except McpError:
        return McdMcpClient(token=token, url=url, timeout=timeout)
