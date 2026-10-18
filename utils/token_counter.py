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


def count_text(text: str) -> int:
    """估算一段纯文本的 token 数。"""
    if not text:
        return 0
    encoding = _get_encoding()
    return len(encoding.encode(text)) if encoding is not None else _estimate_chars(text)


def _find_last_usage(message_list):
    """从后往前找最近一条带真实 usage 的 assistant 消息，返回 (index, usage)。

    usage 存在 rowmessage 里（Memory 落盘时只存 JSON 原生类型）。
    真实 usage 的 prompt_tokens 是 API 给的精确上下文大小（含 system prompt + tools schema）。
    """
    for i in range(len(message_list) - 1, -1, -1):
        m = message_list[i]
        if getattr(m, "role", None) != "assistant":
            continue
        u = (getattr(m, "rowmessage", None) or {}).get("usage")
        if u and u.get("prompt_tokens"):
            return i, u
    return None, None


def get_usage(message_list, model: str | None = None, system_prompt: str = "") -> dict:
    """统计上下文用量 + 累计花费 + 缓存命中。

    上下文大小（used）优先用真实值：
      最近一次调用的 prompt_tokens（精确，含 system prompt + tools schema）
      + 之后新增消息的估算。
    没有真实 usage 时（旧会话 / 尚未调用过）回退到纯估算，并补上 system prompt。

    返回：
      used / window / ratio : 当前上下文大小与占比
      source                : "real"（含真实值）或 "estimated"
      spend                 : 累计 prompt / completion / reasoning / total
      cache                 : hit / miss / rate
      calls                 : 统计到几次调用
    """
    window = get_context_window(model)
    idx, real = _find_last_usage(message_list)

    if real is not None:
        tail = message_list[idx + 1:]
        used = int(real.get("prompt_tokens") or 0) + count_tokens(tail)
        source = "real"
    else:
        used = count_tokens(message_list) + count_text(system_prompt)
        source = "estimated"

    spend_prompt = spend_completion = spend_reasoning = 0
    cache_hit = cache_miss = calls = 0
    for m in message_list:
        u = (getattr(m, "rowmessage", None) or {}).get("usage")
        if not u:
            continue
        calls += 1
        spend_prompt += u.get("prompt_tokens") or 0
        spend_completion += u.get("completion_tokens") or 0
        spend_reasoning += (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        cache_hit += u.get("prompt_cache_hit_tokens") or 0
        cache_miss += u.get("prompt_cache_miss_tokens") or 0

    # 最近一次调用的单独统计（面板的"本轮"页签）
    last = None
    if real is not None:
        l_hit = real.get("prompt_cache_hit_tokens") or 0
        l_miss = real.get("prompt_cache_miss_tokens") or 0
        l_sum = l_hit + l_miss
        last = {
            "prompt": real.get("prompt_tokens") or 0,
            "hit": l_hit,
            "miss": l_miss,
            "rate": round(l_hit / l_sum, 4) if l_sum else 0.0,
            "completion": real.get("completion_tokens") or 0,
            "reasoning": (real.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
            "cache_available": bool(l_sum),
        }

    total_cache = cache_hit + cache_miss
    return {
        "used": used,
        "window": window,
        "ratio": round(used / window, 4) if window else 0.0,
        "source": source,
        "calls": calls,
        "spend": {
            "prompt": spend_prompt,
            "completion": spend_completion,
            "reasoning": spend_reasoning,
            "total": spend_prompt + spend_completion,
        },
        "cache": {
            "hit": cache_hit,
            "miss": cache_miss,
            "rate": round(cache_hit / total_cache, 4) if total_cache else 0.0,
        },
        "last": last,
    }
