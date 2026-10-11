"""论文批量阅读 → 自动写报告 工作流（LangGraph）。

自包含、可移植：不依赖项目里的 ChatModel / Message / tools 结构，
只依赖：
  - langgraph
  - openai（直接读环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / REPORT_MODEL）
  - 同目录的 mineru_pdf.py（PDF 批量转 md）

用法（命令行）:
    python report_workflow.py <PDF文件夹> [输出md路径]

或在代码里:
    from report_workflow import run
    run("D:/论文", output_path="D:/报告.md")

流程:
    scan_pdfs（扫文件夹）
      → parse_batch（MinerU 批量转 md，超时跳过慢的）
      → check_parse（人类介入：报告成功/未完成，选 retry 或 continue）
          ├─ retry 且还有未完成 → 回到 parse_batch（最多重试 MAX_RETRY 次）
          └─ continue / 全部完成 → write_report 循环（只对解析成功的写报告）
      → aggregate（汇总成一个 md）
"""
import os
import sys
from typing import TypedDict

from dotenv import load_dotenv
from openai import OpenAI
from langgraph.graph import StateGraph, END, START
from langgraph.types import interrupt, Command, RetryPolicy
from langgraph.checkpoint.memory import InMemorySaver

# 让同目录的 mineru_pdf 能被 import（无论从哪运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mineru_pdf import pdfs_to_markdown  # noqa: E402

load_dotenv()

# ============ 可调参数 ============
PARSE_TIMEOUT = 300        # 单轮 MinerU 解析总超时（秒），慢的跳过
MAX_RETRY = 2              # 解析失败最多重试次数
MAX_INPUT_CHARS = 12000    # 喂给 LLM 的每篇 md 最大字符数（覆盖标题+摘要+方法开头）
REPORT_MAX_TOKENS = 3000   # 单篇报告输出 token 上限（500 字中文 + 英文术语，留足余量防截断）

SYSTEM_PROMPT = """你是一个学术论文阅读助手。请根据提供的论文内容，写一份简短的中文阅读报告，约 500 字。

严格按以下格式输出（markdown）：

### [论文标题]

- **领域**：这篇论文属于什么研究领域/方向
- **发现的问题**：领域内存在什么问题、痛点或挑战
- **解决了什么**：这篇论文解决了什么具体问题
- **技术/方法**：采用了什么技术、方法或模型
- **是否开源**：如果摘要/内容中明确提到代码开源并给出链接，写"开源（链接）"；否则写"未开源"

要求：
1. 只依据提供的论文内容，不要编造
2. 语言简洁，每点 1-2 句话
3. 内容里没有足够信息时，写"未提及"
"""


# ============ State ============
class State(TypedDict, total=False):
    folder: str            # 输入 PDF 文件夹
    pdf_paths: list[str]   # 有序的 PDF 路径列表
    md_map: dict           # {pdf路径: markdown文本}，只含解析成功的
    pending: list[str]     # 未解析成功的 pdf 路径
    reports: list[dict]    # [{path, title, report}]
    current_index: int     # 遍历 pdf_paths 的游标
    retry_count: int       # 解析重试次数
    decision: str          # 人类介入的选择：retry / continue
    output_path: str       # 最终汇总 md 路径


# ============ 独立 LLM 调用（不依赖项目 ChatModel）============
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
    return _client


def _extract_title(md: str, fallback: str) -> str:
    """从 markdown 开头提取论文标题（第一个 # 开头的行）。"""
    for line in md.splitlines()[:30]:
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title
    return fallback


def _generate_report(path: str, md: str) -> str:
    """对单篇 md 生成报告（独立 LLM 调用）。"""
    content = md[:MAX_INPUT_CHARS]
    client = _get_client()
    resp = client.chat.completions.create(
        model=os.getenv("REPORT_MODEL", "deepseek-flash"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"论文文件：{os.path.basename(path)}\n\n论文内容（摘要和方法部分）：\n{content}",
            },
        ],
        temperature=0.7,
        max_tokens=REPORT_MAX_TOKENS,
        timeout=120,
    )
    msg = resp.choices[0].message
    if resp.choices[0].finish_reason == "length":
        print(f"  [警告] 报告可能被截断（finish_reason=length）: {os.path.basename(path)}")
    return msg.content


