import os
from tools.Toolregistry import tool


@tool(name="splitPaper", description="把一个 MinerU 解析出的 full.md 按章节切分成多个小 md 文件，并清理图片链接等噪音。"
                                       "切分粒度：顶层大节（如 1.Introduction、3.Method、4.Experiments）一个文件，子章节归入所属大节保持连贯性，"
                                       "消融(Ablation)单独拎出，附录(Appendix)单独标记。"
                                       "参数 md_path 是 full.md 的绝对路径（必填）。"
                                       "切分后会在 full.md 同目录生成 *_split 文件夹，内含各章节 md 文件 + _sections.json 清单。"
                                       "返回每个章节的【文件名】、类别、标题与字符数，可直接用于 read_file，"
                                       "不要自行推测文件名（文件名带序号前缀、经过 slug 化，长标题还会被截断，猜不出来）。"
                                       "字符数为 0 的章节是被相邻标题合并吸收的空壳，仍会列出以保留章节结构，但无需读取。")
def splitPaper(md_path: str) -> str:
    from utils.md_splitter import split_markdown

    md_path = os.path.abspath(md_path)
    manifest = split_markdown(md_path)

    base = os.path.splitext(os.path.basename(md_path))[0]
    out_dir = os.path.join(os.path.dirname(md_path), base + "_split")

    # 输出目录只给一次：逐条拼全路径会把上下文撑大（目录名本身就有 150+ 字符）
    lines = [
        f"切分完成，共 {len(manifest)} 个章节。",
        f"输出目录：{out_dir}",
        "读取章节时用「输出目录 + 文件名」拼接；文件名见下文，不要自己推测。",
        "",
    ]
    for s in manifest:
        if s["chars"] == 0:
            size = "空（被相邻标题合并吸收，保留以体现章节结构，无需读取）"
        else:
            size = f"{s['chars']} 字符"
        lines.append(f"- {s['file']}  [{s['category']}] {s['title']}  {size}")
    return "\n".join(lines)
