"""路径与会话枚举：统一从这里取，避免模块挪动后相对路径失效。"""

import os

# observability/ 的上一级就是项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_ROOT = os.path.join(PROJECT_ROOT, "memory")


def memory_file(session_id: str) -> str:
    """会话的记忆文件路径。"""
    return os.path.join(MEMORY_ROOT, session_id, "memory.jsonl")


def list_sessions() -> list[str]:
    """按修改时间倒序列出所有会话。"""
    if not os.path.isdir(MEMORY_ROOT):
        return []
    found = []
    for name in os.listdir(MEMORY_ROOT):
        p = memory_file(name)
        if os.path.exists(p):
            found.append((os.path.getmtime(p), name))
    return [name for _, name in sorted(found, reverse=True)]


def latest_session() -> str | None:
    s = list_sessions()
    return s[0] if s else None
