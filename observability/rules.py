"""异常规则：每条规则是一个纯函数，输入 steps + ctx，输出 findings 列表。

设计约束
────────
· 全部是确定性判断（计数 / 阈值 / 前缀），不用 LLM
· 每条 finding 必须带 step 编号和 evidence（原始数字），否则不可复核
· severity: error（确定是问题）/ warn（可疑）/ info（仅供参考）

ctx 里预计算的字段（由 analyzer.build_context 提供）：
    turns            轮列表
    summary          build_summary() 的结果
    calls            [(prompt, hit, miss, completion, step_no)] 仅 assistant
    tool_events      [(step_no, name, args, call_id)]
    median_reason    reasoning 字数的中位数
    median_delta     步骤耗时的中位数
"""

# 会被模型用来"看文件系统"的工具——异步等待期间调这些，基本等于轮询
FS_TOOLS = {
    "list_directory", "read_file", "glob", "file_exists",
    "get_file_info", "search_file", "grep_file",
}

ASYNC_PLACEHOLDER_MARK = "结果稍后自动返回"


# ══════════════════════════════════════════════════════════
def finding(rule, severity, step, title, detail="", evidence=None, suggestion=""):
    return {
        "rule": rule,
        "severity": severity,
        "step": step,
        "title": title,
        "detail": detail,
        "evidence": evidence or {},
        "suggestion": suggestion,
    }


def _head(text, n=80):
    if not isinstance(text, str):
        return ""
    return text.replace("\n", " ").strip()[:n]


# ══════════════════════════════════════════════════════════
# A. 错误
# ══════════════════════════════════════════════════════════
def check_tool_error(steps, ctx):
    """工具返回以错误前缀开头。"""
    out = []
    for s in steps:
        if s["is_error"]:
            out.append(finding(
                "tool_error", "error", s["n"],
                f'工具失败：{s["paired_name"] or "?"}',
                detail=_head(s["content"], 140),
                evidence={"tool": s["paired_name"], "chars": len(s["content"] or "")},
                suggestion="检查工具入参是否来自模型猜测（如文件名），优先让工具返回确切标识符",
            ))
    return out


def check_error_cluster(steps, ctx):
    """连续 2 次以上工具失败——通常意味着模型在同一个错误假设上反复试。"""
    out = []
    run = []
    for s in steps:
        if s["is_error"]:
            run.append(s)
        else:
            if len(run) >= 2:
                out.append(finding(
                    "error_cluster", "error", run[-1]["n"],
                    f"连续 {len(run)} 次工具失败",
                    detail="、".join(str(x["n"]) for x in run),
                    evidence={"steps": [x["n"] for x in run],
                              "tools": [x["paired_name"] for x in run]},
                    suggestion="错误信息是否被回灌给模型？模型是否在重复同一个错误入参",
                ))
            run = []
    if len(run) >= 2:
        out.append(finding(
            "error_cluster", "error", run[-1]["n"],
            f"连续 {len(run)} 次工具失败",
            evidence={"steps": [x["n"] for x in run],
                      "tools": [x["paired_name"] for x in run]},
        ))
    return out


# ══════════════════════════════════════════════════════════
# B. 重复调用（轮询）
# ══════════════════════════════════════════════════════════
def check_duplicate_call(steps, ctx):
    """同一工具 + 同一参数被调用 ≥2 次。"""
    seen = {}
    for step_no, name, args, _cid in ctx["tool_events"]:
        seen.setdefault((name, args), []).append(step_no)
    out = []
    for (name, args), ns in seen.items():
        if len(ns) >= 2:
            out.append(finding(
                "duplicate_call", "error", ns[1],
                f"重复调用 {name}（同参数 {len(ns)} 次）",
                detail=_head(args, 120),
                evidence={"tool": name, "args": _head(args, 200), "steps": ns},
                suggestion="1) 工具是否返回了足够的信息；2) 是否需要拒绝重复调用的护栏",
            ))
    return out


