# -*- coding: utf-8 -*-
"""AEGIS 的 MCP 管理模块。

注意：这个包不能叫 `mcp`，否则会遮蔽官方的 `mcp` SDK
（Python 会优先从当前工作目录找同名包，`from mcp import ClientSession` 就导到自己身上了）。

用法：
    from mcp_kit import MCPManager

    mgr = MCPManager(command=sys.executable, args=["demo_server.py"])
    await mgr.start()                        # 初始化 + 拿工具列表
    await mgr.call("add", {"a": 1, "b": 2})  # 调工具
    await mgr.stop()
"""
from .manager import MCPManager, MCPTool

__all__ = ["MCPManager", "MCPTool"]
