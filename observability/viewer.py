"""Trace 查看器：把 memory/<session>/memory.jsonl 渲染成单文件 HTML。

解析逻辑在 loader.py，路径在 paths.py，本文件只负责渲染。

用法
────
    python -m observability.viewer --session probe1
    python -m observability.viewer --session probe1 --out trace.html
    python -m observability.viewer                 # 默认取最新的会话
"""

import argparse
import html
import json
import os
import sys

from .paths import MEMORY_ROOT, latest_session
from .loader import (  # noqa: F401
    INJECTED_PREFIXES,
    TOOL_ERR_PREFIXES,
    _is_tool_error,
    _parse_ts,
    _is_injected,
    load_steps,
    group_turns,
    build_summary,
)

KIND_COLOR = {
    "llm": "#4c8dff",
    "tool": "#ff9f43",
    "async": "#ee5253",
    "user": "#10ac84",
    "inj": "#95a5a6",
}

ROLE_LABEL = {
    "user": "用户",
    "assistant": "助手",
    "tool": "工具",
    "async": "异步",
    "injected": "注入",
    "system": "系统",
    "summary": "摘要",
    "boundary": "边界",
}

# ══════════════════════════════════════════════════════════
# 渲染（单文件 HTML）
# ══════════════════════════════════════════════════════════
def _e(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _fmt_dur(d) -> str:
    if d is None:
        return "—"
    if d >= 1:
        return f"{d:.2f}s"
    return f"{d * 1000:.0f}ms"


def _kind_of(step) -> str:
    if step["is_injected"]:
        return "inj"
    if step["role"] == "assistant":
        return "llm"
    if step["role"] == "tool":
        return "tool"
    if step["role"] == "async":
        return "async"
    return "user"


def _dur_class(d) -> str:
    if d is None:
        return ""
    if d >= 10:
        return " slow2"
    if d >= 2:
        return " slow1"
    return ""


def _row_summary(s: dict) -> str:
    """一行摘要：尽量用一句话说清这一步做了什么。"""
    c = (s["content"] or "").replace("\n", " ").strip()
    if s["role"] == "tool":
        name = s["paired_name"] or "工具结果"
        return f"← {name}   {c[:90]}"
    if s["tool_calls"]:
        names = "、".join(tc["name"] for tc in s["tool_calls"])
        return f"→ {names}"
    if c:
        return c[:120]
    if s["reasoning"]:
        return "（仅思考）" + s["reasoning"].replace("\n", " ").strip()[:110]
    return "（空）"


def _sec(title: str, body: str, err: bool = False) -> str:
    """内容分块：标题、标题下的淡分割线、内容、结尾的淡分割线。"""
    cls = "sec err" if err else "sec"
    return (f'<section class="{cls}"><h3 class="sec-t">{_e(title)}</h3>'
            f'<pre>{_e(body)}</pre></section>')


def _meta_rows(s: dict) -> str:
    """第二层：每项一行的信息展示。"""
    rows = [
        ("开始时间", s["ts"][11:23] if s["ts"] else "—"),
        ("耗时", _fmt_dur(s["delta"])),
    ]
    if s["paired_name"]:
        rows.append(("工具", s["paired_name"]))
    if s["usage"]:
        u = s["usage"]
        rate = u["hit"] / (u["hit"] + u["miss"]) * 100 if (u["hit"] + u["miss"]) else 0
        rows.append(("缓存命中", f"{rate:.0f}%"))
        rows.append(("prompt", f'{u["prompt"]:,}'))
        rows.append(("输出", f'{u["completion"]:,}'))
    return '<div class="d-meta">' + "".join(
        f'<div class="m-row"><span class="m-k">{_e(k)}</span>'
        f'<span class="m-v">{_e(v)}</span></div>' for k, v in rows) + "</div>"


def _detail_preview(s: dict) -> str:
    """第一层下面：分段展示参数、思考、内容（不折叠，整块滚动）。"""
    out = []

    for tc in s["tool_calls"]:
        out.append(_sec(f'→ {tc["name"]} 参数', tc["args"]))
    if s["role"] == "tool" and s["paired_args"]:
        out.append(_sec(f'← {s["paired_name"] or "工具"} 原始参数', s["paired_args"]))

    if s["reasoning"]:
        body = s["reasoning"]
        short = body if len(body) <= 800 else \
            body[:800] + "\n…（预览已截断，切到「完整结果」看全文）"
        out.append(_sec(f"思考 · {len(body)} 字", short))

    if s["content"]:
        body = s["content"]
        short = body if len(body) <= 1500 else \
            body[:1500] + "\n…（预览已截断，切到「完整结果」看全文）"
        label = "工具结果" if s["role"] == "tool" else "正文"
        out.append(_sec(f"{label} · {len(body)} 字", short, err=s["is_error"]))

    return "".join(out) or '<div class="muted">这一步没有内容</div>'


def _detail_full(s: dict) -> str:
    """第二层：不截断的完整内容。"""
    out = []
    for tc in s["tool_calls"]:
        out.append(_sec(f'→ {tc["name"]} 参数', tc["args"]))
    if s["role"] == "tool" and s["paired_args"]:
        out.append(_sec(f'← {s["paired_name"] or "工具"} 原始参数', s["paired_args"]))
    if s["reasoning"]:
        out.append(_sec("思考", s["reasoning"]))
    if s["content"]:
        label = "工具结果" if s["role"] == "tool" else "正文"
        out.append(_sec(label, s["content"], err=s["is_error"]))
    return "".join(out) or '<div class="muted">这一步没有内容</div>'


def _render_step(s: dict) -> str:
    """只渲染一行摘要；详情由 _render_detail 放进右侧面板。"""
    kind = _kind_of(s)
    d = s["delta"]
    n = s["n"]

    tags = ""
    if s["is_error"]:
        tags += '<span class="tag err">错误</span>'
    if s["role"] == "async":
        tags += '<span class="tag async">异步</span>'
    if s["is_injected"]:
        tags += '<span class="tag inj">注入</span>'

    summary = _row_summary(s)
    if len(summary) > 120:
        summary = summary[:120] + "…"

    label = "注入" if s["is_injected"] else ROLE_LABEL.get(s["role"], s["role"])
    return (
        f'<div class="row {kind}{_dur_class(d)}" data-step="{n}">'
        f'<span class="type {kind}">{_e(label)}</span>'
        f'<span class="summary">{_e(summary)}</span>'
        f'{tags}'
        f'<span class="dur">{_fmt_dur(d)}</span>'
        f'</div>'
    )


def _render_detail(s: dict) -> str:
    """右侧面板里的详情块（默认隐藏，点行时显示）。"""
    n = s["n"]
    label = "注入" if s["is_injected"] else ROLE_LABEL.get(s["role"], s["role"])
    kind = _kind_of(s)
    turn = s.get("turn")
    turn_txt = f'第 {turn} 轮' if turn else "无轮次"
    return (
        f'<div class="detail" data-step="{n}" hidden>'
        f'<div class="d-head">'
        f'<span class="type {kind}">{_e(label)}</span>'
        f'<span class="dot">·</span><span>{_e(turn_txt)}</span>'
        f'<span class="dot">·</span><span>第 {n} 步</span>'
        f'</div>'
        f'<div class="tabs">'
        f'<button class="tab active" data-pane="preview">预览</button>'
        f'<button class="tab" data-pane="full">完整结果</button>'
        f'</div>'
        f'<div class="pane" data-pane="preview">{_meta_rows(s)}{_detail_preview(s)}</div>'
        f'<div class="pane" data-pane="full" hidden>{_detail_full(s)}</div>'
        f'</div>'
    )


def _render_turn(t, default_open: bool) -> str:
    dur = _fmt_dur(t["duration"])
    label = f'第 {t["index"]} 轮' if t["index"] else "（无用户消息）"
    head = (
        f'<div class="turn-head">'
        f'<span class="idx">{label}</span>'
        f'<span class="sep"></span>'
        f'<span class="turn-meta">{_e(t["start_ts"][11:23])} → {_e(t["end_ts"][11:23])}'
        f' · {dur} · {t["n_steps"]} 步</span>'
        f'</div>'
    )
    body = "".join(_render_step(s) for s in t["steps"])
    return f'<section class="turn" data-open="{1 if default_open else 0}">{head}{body}</section>'


def _render_timeline(turns, steps) -> str:
    segs = []
    for s in steps:
        d = s["delta"]
        if d is None or d <= 0:
            continue
        kind = _kind_of(s)
        w = max(d, 0.02)
        title = f'#{s["n"]} {s["role"]} {_fmt_dur(d)}'
        if s["paired_name"]:
            title += f' · {s["paired_name"]}'
        segs.append(
            f'<div class="seg {kind}" style="flex:{w:.4f}" title="{_e(title)}"></div>'
        )
    return '<div class="timeline">' + "".join(segs) + "</div>"


def _render_bar(summary) -> str:
    llm = summary["llm_time"]
    tool = summary["tool_time"]
    async_ = summary["async_time"]
    other = summary["other_time"]
    total = (llm + tool + async_ + other) or 1
    segs = []
    for label, val, cls in (("LLM", llm, "llm"), ("工具", tool, "tool"),
                            ("异步回填", async_, "async"), ("其他", other, "other")):
        if val <= 0:
            continue
        p = val / total * 100
        segs.append(f'<div class="bar-seg {cls}" style="width:{p:.2f}%">'
                    f'{label} {p:.0f}%</div>')
    return '<div class="bar">' + "".join(segs) + "</div>"


def build_html(session_id: str, analysis_href: str | None = None) -> str:
    memory_file = os.path.join(MEMORY_ROOT, session_id, "memory.jsonl")
    if not os.path.exists(memory_file):
        return (f"<html><body><h2>找不到会话 {_e(session_id)}</h2>"
                f"<p>{_e(memory_file)}</p></body></html>")

    if analysis_href is None:
        analysis_href = f"/api/analysis/{session_id}"

    steps = load_steps(memory_file)
    turns = group_turns(steps)
    summary = build_summary(turns, steps)

    slowest = summary["slowest"]
    slowest_txt = (f'{_e(slowest["name"])} · {_fmt_dur(slowest["dur"])}'
                   if slowest else "—")

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">轮内耗时</div><div class="v">{summary['wall']}s
        <span class="sub">跨度 {summary['span']}s</span></div></div>
      <div class="card"><div class="k">轮 / 步</div><div class="v">{summary['turns']} / {summary['steps']}</div></div>
      <div class="card"><div class="k">工具调用</div><div class="v">{summary['tool_calls']}
        <span class="sub">错误 {summary['tool_errors']}</span></div></div>
      <div class="card"><div class="k">输入 tokens</div><div class="v">{summary['prompt']:,}
        <span class="sub">命中 {summary['cache_rate'] * 100:.1f}%</span></div></div>
      <div class="card"><div class="k">输出 tokens</div><div class="v">{summary['completion']:,}</div></div>
      <div class="card"><div class="k">最慢一步</div><div class="v small">{slowest_txt}</div></div>
    </div>
    """

    turns_html = "".join(
        _render_turn(t, default_open=(i == len(turns) - 1))
        for i, t in enumerate(turns)
    )
    details_html = "".join(_render_detail(s) for t in turns for s in t["steps"])

    legend = "".join(
        f'<span class="lg"><i style="background:{c}"></i>{k}</span>'
        for k, c in (("LLM", KIND_COLOR["llm"]), ("工具", KIND_COLOR["tool"]),
                     ("异步回填", KIND_COLOR["async"]), ("用户", KIND_COLOR["user"]))
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>trace · {_e(session_id)}</title>
<style>
  :root {{ --bg:#f7f8fa; --card:#fff; --bd:#e3e6ec; --tx:#1d1d1f; --mut:#8a8f98; }}
  * {{ box-sizing:border-box; }}
  /* ── 细滚动条 ── */
  * {{ scrollbar-width:thin; scrollbar-color:#d9dee7 transparent; }}
  ::-webkit-scrollbar {{ width:10px; height:10px; }}
  ::-webkit-scrollbar-track {{ background:transparent; }}
  ::-webkit-scrollbar-thumb {{ background-color:#dde2ea; border-radius:10px;
                               border:3px solid transparent; background-clip:content-box; }}
  ::-webkit-scrollbar-thumb:hover {{ background-color:#c8d0dd; }}
  ::-webkit-scrollbar-corner {{ background:transparent; }}
  html, body {{ height:100%; }}
  body {{ margin:0; overflow:hidden; display:flex; flex-direction:column;
         background:var(--bg); color:var(--tx);
         font:13px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  /* 区域一：顶部汇总（固定，永不滚动） */
  .top {{ flex:none; width:100%; max-width:1180px; margin:0 auto; padding:22px 64px 0; }}
  /* 区域二 + 三：左列表可滚 / 右详情固定 */
  .main {{ flex:1; min-height:0; display:flex; gap:26px; width:100%;
           max-width:1180px; margin:0 auto; padding:12px 64px 20px; }}
  h1 {{ font-size:18px; margin:0 0 4px; }}
  .sub-title {{ color:var(--mut); font-size:12px; margin-bottom:18px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
            gap:10px; margin-bottom:16px; }}
  .card {{ background:var(--card); border:1px solid var(--bd); border-radius:10px; padding:10px 12px; }}
  .card .k {{ color:var(--mut); font-size:11px; }}
  .card .v {{ font-size:17px; font-weight:600; font-variant-numeric:tabular-nums; }}
  .card .v.small {{ font-size:13px; }}
  .card .sub {{ font-size:11px; color:var(--mut); font-weight:400; margin-left:4px; }}
  .bar {{ display:flex; height:26px; border-radius:6px; overflow:hidden; margin-bottom:8px;
          border:1px solid var(--bd); background:#fff; font-size:11px; color:#fff; }}
  .bar-seg {{ display:flex; align-items:center; justify-content:center; overflow:hidden; white-space:nowrap; }}
  .bar-seg.llm {{ background:{KIND_COLOR['llm']}; }}
  .bar-seg.tool {{ background:{KIND_COLOR['tool']}; }}
  .bar-seg.async {{ background:{KIND_COLOR['async']}; }}
  .bar-seg.other {{ background:#adb5bd; }}
  .legend {{ margin:0 0 8px; }}
  .lg {{ margin-right:14px; color:var(--mut); font-size:11px; }}
  .lg i {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; }}
  .timeline {{ display:flex; height:14px; border-radius:4px; overflow:hidden;
               margin-bottom:0; background:#eef0f4; }}
  .seg {{ min-width:1px; }}
  .seg.llm {{ background:{KIND_COLOR['llm']}; }}
  .seg.tool {{ background:{KIND_COLOR['tool']}; }}
  .seg.async {{ background:{KIND_COLOR['async']}; }}
  .seg.user {{ background:{KIND_COLOR['user']}; }}
  .seg.inj {{ background:{KIND_COLOR['inj']}; }}
  /* ── 轮分界线 ── */
  .turn-head {{ display:flex; align-items:center; gap:14px; margin:30px 0 4px; }}
  .turn-head:first-of-type {{ margin-top:6px; }}
  .turn-head .idx {{ font-weight:700; font-size:13px; color:#111318; white-space:nowrap; }}
  .turn-head .sep {{ flex:1; height:1px;
                     background:linear-gradient(to right,#c3cad8 0%,#e8ecf3 70%,transparent 100%); }}
  .turn-head .turn-meta {{ color:var(--mut); font-size:11.5px;
                           font-variant-numeric:tabular-nums; white-space:nowrap; }}

  /* ── 一行摘要 ── */
  .row {{ display:flex; align-items:center; gap:10px; padding:7px 10px;
          border-radius:7px; cursor:pointer; border-left:3px solid transparent; }}
  .row:hover {{ background:#edf1f8; }}
  .row.slow1 {{ border-left-color:#f0b429; }}
  .row.slow2 {{ border-left-color:#e03131; background:#fff7f7; }}
  .row .type {{ flex:none; width:76px; font-size:11px; font-weight:700; letter-spacing:.3px; }}
  .row .summary {{ flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
                   white-space:nowrap; color:#2a2d34; font-size:12.5px; }}
  .row .dur {{ flex:none; width:64px; text-align:right; font-variant-numeric:tabular-nums;
               font-size:11.5px; color:#6b7280; }}
  .row .type.user {{ color:#0b8a63; }}
  .row .type.llm {{ color:#2f6fe4; }}
  .row .type.tool {{ color:#d97706; }}
  .row .type.async {{ color:#dc2626; }}
  .row .type.inj {{ color:#8a8f98; }}

  .tag {{ flex:none; font-size:10px; padding:1px 6px; border-radius:4px;
          color:#fff; line-height:1.5; }}
  .tag.err {{ background:#e03131; }}
  .tag.async {{ background:{KIND_COLOR['async']}; }}
  .tag.inj {{ background:#adb5bd; }}

  /* ── 左列表（唯一滚动区）/ 右侧固定详情 ── */
  .list {{ flex:1 1 auto; min-width:0; overflow-y:auto; padding-right:6px; }}
  .side {{ flex:0 0 44%; display:flex; flex-direction:column; overflow:hidden;
           background:var(--card); border:1px solid var(--bd);
           border-radius:10px; padding:16px 18px; }}
  .side-empty {{ flex:1; display:flex; align-items:center; justify-content:center;
                 color:var(--mut); font-size:12px; text-align:center; }}
  body.has-sel .side-empty {{ display:none; }}
  .side-body {{ flex:1; min-height:0; display:flex; flex-direction:column; }}
  .side-body > .detail:not([hidden]) {{ flex:1; min-height:0; display:flex;
                                        flex-direction:column; }}
  .row.sel {{ background:#e3ecff; }}
  .row.sel .summary {{ color:#1b3d8f; }}

  /* ── 详情：头部与标签固定，只有内容区滚动 ── */
  .d-head {{ flex:none; display:flex; align-items:center; gap:7px;
             font-size:12.5px; color:#4b5563; margin-bottom:2px; }}
  .d-head .type {{ font-size:12.5px; font-weight:700; }}
  .d-head .dot {{ color:#c8cfdb; }}
  .tabs {{ flex:none; display:flex; gap:24px; margin:14px 0 0;
           border-top:1px solid #eef1f5; border-bottom:1px solid #eef1f5; }}
  .pane {{ flex:1; min-height:0; overflow-y:auto; padding-right:8px; }}
  .tab {{ border:none; background:none; font-family:inherit; font-size:12px;
          line-height:1; color:#6b7280; padding:10px 1px 9px; cursor:pointer;
          position:relative; transition:color .12s ease; }}
  .tab:hover {{ color:#2f6fe4; }}
  .tab.active {{ color:#2f6fe4; font-weight:600; }}
  .tab.active::after {{ content:""; position:absolute; left:0; right:0; bottom:-1px;
                        height:2px; background:#2f6fe4; }}

  /* 第二层：每项一行的信息 */
  .d-meta {{ padding:4px 0 2px; }}
  .m-row {{ display:flex; align-items:baseline; gap:14px; padding:6px 0;
            border-bottom:1px solid #f2f4f8; }}
  .m-row:last-child {{ border-bottom:none; }}
  .m-k {{ flex:none; width:78px; color:#9aa1ae; font-size:11.5px; }}
  .m-v {{ color:#24272e; font-size:12px; font-variant-numeric:tabular-nums;
          word-break:break-all; }}

  /* 第三层：标题 + 淡分割线 + 内容 + 淡分割线 */
  .sec-t {{ font-size:11.5px; font-weight:600; color:#6b7280; margin:16px 0 8px;
            padding-bottom:6px; border-bottom:1px solid #eef1f5; letter-spacing:.2px; }}
  .sec pre {{ margin:0; }}
  .sec::after {{ content:""; display:block; margin-top:14px;
                 border-top:1px solid #eef1f5; }}
  .sec.err .sec-t {{ color:#c92a2a; }}
  .muted {{ color:var(--mut); font-size:12px; }}
  pre {{ margin:6px 0 0; white-space:pre-wrap; word-break:break-word; color:#24272e;
         font:11.5px/1.6 ui-monospace,Consolas,monospace; }}

  /* ── 标题行 + 分析入口 ── */
  .top-head {{ display:flex; align-items:center; justify-content:space-between; gap:16px; }}
  .btn-analysis {{ flex:none; border:1px solid #d7dce6; background:#fff; color:#2f6fe4;
                   text-decoration:none; font-size:12px; padding:5px 13px; border-radius:6px;
                   transition:background .12s ease, border-color .12s ease; }}
  .btn-analysis:hover {{ background:#eef3ff; border-color:#b9c9ea; }}
  .toolbar {{ margin:18px 0 6px; }}
  .toolbar button {{ border:1px solid var(--bd); background:#fff; border-radius:6px;
                     padding:4px 11px; cursor:pointer; font-size:11.5px;
                     font-family:inherit; margin-right:6px; color:#4b5563; }}
  .toolbar button:hover {{ border-color:#b9c2d4; }}
</style></head><body>
<div class="top">
  <div class="top-head">
    <h1>Trace · {_e(session_id)}</h1>
    <a class="btn-analysis" href="{_e(analysis_href)}" target="_blank" rel="noopener">分析 →</a>
  </div>
  <div class="sub-title">{_e(memory_file)}</div>
  {cards}
  {_render_bar(summary)}
  <div class="legend">{legend}</div>
  {_render_timeline(turns, steps)}
</div>
<div class="main">
  <main class="list">{turns_html}</main>
  <aside class="side" id="side">
    <div class="side-empty" id="side-empty">点左侧任意一行，详情在这里展开</div>
    <div class="side-body" id="side-body">{details_html}</div>
  </aside>
</div>
<script>
function closeSide() {{
  document.body.classList.remove('has-sel');
  document.querySelectorAll('.detail').forEach(function (d) {{ d.hidden = true; }});
  document.querySelectorAll('.row').forEach(function (r) {{ r.classList.remove('sel'); }});
}}
document.querySelectorAll('.row').forEach(function (r) {{
  r.addEventListener('click', function () {{
    var step = r.dataset.step;
    var already = r.classList.contains('sel');
    closeSide();
    if (already) return;
    document.body.classList.add('has-sel');
    r.classList.add('sel');
    var d = document.querySelector('.detail[data-step="' + step + '"]');
    if (d) {{
      d.hidden = false;
      var p = d.querySelector('.pane:not([hidden])');
      if (p) p.scrollTop = 0;
      if (location.hash !== '#s' + step) history.replaceState(null, '', '#s' + step);
    }}
  }});
}});
document.querySelectorAll('.detail .tab').forEach(function (b) {{
  b.addEventListener('click', function (ev) {{
    ev.stopPropagation();
    var box = b.closest('.detail');
    box.querySelectorAll('.tab').forEach(function (x) {{
      x.classList.toggle('active', x === b);
    }});
    box.querySelectorAll('.pane').forEach(function (p) {{
      p.hidden = (p.dataset.pane !== b.dataset.pane);
    }});
    var shown = box.querySelector('.pane:not([hidden])');
    if (shown) shown.scrollTop = 0;
  }});
}});
// 支持通过 #s<step> 直接定位某一步
(function () {{
  var m = location.hash.match(/^#s(\\d+)$/);
  if (!m) return;
  var row = document.querySelector('.row[data-step="' + m[1] + '"]');
  if (row) row.click();
}})();
// 点击异常清单里的「步 N」时同样能跳转
window.addEventListener('hashchange', function () {{
  var m = location.hash.match(/^#s(\\d+)$/);
  if (!m) return;
  var row = document.querySelector('.row[data-step="' + m[1] + '"]');
  if (row && !row.classList.contains('sel')) row.click();
}});
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    sid = a.session or latest_session()
    if not sid:
        print("[x] memory/ 下没有可用会话")
        return 1

    out = a.out or os.path.join(MEMORY_ROOT, sid, "trace.html")
    out = os.path.abspath(out)
    ana_out = os.path.join(os.path.dirname(out), f"{sid}.analysis.html")

    # 本地打开时按钮指向同目录的分析页；服务端打开时指向 /api/analysis/<sid>
    doc = build_html(sid, analysis_href=os.path.basename(ana_out))
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)

    from .analyzer import build_analysis_page
    with open(ana_out, "w", encoding="utf-8") as f:
        f.write(build_analysis_page(sid, trace_href=os.path.basename(out)))

    # 终端摘要
    memory_file = os.path.join(MEMORY_ROOT, sid, "memory.jsonl")
    steps = load_steps(memory_file)
    turns = group_turns(steps)
    s = build_summary(turns, steps)
    print("=" * 62)
    print(f"会话 {sid}")
    print("=" * 62)
    print(f"  轮 / 步        {s['turns']} / {s['steps']}")
    print(f"  轮内耗时       {s['wall']}s   （会话跨度 {s['span']}s，轮间空闲 {s['idle']}s）")
    print(f"  LLM            {s['llm_time']}s")
    print(f"  工具           {s['tool_time']}s")
    print(f"  异步回填       {s['async_time']}s")
    print(f"  其他           {s['other_time']}s")
    if s["wall"]:
        print(f"  时间占比       LLM {s['llm_time'] / s['wall'] * 100:.0f}% · "
              f"工具 {s['tool_time'] / s['wall'] * 100:.0f}% · "
              f"异步 {s['async_time'] / s['wall'] * 100:.0f}%")
    print(f"  工具调用       {s['tool_calls']} 次（错误 {s['tool_errors']}）")
    print(f"  tokens         输入 {s['prompt']:,}（缓存 {s['cache_rate'] * 100:.1f}%）"
          f" / 输出 {s['completion']:,}")
    if s["slowest"]:
        print(f"  最慢一步       #{s['slowest']['step']} {s['slowest']['name']}"
              f" {s['slowest']['dur']}s")
    print()
    print(f"  Trace    : {out}")
    print(f"  分析器   : {ana_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