def check_repeat_tool_total(steps, ctx):
    """同一工具被调用 ≥5 次（参数不同也算）——反复试探。"""
    cnt = {}
    for step_no, name, _args, _cid in ctx["tool_events"]:
        cnt.setdefault(name, []).append(step_no)
    out = []
    for name, ns in cnt.items():
        if len(ns) >= 5:
            out.append(finding(
                "repeat_tool_total", "warn", ns[-1],
                f"{name} 被调用 {len(ns)} 次",
                evidence={"tool": name, "count": len(ns), "steps": ns},
                suggestion="是否缺少一次能拿到全部信息的工具（如按目录批量读）",
            ))
    return out


# ══════════════════════════════════════════════════════════
# C. 上下文 / 缓存
# ══════════════════════════════════════════════════════════
def check_big_context_jump(steps, ctx, limit=15000):
    """单次调用的 prompt 比上次多出 > limit tokens。"""
    out = []
    prev = 0
    for prompt, hit, _miss, _comp, step_no in ctx["calls"]:
        delta = prompt - prev
        if delta > limit and prev > 0:
            rate = hit / prompt * 100 if prompt else 0
            out.append(finding(
                "big_context_jump", "error", step_no,
                f"单次上下文增加 {delta:,} tokens",
                detail=f"prompt {prev:,} → {prompt:,}，本次命中率 {rate:.1f}%",
                evidence={"prompt_before": prev, "prompt_after": prompt,
                          "delta": delta, "cache_rate": round(rate, 1)},
                suggestion="检查这一步塞进了什么大内容（可能是全文/大文件），应先定位再读",
            ))
        prev = prompt
    return out


def check_large_tool_result(steps, ctx, limit=50000):
    """单步工具结果超过 5 万字。"""
    out = []
    for s in steps:
        if s["role"] in ("tool", "async") and isinstance(s["content"], str) \
                and len(s["content"]) > limit:
            out.append(finding(
                "large_tool_result", "warn", s["n"],
                f"工具返回 {len(s['content']):,} 字",
                detail=f'{s["paired_name"] or s["role"]}',
                evidence={"tool": s["paired_name"], "chars": len(s["content"])},
                suggestion="大结果会永久留在上下文，考虑按需分片读取",
            ))
    return out


def check_cache_drop(steps, ctx, floor=0.6, min_prompt=5000):
    """prompt 已经比较大，但命中率仍然很低——前缀被打断或大量新内容。"""
    out = []
    for prompt, hit, _miss, _comp, step_no in ctx["calls"]:
        if prompt >= min_prompt:
            rate = hit / prompt
            if rate < floor:
                out.append(finding(
                    "cache_drop", "warn", step_no,
                    f"缓存命中率仅 {rate * 100:.1f}%",
                    detail=f"prompt {prompt:,}，hit {hit:,}",
                    evidence={"prompt": prompt, "hit": hit,
                              "rate": round(rate, 3)},
                    suggestion="确认是否有压缩/回滚打断了前缀，或本步塞入了大量新内容",
                ))
    return out


# ══════════════════════════════════════════════════════════
# D. 时间
# ══════════════════════════════════════════════════════════
def check_slow_tool_step(steps, ctx, limit=30.0):
    """单步耗时 > limit 秒。"""
    out = []
    for s in steps:
        d = s["delta"]
        if d and d > limit:
            out.append(finding(
                "slow_tool_step", "warn", s["n"],
                f"单步耗时 {d:.1f}s",
                detail=f'{s["role"]} / {s["paired_name"] or "—"}',
                evidence={"role": s["role"], "tool": s["paired_name"],
                          "seconds": round(d, 3)},
                suggestion="若是外部工具，考虑异步化；影响不大则忽略",
            ))
    return out


