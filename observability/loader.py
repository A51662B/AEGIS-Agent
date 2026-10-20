"""memory.jsonl 解析：把会话记录整形成可分析的 steps / turns / summary。

这一层只做解析，不做渲染、不打印，供 viewer 与 analyzer 共用。
"""

import json
from datetime import datetime

# 历史数据兼容：这些内容是以 role='user' 存下来的系统注入消息
INJECTED_PREFIXES = (
    "用户手动中断了之前的操作",
    "工具执行超时",
    "达到最大工具调用轮数",
)

# 真正的工具错误：只由这些固定前缀标识（见 async_ChatModel.py 的 invoke 与两个 executor）。
# 不能用关键词匹配——论文点评里出现「错误」「error」是正常的，会被误判。
TOOL_ERR_PREFIXES = (
    "工具执行出错",
    "工具参数错误",
    "未知工具",
    "子代理启动失败",
    "子代理执行出错",
    "工作流启动失败",
    "工作流执行出错",
)


def _is_tool_error(role: str, content) -> bool:
    """只有 role 为 tool/async 且内容以错误前缀开头，才算工具失败。"""
    if role not in ("tool", "async"):
        return False
    if not isinstance(content, str):
        return False
    return content.startswith(TOOL_ERR_PREFIXES)

# ══════════════════════════════════════════════════════════
# 解析
# ══════════════════════════════════════════════════════════
def _parse_ts(s: str) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:                                    # noqa: BLE001
        return None


def _inner(row: dict) -> dict:
    m = row.get("message")
    return m if isinstance(m, dict) else {}


def _is_injected(role: str, content) -> bool:
    if role == "injected":
        return True
    if role != "user":
        return False
    c = content if isinstance(content, str) else ""
    return any(c.startswith(p) for p in INJECTED_PREFIXES)


