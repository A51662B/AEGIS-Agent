"""Trace 分析器：跑规则，输出带证据的异常清单。

用法
────
    python -m observability.analyzer --session sess_xxx
    python -m observability.analyzer --all
    python -m observability.analyzer --all --only error
    python -m observability.analyzer --all --json report.json
"""

import argparse
import json
import sys

from . import rules as R
from .loader import build_summary, group_turns, load_steps
from .paths import latest_session, list_sessions, memory_file

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}
SEV_ICON = {"error": "✕", "warn": "!", "info": "i"}
SEV_LABEL = {"error": "错误", "warn": "警告", "info": "提示"}


# ══════════════════════════════════════════════════════════
def build_context(steps, turns, summary) -> dict:
    """预计算规则需要的派生数据。"""
    calls = []          # (prompt, hit, miss, completion, step_no)
    tool_events = []    # (step_no, name, args, call_id)

    for s in steps:
        if s["role"] != "assistant":
            continue
        if s["usage"]:
            u = s["usage"]
            calls.append((u["prompt"], u["hit"], u["miss"], u["completion"], s["n"]))
        for tc in s["tool_calls"]:
            tool_events.append((s["n"], tc["name"], tc["args"], tc["id"]))

    reasons = sorted(len(s["reasoning"] or "") for s in steps if s["reasoning"])
    deltas = sorted(s["delta"] for s in steps if s["delta"])

    def median(xs):
        return xs[len(xs) // 2] if xs else 0

    return {
        "turns": turns,
        "summary": summary,
        "calls": calls,
        "tool_events": tool_events,
        "median_reason": median(reasons),
        "median_delta": median(deltas),
    }


def analyze(session_id: str) -> dict:
    """跑全部规则，返回结构化报告。"""
    steps = load_steps(memory_file(session_id))
    turns = group_turns(steps)
    summary = build_summary(turns, steps)
    ctx = build_context(steps, turns, summary)

    findings = []
    for rule_id, _sev, fn in R.RULES:
        try:
            findings.extend(fn(steps, ctx) or [])
        except Exception as e:                            # noqa: BLE001
            findings.append(R.finding(
                rule_id, "info", 0, f"规则 {rule_id} 执行失败: {e}"))

    findings.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 9), f["step"]))

    return {
        "session": session_id,
        "summary": summary,
        "findings": findings,
        "counts": {
            "error": sum(1 for f in findings if f["severity"] == "error"),
            "warn": sum(1 for f in findings if f["severity"] == "warn"),
            "info": sum(1 for f in findings if f["severity"] == "info"),
        },
    }


# ══════════════════════════════════════════════════════════
# 证据字段的中文标签 —— 直接把 key 丢给用户看不懂
# ══════════════════════════════════════════════════════════
EVIDENCE_LABELS = {
    "prompt_before": "之前的 prompt",
    "prompt_after": "之后的 prompt",
    "delta": "本次新增",
    "prompt": "本次 prompt",
    "hit": "缓存命中",
    "miss": "缓存未命中",
    "cache_rate": "本次缓存命中率",
    "rate": "命中率",
    "tool": "工具",
    "tools": "涉及工具",
    "args": "工具参数",
    "steps": "涉及步骤",
    "count": "调用次数",
    "chars": "内容长度",
    "role": "消息角色",
    "duration": "耗时",
    "turn": "轮次",
    "steps_total": "总步数",
    "calls": "LLM 调用次数",
    "median": "会话中位数",
    "async_placeholder_step": "异步占位提示所在步骤",
    "poll_step": "轮询所在步骤",
    "content_head": "返回内容开头",
    "usage": "token 用量",
    "seconds": "单步耗时",
}

# 这些 key 的值是 token/字数，展示时加千分位和单位
_COUNT_KEYS = {"prompt_before", "prompt_after", "delta", "prompt", "hit",
               "miss", "chars", "calls", "count", "median", "steps_total"}
_SEC_KEYS = {"duration", "seconds"}
_PCT_KEYS = {"rate", "cache_rate"}


def _fmt_value(key, v) -> str:
    if v is None:
        return "—"
    if isinstance(v, list):
        uniq = []
        for x in v:
            if x not in uniq:
                uniq.append(x)
        return "、".join(str(x) for x in uniq)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        if key in _PCT_KEYS:
            # 0.452 → 45.2%
            return f"{v * 100:.1f}%" if v <= 1 else f"{v:.1f}%"
        if key in ("duration", "seconds"):
            return f"{v:.2f} 秒"
        if key in ("prompt_before", "prompt_after", "prompt", "hit", "miss", "delta"):
            return f"{v:,} tokens"
        if key in ("chars", "median"):
            return f"{v:,} 字"
        if key in ("calls", "count"):
            return f"{v:,} 次"
        return f"{v:,}" if isinstance(v, int) else f"{v}"
    return str(v)