# ============ 节点 ============
def _find_existing_md(pdf_path: str, folder: str) -> str:
    """查找某 PDF 是否已有对应的 md（在同目录 md/ 子目录下，或同目录下）。"""
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    candidates = [
        os.path.join(folder, "md", stem + ".md"),
        os.path.join(os.path.dirname(pdf_path), stem + ".md"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def scan_pdfs(state: State) -> dict:
    """扫描文件夹，列出所有 PDF，并复用已转好的 md（跳过重复解析）。"""
    folder = state["folder"]
    paths = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".pdf"):
                paths.append(os.path.join(root, f))
    paths.sort()
    if not paths:
        raise FileNotFoundError(f"文件夹里没有 PDF: {folder}")
    print(f"[scan] 找到 {len(paths)} 个 PDF")

    # 复用已存在的 md，只把「没有 md 的」放进 pending
    md_map = {}
    pending = []
    for p in paths:
        existing = _find_existing_md(p, folder)
        if existing:
            with open(existing, encoding="utf-8") as f:
                md_map[p] = f.read()
            print(f"  [scan] 复用已有 md: {os.path.basename(existing)}")
        else:
            pending.append(p)

    return {
        "pdf_paths": paths,
        "pending": pending,
        "md_map": md_map,
        "reports": [],
        "current_index": 0,
        "retry_count": 0,
        "decision": "",
    }


def parse_batch(state: State) -> dict:
    """用 MinerU 批量解析 pending 里的 PDF，成功的进 md_map，慢的/失败的留在 pending。"""
    pending = state["pending"]
    md_map = dict(state["md_map"])
    if not pending:
        return {"md_map": md_map, "pending": []}

    print(f"[parse] 解析 {len(pending)} 个 PDF（超时 {PARSE_TIMEOUT}s）...")
    result = pdfs_to_markdown(pending, timeout=PARSE_TIMEOUT)

    new_pending = []
    for p in pending:
        md = result.get(os.path.basename(p))
        if md:
            md_map[p] = md
        else:
            new_pending.append(p)

    print(f"[parse] 成功 {len(md_map)} 篇，未完成 {len(new_pending)} 篇")
    return {"md_map": md_map, "pending": new_pending}


def check_parse(state: State) -> dict:
    """人类介入：报告解析情况，让用户选 retry（重试）或 continue（放弃剩余）。"""
    if not state["pending"]:
        return {"decision": "continue"}

    decision = interrupt({
        "成功": len(state["md_map"]),
        "未完成": [os.path.basename(p) for p in state["pending"]],
    })
    # retry 计数 +1
    retry_count = state.get("retry_count", 0)
    if decision == "retry":
        retry_count += 1
    return {"decision": decision, "retry_count": retry_count}


def write_report(state: State) -> dict:
    """对当前（在 md_map 里的）这篇 md 生成报告，游标前进。"""
    idx = state["current_index"]
    paths = state["pdf_paths"]
    md_map = state["md_map"]
    reports = list(state["reports"])

    # 跳过未解析成功的
    while idx < len(paths) and paths[idx] not in md_map:
        idx += 1

    if idx < len(paths):
        path = paths[idx]
        title = _extract_title(md_map[path], os.path.basename(path))
        print(f"[report] 写报告 ({idx + 1}/{len(paths)}): {title}")
        report = _generate_report(path, md_map[path])
        reports.append({"path": path, "title": title, "report": report})
        idx += 1

    return {"current_index": idx, "reports": reports}


def aggregate(state: State) -> dict:
    """汇总所有报告，写成一个 md 文件。"""
    reports = state["reports"]
    output = state.get("output_path") or os.path.join(state["folder"], "reports.md")

    # 容错：如果 output 指向的是目录（或没带 .md 后缀），自动拼上文件名
    if os.path.isdir(output) or not output.lower().endswith(".md"):
        output = os.path.join(output, "reports.md")

    lines = ["# 论文阅读报告", "", f"共 {len(reports)} 篇", ""]
    for i, r in enumerate(reports, 1):
        lines.append(r["report"])
        lines.append("")

    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[aggregate] 已写出 {len(reports)} 篇报告 -> {output}")
    return {"output_path": output}


# ============ 条件边 ============
def route_after_check(state: State) -> str:
    """check_parse 之后的走向：retry 且还有未完成且没超上限 → 回 parse；否则写报告。"""
    if (
        state.get("decision") == "retry"
        and state.get("pending")
        and state.get("retry_count", 0) < MAX_RETRY
    ):
        return "parse_batch"
    return "write_report"


def route_after_write(state: State) -> str:
    """写报告之后：后面还有解析成功的 → 继续写；否则汇总。"""
    idx = state["current_index"]
    paths = state["pdf_paths"]
    md_map = state["md_map"]
    if any(p in md_map for p in paths[idx:]):
        return "write_report"
    return "aggregate"


# ============ 构图 ============
def build_graph():
    g = StateGraph(State)

    retry = RetryPolicy(
        retry_on=Exception,
        max_attempts=3,
        initial_interval=2.0,
        backoff_factor=2.0,
    )

    g.add_node("scan_pdfs", scan_pdfs)
    g.add_node("parse_batch", parse_batch)
    g.add_node("check_parse", check_parse)
    g.add_node("write_report", write_report, retry=retry)
    g.add_node("aggregate", aggregate)

    g.add_edge(START, "scan_pdfs")
    g.add_edge("scan_pdfs", "parse_batch")
    g.add_edge("parse_batch", "check_parse")
    g.add_conditional_edges("check_parse", route_after_check, ["parse_batch", "write_report"])
    g.add_conditional_edges("write_report", route_after_write, ["write_report", "aggregate"])
    g.add_edge("aggregate", END)

    return g.compile(checkpointer=InMemorySaver())


# ============ 运行入口 ============
def run(folder: str, output_path: str = None, auto: bool = False) -> dict:
    """运行工作流。auto=True 时关闭人类介入，自动重试到上限后跳过剩余。返回最终 state。"""
    app = build_graph()
    config = {"configurable": {"thread_id": "report_workflow"}}

    state = {"folder": folder}
    if output_path:
        state["output_path"] = output_path

    result = app.invoke(state, config)

    # 处理 interrupt（可能多轮 retry）
    while "__interrupt__" in result:
        if auto:
            # 自动模式：有未完成就重试，重试到上限就跳过剩余继续
            retry_count = result.get("retry_count", 0)
            choice = "retry" if retry_count < MAX_RETRY else "continue"
            print(f"[auto] 有未完成解析，自动选择: {choice}（已重试 {retry_count}/{MAX_RETRY} 次）")
        else:
            payload = result["__interrupt__"][0].value
            print("\n" + "=" * 50)
            print(f"解析完成情况：成功 {payload['成功']} 篇")
            if payload["未完成"]:
                print(f"未完成 {len(payload['未完成'])} 篇：")
                for name in payload["未完成"]:
                    print(f"  - {name}")
            print("=" * 50)
            choice = input("输入 retry（重试未完成的）/ continue（放弃未完成的，直接写报告）: ").strip().lower()
            if choice not in ("retry", "continue"):
                print("无效输入，默认 continue")
                choice = "continue"
        result = app.invoke(Command(resume=choice), config)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python report_workflow.py <PDF文件夹> [输出md路径]")
        sys.exit(1)
    folder = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else None
    final = run(folder, output)
    print("\n完成！最终输出:", final.get("output_path"))