def load_steps(memory_file: str) -> list[dict]:
    """读 memory.jsonl → 扁平的步骤列表（含 tool_call 配对）。"""
    rows = []
    for line in open(memory_file, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # 建 tool_call_id → {name, args} 映射（一次 assistant 可能并发多个调用）
    call_meta = {}
    for r in rows:
        m = r.get("message") or {}
        inner = m.get("message")
        if m.get("role") != "assistant" or not isinstance(inner, dict):
            continue
        for tc in (inner.get("tool_calls") or []):
            fn = tc.get("function") or {}
            call_meta[tc.get("id")] = {
                "name": fn.get("name") or "?",
                "args": fn.get("arguments") or "",
            }

    steps = []
    prev_ts = None
    for i, r in enumerate(rows):
        m = r.get("message") or {}
        role = m.get("role") or "?"
        inner = m.get("message")
        inner = inner if isinstance(inner, dict) else {}
        content = inner.get("content")
        ts_raw = m.get("timestamp") or ""
        ts = _parse_ts(ts_raw)
        delta = (ts - prev_ts) if (ts is not None and prev_ts is not None) else None
        if ts is not None:
            prev_ts = ts

        usage = None
        rm = m.get("rowmessage")
        if isinstance(rm, dict) and isinstance(rm.get("usage"), dict):
            u = rm["usage"]
            usage = {
                "prompt": u.get("prompt_tokens") or 0,
                "completion": u.get("completion_tokens") or 0,
                "hit": u.get("prompt_cache_hit_tokens") or 0,
                "miss": u.get("prompt_cache_miss_tokens") or 0,
            }

        tcs = []
        for tc in (inner.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tcs.append({
                "id": tc.get("id"),
                "name": fn.get("name") or "?",
                "args": fn.get("arguments") or "",
            })

        call_id = inner.get("tool_call_id")
        paired = call_meta.get(call_id) if call_id else None

        is_err = _is_tool_error(role, content)

        steps.append({
            "n": len(steps),
            "row": i,
            "role": role,
            "ts": ts_raw,
            "ts_epoch": ts,
            "delta": round(delta, 3) if delta is not None else None,
            "content": content if isinstance(content, str) else
                       (json.dumps(content, ensure_ascii=False) if content else ""),
            "reasoning": inner.get("reasoning_content"),
            "tool_calls": tcs,
            "tool_call_id": call_id,
            "paired_name": paired["name"] if paired else None,
            "paired_args": paired["args"] if paired else None,
            "usage": usage,
            "is_error": is_err,
            "is_injected": _is_injected(role, content),
        })
    return steps


def group_turns(steps: list[dict]) -> list[dict]:
    """按 role=='user'（且非注入）切轮。"""
    turns = []
    cur = None
    for s in steps:
        starts_turn = (s["role"] == "user" and not s["is_injected"])
        if starts_turn or cur is None:
            cur = {"index": len(turns) + 1, "steps": [], "start_ts": s["ts"]}
            turns.append(cur)
            if not starts_turn:
                # 会话开头没有 user 消息（异常数据），也单独成一轮
                cur["index"] = 0
        cur["steps"].append(s)
        cur["end_ts"] = s["ts"]

    for t in turns:
        # 每轮第一步的时间基准要重置：不能减去上一轮最后一条消息的时间，
        # 否则会把轮与轮之间的空闲（例如用户中断后隔了 6 分钟才发下一条）算成这一步的耗时。
        if t["steps"]:
            t["steps"][0]["delta"] = None
        ts_list = [s["ts_epoch"] for s in t["steps"] if s["ts_epoch"] is not None]
        for s in t["steps"]:
            s["turn"] = t["index"]
        t["start_epoch"] = ts_list[0] if ts_list else None
        t["end_epoch"] = ts_list[-1] if ts_list else None
        t["duration"] = round(t["end_epoch"] - t["start_epoch"], 3) \
            if len(ts_list) > 1 else None
        t["n_steps"] = len(t["steps"])
    return turns


def build_summary(turns: list[dict], steps: list[dict]) -> dict:
    llm_time = tool_time = async_time = 0.0
    prompt = completion = hit = miss = 0
    tool_calls = tool_errors = 0
    slowest = None

    for t in turns:
        for s in t["steps"]:
            d = s["delta"] or 0.0
            if s["role"] == "assistant":
                llm_time += d
            elif s["role"] == "tool":
                tool_time += d
            elif s["role"] == "async":
                async_time += d
            if s["usage"]:
                prompt += s["usage"]["prompt"]
                completion += s["usage"]["completion"]
                hit += s["usage"]["hit"]
                miss += s["usage"]["miss"]
            tool_calls += len(s["tool_calls"])
            if s["is_error"]:
                tool_errors += 1

            # 最慢一步：工具结果和异步回填都算
            if s["role"] in ("tool", "async") and d > 0:
                name = s["paired_name"] or ("异步回填" if s["role"] == "async" else s["role"])
                if slowest is None or d > slowest["dur"]:
                    slowest = {"name": name, "dur": round(d, 3), "step": s["n"]}

    # 轮内耗时之和（排除轮与轮之间的空闲，比如用户中断后想了 6 分钟才发下一条）
    wall = sum(t["duration"] for t in turns if t["duration"])

    # 整个会话的时间跨度（含轮间空闲），仅作参考
    span = 0.0
    if steps and steps[0]["ts_epoch"] is not None and steps[-1]["ts_epoch"] is not None:
        span = steps[-1]["ts_epoch"] - steps[0]["ts_epoch"]

    other = max(0.0, wall - llm_time - tool_time - async_time)

    return {
        "steps": len(steps),
        "turns": len(turns),
        "wall": round(wall, 2),
        "span": round(span, 2),
        "idle": round(span - wall, 2),
        "llm_time": round(llm_time, 2),
        "tool_time": round(tool_time, 2),
        "async_time": round(async_time, 2),
        "other_time": round(other, 2),
        "prompt": prompt, "completion": completion,
        "hit": hit, "miss": miss,
        "cache_rate": round(hit / (hit + miss), 4) if (hit + miss) else 0.0,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "slowest": slowest,
    }