def _evidence_rows(ev: dict) -> str:
    items = []
    for k, v in ev.items():
        label = EVIDENCE_LABELS.get(k, k)
        items.append(f'<dt>{_e(label)}</dt><dd>{_e(_fmt_value(k, v))}</dd>')
    return '<dl class="ev">' + "".join(items) + "</dl>"


# ══════════════════════════════════════════════════════════
# HTML 区块（供 viewer 内嵌）
# ══════════════════════════════════════════════════════════
def render_section(report: dict) -> str:
    findings = report.get("findings") or []
    if not findings:
        return ('<div class="findings ok">'
                '<span class="f-ok">✓ 未发现异常</span></div>')

    c = report["counts"]
    parts = ['<div class="findings">',
             f'<div class="findings-head">发现 {len(findings)} 项'
             f'<span class="f-count err">错误 {c["error"]}</span>'
             f'<span class="f-count warn">警告 {c["warn"]}</span>'
             f'<span class="f-count info">提示 {c["info"]}</span>'
             f'</div>']

    for f in findings:
        sev = f["severity"]
        parts.append('<div class="finding">')

        # 第一行：严重度 + 步骤 + 标题 + 细节
        line = (f'<div class="f-line">'
                f'<span class="f-badge {sev}">{SEV_ICON.get(sev, "?")}</span>'
                f'<a class="f-step" href="#s{f["step"]}">步 {f["step"]}</a>'
                f'<span class="f-title">{_e(f["title"])}</span>')
        if f.get("detail"):
            line += f'<span class="f-detail">{_e(f["detail"])}</span>'
        line += "</div>"
        parts.append(line)

        if f.get("suggestion"):
            parts.append(f'<div class="f-tip">建议：{_e(f["suggestion"])}</div>')

        ev = f.get("evidence") or {}
        if ev:
            parts.append(
                f'<details class="f-ev">'
                f'<summary>展开证据（{len(ev)} 项原始数据）</summary>'
                f'{_evidence_rows(ev)}</details>')

        parts.append('</div>')

    parts.append("</div>")
    return "".join(parts)


def _e(s) -> str:
    import html
    return html.escape(str(s)) if s is not None else ""


# ══════════════════════════════════════════════════════════
# 独立页面
# ══════════════════════════════════════════════════════════
_PAGE_CSS = """
  :root { --bg:#f7f8fa; --card:#fff; --bd:#e3e6ec; --tx:#1d1d1f; --mut:#8a8f98; }
  * { box-sizing:border-box; scrollbar-width:thin; scrollbar-color:#d9dee7 transparent; }
  ::-webkit-scrollbar { width:10px; height:10px; }
  ::-webkit-scrollbar-track { background:transparent; }
  ::-webkit-scrollbar-thumb { background-color:#dde2ea; border-radius:10px;
                              border:3px solid transparent; background-clip:content-box; }
  html, body { height:100%; }
  body { margin:0; overflow-y:auto; background:var(--bg); color:var(--tx);
         font:13px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  .wrap { max-width:1000px; margin:0 auto; padding:42px 56px 80px; }
  .head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  h1 { font-size:18px; margin:0; }
  .btn-back { flex:none; border:1px solid #d7dce6; background:#fff; color:#2f6fe4;
              text-decoration:none; font-size:12px; padding:5px 13px; border-radius:6px; }
  .btn-back:hover { background:#eef3ff; border-color:#b9c9ea; }
  .sub-title { color:var(--mut); font-size:12px; margin:4px 0 18px;
               font-variant-numeric:tabular-nums; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:10px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:10px;
          padding:10px 12px; }
  .card .k { color:var(--mut); font-size:11px; }
  .card .v { font-size:17px; font-weight:600; font-variant-numeric:tabular-nums; }
  .card .sub { font-size:11px; color:var(--mut); font-weight:400; margin-left:4px; }

  .findings { background:var(--card); border:1px solid var(--bd); border-radius:10px;
              padding:6px 16px 10px; }
  .findings.ok { border-color:#c7e9d8; background:#f4fdf8; padding:14px 16px;
                 color:#0b8a63; font-size:12.5px; }
  .findings-head { font-size:13px; font-weight:700; color:#111318; padding:10px 0 4px; }
  .f-count { font-weight:400; font-size:11.5px; margin-left:12px; }
  .f-count.err { color:#e03131; }
  .f-count.warn { color:#d97706; }
  .f-count.info { color:#8a8f98; }
  .finding { padding:10px 0; border-top:1px solid #f2f4f8; }
  .f-line { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; font-size:12.5px; }
  .f-badge { flex:none; width:16px; height:16px; line-height:16px; text-align:center;
             border-radius:50%; color:#fff; font-size:10px; font-weight:700; }
  .f-badge.error { background:#e03131; }
  .f-badge.warn { background:#f0b429; }
  .f-badge.info { background:#adb5bd; }
  .f-step { flex:none; color:#2f6fe4; text-decoration:none;
            font-variant-numeric:tabular-nums; }
  .f-step:hover { text-decoration:underline; }
  .f-title { font-weight:600; color:#24272e; }
  .f-detail { color:#6b7280; }
  .f-tip { font-size:11.5px; color:#6b7280; padding:3px 0 0 26px; }
  .f-ev { margin:5px 0 0 26px; }
  .f-ev > summary { cursor:pointer; font-size:11px; color:#9aa1ae; list-style:none;
                    padding:3px 0; }
  .f-ev > summary::-webkit-details-marker { display:none; }
  .f-ev > summary::before { content:"▸ "; }
  .f-ev[open] > summary::before { content:"▾ "; }
  .ev { margin:4px 0 2px; display:grid; grid-template-columns:max-content 1fr;
        gap:5px 16px; background:#f8f9fc; border:1px solid #eef1f6; border-radius:6px;
        padding:9px 13px; font-size:11.5px; }
  .ev dt { color:#9aa1ae; white-space:nowrap; }
  .ev dd { margin:0; color:#24272e; font-variant-numeric:tabular-nums;
           word-break:break-all; }
"""


