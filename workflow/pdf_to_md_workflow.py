"""PDF 批量转 Markdown 工作流（LangGraph）。

只做「PDF → Markdown」，不写报告。与 report_workflow 的区别：
  - 完整保留 MinerU 返回的所有内容（full.md + 图片 + 其他资源），
    下载结果 zip 后逐个解压成文件，再删掉 zip。
  - 不做 LLM 报告生成，不汇总。

自包含、可移植：只依赖：
  - langgraph
  - 同目录的 mineru_pdf.py（PDF 批量完整解压）

用法（命令行）:
    python pdf_to_md_workflow.py <PDF文件夹> [输出目录]

或在代码里:
    from pdf_to_md_workflow import run
    run("D:/论文", output_dir="D:/论文/md")

流程:
    scan_pdfs（扫文件夹，跳过已有解压结果的）
      → extract_batch（MinerU 批量完整解压）
      → check_parse（人类介入：报告成功/未完成，选 retry 或 continue）
          ├─ retry 且还有未完成 → 回到 extract_batch（最多 MAX_RETRY 次）
          └─ continue / 全部完成 → finish
"""
import os
import sys
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import StateGraph, END, START
from langgraph.types import interrupt, Command, RetryPolicy
from langgraph.checkpoint.memory import InMemorySaver

# 让同目录的 mineru_pdf 能被 import（无论从哪运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mineru_pdf import pdfs_extract_all  # noqa: E402

load_dotenv()

# ============ 可调参数 ============
EXTRACT_TIMEOUT = 300   # 单轮 MinerU 完整解压总超时（秒），慢的跳过
MAX_RETRY = 2           # 解析失败最多重试次数


# ============ State ============
class State(TypedDict, total=False):
    folder: str            # 输入 PDF 文件夹
    output_dir: str        # 解压根目录
    pdf_paths: list[str]   # 有序的 PDF 路径列表
    pending: list[str]     # 还没解压成功的 pdf 路径
    done: dict             # {pdf路径: 解压目录}，只含成功的
    retry_count: int       # 解析重试次数
    decision: str          # 人类介入的选择：retry / continue


# ============ 辅助 ============
def _find_existing_extract(pdf_path: str, output_dir: str) -> str:
    """查找某 PDF 是否已解压过（output_dir/<去后缀>/full.md 存在即算）。"""
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    full_md = os.path.join(output_dir, stem, "full.md")
    if os.path.isfile(full_md):
        return os.path.dirname(full_md)
    return None


# ============ 节点 ============
def scan_pdfs(state: State) -> dict:
    """扫描文件夹，列出所有 PDF，复用已解压的结果（跳过重复解析）。"""
    folder = state["folder"]
    output_dir = state["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    paths = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".pdf"):
                paths.append(os.path.join(root, f))
    paths.sort()
    if not paths:
        raise FileNotFoundError(f"文件夹里没有 PDF: {folder}")
    print(f"[scan] 找到 {len(paths)} 个 PDF")

    done = {}
    pending = []
    for p in paths:
        existing = _find_existing_extract(p, output_dir)
        if existing:
            done[p] = existing
            print(f"  [scan] 复用已有解压: {os.path.basename(existing)}")
        else:
            pending.append(p)

    return {
        "pdf_paths": paths,
        "pending": pending,
        "done": done,
        "retry_count": 0,
        "decision": "",
    }


def extract_batch(state: State) -> dict:
    """用 MinerU 批量完整解压 pending 里的 PDF，成功的进 done，失败的留 pending。"""
    pending = state["pending"]
    done = dict(state["done"])
    if not pending:
        return {"done": done, "pending": []}

    print(f"[extract] 完整解压 {len(pending)} 个 PDF（超时 {EXTRACT_TIMEOUT}s）...")
    result = pdfs_extract_all(pending, state["output_dir"], timeout=EXTRACT_TIMEOUT)

    new_pending = []
    for p in pending:
        dir_path = result.get(os.path.basename(p))
        if dir_path:
            done[p] = dir_path
        else:
            new_pending.append(p)

    print(f"[extract] 成功 {len(done)} 篇，未完成 {len(new_pending)} 篇")
    return {"done": done, "pending": new_pending}


def check_parse(state: State) -> dict:
    """人类介入：报告解析情况，让用户选 retry（重试）或 continue（放弃剩余）。"""
    if not state["pending"]:
        return {"decision": "continue"}

    decision = interrupt({
        "成功": len(state["done"]),
        "未完成": [os.path.basename(p) for p in state["pending"]],
    })
    retry_count = state.get("retry_count", 0)
    if decision == "retry":
        retry_count += 1
    return {"decision": decision, "retry_count": retry_count}


def finish(state: State) -> dict:
    """打印最终结果，结束。"""
    done = state["done"]
    print(f"[finish] 共解压 {len(done)} 个 PDF -> {state['output_dir']}")
    for p, d in done.items():
        print(f"  - {os.path.basename(p)} -> {d}")
    return {"done": done}


# ============ 条件边 ============
def route_after_check(state: State) -> str:
    """check_parse 之后的走向：retry 且还有未完成且没超上限 → 回 extract；否则结束。"""
    if (
        state.get("decision") == "retry"
        and state.get("pending")
        and state.get("retry_count", 0) < MAX_RETRY
    ):
        return "extract_batch"
    return "finish"


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
    g.add_node("extract_batch", extract_batch, retry=retry)
    g.add_node("check_parse", check_parse)
    g.add_node("finish", finish)

    g.add_edge(START, "scan_pdfs")
    g.add_edge("scan_pdfs", "extract_batch")
    g.add_edge("extract_batch", "check_parse")
    g.add_conditional_edges("check_parse", route_after_check, ["extract_batch", "finish"])
    g.add_edge("finish", END)

    return g.compile(checkpointer=InMemorySaver())


# ============ 运行入口 ============
def run(folder: str, output_dir: str = None, auto: bool = False) -> dict:
    """运行工作流。auto=True 时关闭人类介入，自动重试到上限后跳过剩余。返回最终 state。"""
    app = build_graph()
    config = {"configurable": {"thread_id": "pdf_to_md_workflow"}}

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"文件夹不存在: {folder}")
    if output_dir is None:
        output_dir = os.path.join(folder, "md")
    elif not os.path.isabs(output_dir):
        # 相对路径 → 相对于 PDF 目录解析（例如 "md_xxx" → folder/md_xxx）
        output_dir = os.path.join(folder, output_dir)
    output_dir = os.path.abspath(output_dir)

    state = {"folder": folder, "output_dir": output_dir}

    result = app.invoke(state, config)

    # 处理 interrupt（可能多轮 retry）
    while "__interrupt__" in result:
        if auto:
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
            choice = input("输入 retry（重试未完成的）/ continue（放弃未完成的）: ").strip().lower()
            if choice not in ("retry", "continue"):
                print("无效输入，默认 continue")
                choice = "continue"
        result = app.invoke(Command(resume=choice), config)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python pdf_to_md_workflow.py <PDF文件夹> [输出目录]")
        sys.exit(1)
    folder = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else None
    final = run(folder, output)
    print("\n完成！解压目录:", final.get("output_dir"))
