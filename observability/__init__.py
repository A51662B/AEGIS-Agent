"""observability —— 会话可观测性：记录、展示、分析。

模块划分
────────
    paths.py     路径与会话枚举
    loader.py    memory.jsonl → steps / turns / summary（纯解析）
    rules.py     异常规则（确定性判断，不用 LLM）
    analyzer.py  跑规则、出报告、渲染 HTML 区块
    viewer.py    单文件 HTML 渲染

常用入口
────────
    python -m observability.viewer   --session probe1     # 生成本地 HTML
    python -m observability.analyzer --session probe1     # 终端异常报告
    python -m observability.analyzer --all                # 扫描全部会话

公共 API 用惰性导入，避免 `python -m observability.analyzer` 时重复加载本包。
"""

__all__ = [
    "analyze", "render_section",
    "build_summary", "group_turns", "load_steps",
    "latest_session", "list_sessions", "memory_file",
    "build_html",
]

_LAZY = {
    "analyze": ("analyzer", "analyze"),
    "render_section": ("analyzer", "render_section"),
    "build_summary": ("loader", "build_summary"),
    "group_turns": ("loader", "group_turns"),
    "load_steps": ("loader", "load_steps"),
    "latest_session": ("paths", "latest_session"),
    "list_sessions": ("paths", "list_sessions"),
    "memory_file": ("paths", "memory_file"),
    "build_html": ("viewer", "build_html"),
}


def __getattr__(name):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    mod_name, attr = _LAZY[name]
    mod = import_module(f".{mod_name}", __name__)
    return getattr(mod, attr)


def __dir__():
    return sorted(__all__)