def build_analysis_page(session_id: str, trace_href: str | None = None) -> str:
    """把一个会话的分析结果渲染成独立 HTML 页面。"""
    if trace_href is None:
        trace_href = f"/api/trace/{session_id}"
    report = analyze(session_id)
    s = report["summary"]

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">轮 / 步</div>
        <div class="v">{s['turns']} / {s['steps']}</div></div>
      <div class="card"><div class="k">轮内耗时</div>
        <div class="v">{s['wall']}s</div></div>
      <div class="card"><div class="k">工具调用</div>
        <div class="v">{s['tool_calls']}<span class="sub">错误 {s['tool_errors']}</span></div></div>
      <div class="card"><div class="k">输入 tokens</div>
        <div class="v">{s['prompt']:,}<span class="sub">命中 {s['cache_rate'] * 100:.1f}%</span></div></div>
      <div class="card"><div class="k">输出 tokens</div>
        <div class="v">{s['completion']:,}</div></div>
      <div class="card"><div class="k">发现</div>
        <div class="v">{len(report['findings'])}</div></div>
    </div>
    """

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>分析 · {_e(session_id)}</title>
<style>{_PAGE_CSS}</style></head><body>
<div class="wrap">
  <div class="head">
    <h1>分析 · {_e(session_id)}</h1>
    <a class="btn-back" href="{_e(trace_href)}" target="_blank" rel="noopener">← 回到 trace</a>
  </div>
  <div class="sub-title">{report['summary']['steps']} 步 · {report['summary']['turns']} 轮</div>
  {cards}
  {render_section(report)}
</div>
</body></html>"""


# ══════════════════════════════════════════════════════════
def print_report(report: dict):
    s = report["summary"]
    print("=" * 80)
    print(f'会话 {report["session"]}')
    print("=" * 80)
    print(f'  {s["turns"]} 轮 · {s["steps"]} 步 · {s["wall"]}s · '
          f'工具 {s["tool_calls"]} 次（错误 {s["tool_errors"]}）')
    print(f'  tokens 输入 {s["prompt"]:,}（命中 {s["cache_rate"] * 100:.1f}%）'
          f' / 输出 {s["completion"]:,}')

    findings = report["findings"]
    if not findings:
        print("\n  ✓ 未发现异常")
        return

    c = report["counts"]
    print(f'\n  发现 {len(findings)} 项：'
          f'错误 {c["error"]} · 警告 {c["warn"]} · 提示 {c["info"]}')
    for f in findings:
        sev = f["severity"]
        print(f'\n  [{SEV_LABEL[sev]}] 步 {f["step"]:>3}  {f["title"]}')
        if f.get("detail"):
            print(f'          {f["detail"]}')
        if f.get("suggestion"):
            print(f'          → {f["suggestion"]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", choices=["error", "warn", "info"], default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--html", default=None, help="把分析结果写成独立 HTML 页面")
    a = ap.parse_args()

    if a.all:
        sids = list_sessions()
    elif a.session:
        sids = [a.session]
    else:
        s = latest_session()
        sids = [s] if s else []

    if not sids:
        print("[x] 没有可用会话")
        return 1

    reports = []
    for sid in sids:
        try:
            r = analyze(sid)
        except FileNotFoundError:
            print(f"[x] 找不到 {sid}")
            continue
        if a.only:
            r["findings"] = [f for f in r["findings"] if f["severity"] == a.only]
        reports.append(r)
        if not a.json:
            print_report(r)
            print()

    if a.json:
        import json as _json
        with open(a.json, "w", encoding="utf-8") as fh:
            _json.dump(reports, fh, ensure_ascii=False, indent=2)
        print(f"  已写出 {a.json}")

    if a.html:
        sid = sids[0]
        page = build_analysis_page(sid, trace_href=f"{sid}.trace.html")
        with open(a.html, "w", encoding="utf-8") as fh:
            fh.write(page)
        print(f"  已写出 {a.html}")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