def check_slow_turn(steps, ctx, limit=120.0):
    out = []
    for t in ctx["turns"]:
        d = t.get("duration")
        if d and d > limit:
            out.append(finding(
                "slow_turn", "info", t["steps"][0]["n"],
                f"第 {t['index']} 轮耗时 {d:.1f}s",
                evidence={"turn": t["index"], "duration": round(d, 3),
                          "steps": t["n_steps"]},
            ))
    return out


# ══════════════════════════════════════════════════════════
# E. 轮次
# ══════════════════════════════════════════════════════════
def check_too_many_rounds(steps, ctx, limit=20):
    n = len(ctx["calls"])
    if n > limit:
        return [finding(
            "too_many_rounds", "warn", steps[-1]["n"] if steps else 0,
            f"LLM 调用 {n} 次（上限 30）",
            evidence={"calls": n},
            suggestion="是否陷入循环；检查是否有工具反复返回无用信息",
        )]
    return []


# ══════════════════════════════════════════════════════════
# F. 行为
# ══════════════════════════════════════════════════════════
def check_reasoning_spike(steps, ctx, ratio=3.0, floor=300):
    """某一步的思考长度远超会话中位数——纠结时刻。"""
    med = ctx["median_reason"] or 0
    out = []
    for s in steps:
        r = s.get("reasoning") or ""
        if len(r) > floor and len(r) > med * ratio:
            out.append(finding(
                "reasoning_spike", "warn", s["n"],
                f"思考 {len(r)} 字（中位数 {int(med)}）",
                detail=_head(r, 140),
                evidence={"chars": len(r), "median": int(med)},
                suggestion="模型在犹豫该做什么——通常是它缺少一个明确的可选动作",
            ))
    return out


def check_poll_after_async(steps, ctx, window=3):
    """异步工具返回占位提示后，短时间内出现文件系统探查——轮询征兆。"""
    out = []
    for i, s in enumerate(steps):
        c = s.get("content")
        if s["role"] != "tool" or not isinstance(c, str) \
                or ASYNC_PLACEHOLDER_MARK not in c:
            continue
        for j in range(i + 1, min(i + 1 + window, len(steps))):
            for tc in steps[j]["tool_calls"]:
                if tc["name"] in FS_TOOLS:
                    out.append(finding(
                        "poll_after_async", "error", steps[j]["n"],
                        f"异步等待期间调用 {tc['name']} 检查进度",
                        detail=_head(tc["args"], 120),
                        evidence={"async_placeholder_step": s["n"],
                                  "poll_step": steps[j]["n"],
                                  "tool": tc["name"]},
                        suggestion="占位提示应明确给出「继续收集 / 停手等待」两个出口",
                    ))
    return out


def check_empty_assistant(steps, ctx):
    out = []
    for s in steps:
        if s["role"] == "assistant" and not s["content"] and not s["tool_calls"] \
                and not s.get("reasoning"):
            out.append(finding(
                "empty_assistant", "info", s["n"],
                "空的助手消息（无正文/无工具/无思考）",
                evidence={"usage": s.get("usage")},
            ))
    return out


# ══════════════════════════════════════════════════════════
# 规则表
# ══════════════════════════════════════════════════════════
RULES = [
    ("tool_error",          "error", check_tool_error),
    ("error_cluster",       "error", check_error_cluster),
    ("duplicate_call",      "error", check_duplicate_call),
    ("poll_after_async",    "error", check_poll_after_async),
    ("big_context_jump",    "error", check_big_context_jump),
    ("repeat_tool_total",   "warn",  check_repeat_tool_total),
    ("large_tool_result",   "warn",  check_large_tool_result),
    ("cache_drop",          "warn",  check_cache_drop),
    ("slow_tool_step",      "warn",  check_slow_tool_step),
    ("reasoning_spike",     "warn",  check_reasoning_spike),
    ("too_many_rounds",     "warn",  check_too_many_rounds),
    ("slow_turn",           "info",  check_slow_turn),
    ("empty_assistant",     "info",  check_empty_assistant),
]
