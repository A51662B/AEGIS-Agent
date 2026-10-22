# -*- coding: utf-8 -*-
"""MCP 管理器：启动一个 MCP 服务端，拿工具列表，调用工具。

只做三件事，够用就行：

    mgr = MCPManager(command=sys.executable, args=["demo_server.py"])
    await mgr.start()                     # 连接 + initialize + 拿工具列表
    tools = mgr.tools                     # [MCPTool(name, description, input_schema)]
    result = await mgr.call("add", {"a": 1, "b": 2})
    await mgr.stop()

为什么要用 AsyncExitStack
------------------------
stdio_client 和 ClientSession 都是「进入上下文 → 用 → 退出」的对象，
而且必须一直保持打开（关掉连接，session 就废了）。
AsyncExitStack 让「手动 start / 手动 stop」能安全地管住这两个上下文。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class MCPTool:
    """一个工具的描述（从服务端 tools/list 拿到的）。"""

    name: str
    description: str
    input_schema: dict = field(default_factory=dict)

    def to_openai_function(self) -> dict:
        """转成 AEGIS Toolregistry 用的格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class MCPManager:
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        name: str = "mcp",
        timeout: float = 60.0,
    ):
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self.name = name
        self.timeout = timeout

        self.tools: list[MCPTool] = []
        self.server_info: dict = {}
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> "MCPManager":
        """连上服务端并取回工具清单。"""
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))

        info = await self._session.initialize()
        server = getattr(info, "server_info", None)
        self.server_info = {
            "name": getattr(server, "name", ""),
            "version": getattr(server, "version", ""),
            "protocol": getattr(info, "protocol_version", ""),
        }

        listed = await self._session.list_tools()
        self.tools = [
            MCPTool(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.input_schema or {}),
            )
            for t in listed.tools
        ]
        return self

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def __aenter__(self) -> "MCPManager":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # -- 调用 --------------------------------------------------------------

    async def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict:
        """调用一个工具。

        返回统一结构：
            {"ok": bool, "is_error": bool, "text": str, "data": dict|None}
        - text  给模型看的原始文本
        - data  服务端返回的结构化数据（如果有）
        超时会抛 asyncio.TimeoutError，由调用方决定怎么处理。
        """
        if self._session is None:
            raise RuntimeError("MCP 还没启动，先 await mgr.start()")

        result = await asyncio.wait_for(
            self._session.call_tool(tool_name, arguments or {}),
            timeout=self.timeout,
        )
        return self._normalize(result)

    async def ping(self) -> bool:
        if self._session is None:
            return False
        try:
            await self._session.send_ping()
            return True
        except Exception:
            return False

    # -- 结果归一化 --------------------------------------------------------

    @staticmethod
    def _normalize(result: Any) -> dict:
        is_error = bool(getattr(result, "is_error", False))

        texts: list[str] = []
        for item in getattr(result, "content", None) or []:
            kind = getattr(item, "type", "")
            if kind == "text":
                texts.append(item.text)
            else:
                texts.append(f"[{kind}]")
        text = "\n".join(texts)

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            data = structured
        else:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                data = None

        return {"ok": not is_error, "is_error": is_error, "text": text, "data": data}

    # -- 给 AEGIS 用的适配 -------------------------------------------------

    def to_openai_functions(self) -> list[dict]:
        """把工具列表转成 Toolregistry 认识的样子。"""
        return [t.to_openai_function() for t in self.tools]

    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]
