# -*- coding: utf-8 -*-
"""演示：用 MCPManager 连上 demo_server.py，列工具、调工具、测超时和错误。

在 AEGIS 根目录下执行：

    python -m mcp_kit.demo_client
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from manager import MCPManager

HERE = Path(__file__).resolve().parent


async def main() -> None:
    mgr = MCPManager(
        command=sys.executable,
        args=[str(HERE / "demo_server.py")],
        cwd=str(HERE),
        name="demo",
        timeout=3.0,
    )

    await mgr.start()
    print(f"已连接：{mgr.server_info}")
    print(f"发现 {len(mgr.tools)} 个工具：")
    for t in mgr.tools:
        print(f"  - {t.name}: {t.description}")
    print(f"\n转成 AEGIS 格式的 schema：")
    for f in mgr.to_openai_functions():
        fn = f["function"]
        print(f"  {fn['name']}  params={list(fn['parameters'].get('properties', {}))}")
    print(fn)
    print("\n--- 调用 add(2, 3) ---")
    print(await mgr.call("add", {"a": 2, "b": 3}))

    print("\n--- 调用 fail()，看错误怎么返回 ---")
    print(await mgr.call("fail", {"reason": "演示错误"}))

    print("\n--- 调用 echo_slow(seconds=5)，超时设的 3 秒 ---")
    try:
        await mgr.call("echo_slow", {"text": "hi", "seconds": 5})
        print("没超时？不应该")
    except asyncio.TimeoutError:
        print("超时了，符合预期（asyncio.TimeoutError）")

    await mgr.stop()
    print("\n已断开。")


if __name__ == "__main__":
    asyncio.run(main())
