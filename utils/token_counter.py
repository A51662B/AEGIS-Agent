# -*- coding: utf-8 -*-
"""Token 统计：估算当前上下文窗口的使用量。

用 tiktoken 的 cl100k_base（GPT-4 encoding）近似统计。DeepSeek 的 tokenizer 与 OpenAI
略有差异，但用于判断"是否达到触发压缩的阈值"这个量级足够准。
"""
import json
import os

from dotenv import load_dotenv
import tiktoken

load_dotenv()

# 模型 -> 上下文窗口（token）。默认按 128K 计；deepseek-v4.1-flash 若已开通 1M 长上下文，可用 .env 的 CONTEXT_WINDOW 显式覆盖
CONTEXT_WINDOWS = {
    "deepseek-v4.1-flash": 128_000,
    "deepseek-flash": 128_000,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
}

DEFAULT_CONTEXT_WINDOW = 128_000

# 每条消息约 4 token 的角色/格式开销
_MSG_OVERHEAD = 4


def _get_encoding():
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _estimate_chars(text: str) -> int:
    """无 tiktoken 时的兜底估算：中文 1 字 ≈ 1.5 token，英文 1 字符 ≈ 0.3 token。"""
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.3)


def get_context_window(model: str | None = None) -> int:
    """获取模型上下文窗口。优先 .env 的 CONTEXT_WINDOW 显式配置，其次模型映射表，最后默认值。"""
    env = os.getenv("CONTEXT_WINDOW")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return CONTEXT_WINDOWS.get(model or "", DEFAULT_CONTEXT_WINDOW)


def count_tokens(message_list) -> int:
    """估算消息列表的 token 量（content + reasoning_content + tool_calls + 格式开销）。"""
    encoding = _get_encoding()
    total = 0
    for m in message_list:
        d = m.message if hasattr(m, "message") else (m if isinstance(m, dict) else {})
        text = ""
        if isinstance(d, dict):
            if d.get("content"):
                text += str(d["content"])
            if d.get("reasoning_content"):
                text += str(d["reasoning_content"])
            if d.get("tool_calls"):
                text += json.dumps(d["tool_calls"], ensure_ascii=False)
        if encoding is not None:
            total += len(encoding.encode(text))
        else:
            total += _estimate_chars(text)
        total += _MSG_OVERHEAD
    return total


def get_usage(message_list, model: str | None = None) -> dict:
    """返回 {used, window, ratio}，ratio 是 0~1 的使用比例。"""
    used = count_tokens(message_list)
    window = get_context_window(model)
    ratio = (used / window) if window else 0.0
    return {"used": used, "window": window, "ratio": round(ratio, 4)}
