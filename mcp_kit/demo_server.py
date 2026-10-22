# -*- coding: utf-8 -*-
"""演示用 MCP 服务端（mcp 2.x：FastMCP 已改名 MCPServer）。

    python demo_server.py        # 默认 stdio，等客户端来连
"""
from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("DemoServer")


@mcp.tool()
def add(a: int, b: int) -> int:
    """把两个整数相加。"""
    return a + b


@mcp.tool()
async def echo_slow(text: str, seconds: float = 2.0) -> str:
    """原样返回文本，但会故意等 seconds 秒——用来测试超时。"""
    await asyncio.sleep(seconds)
    return text


@mcp.tool()
def fail(reason: str = "故意报错") -> str:
    """总是抛异常——用来测试错误处理。"""
    raise ValueError(reason)


if __name__ == "__main__":
    mcp.run()
