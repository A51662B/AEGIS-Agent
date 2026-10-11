import os
from tools.Toolregistry import tool


@tool(name="splitPaper", description="把一个 MinerU 解析出的 full.md 按章节切分成多个小 md 文件，并清理图片链接等噪音。"
                                       "切分粒度：顶层大节（如 1.Introduction、3.Method、4.Experiments）一个文件，子章节归入所属大节保持连贯性，"
                                       "消融(Ablation)单独拎出，附录(Appendix)单独标记。"
                                       "参数 md_path 是 full.md 的绝对路径（必填）。"
                                       "切分后会在 full.md 同目录生成 *_split 文件夹，内含各章节 md 文件 + _sections.json 清单。"
                                       "返回章节清单（含类别、标题、字符数），供后续按阅读习惯选择性加载章节。")
def splitPaper(md_path: str) -> str:
    from utils.md_splitter import split_markdown

    md_path = os.path.abspath(md_path)
    manifest = split_markdown(md_path)

    base = os.path.splitext(os.path.basename(md_path))[0]
    out_dir = os.path.join(os.path.dirname(md_path), base + "_split")

    lines = [f"切分完成，共 {len(manifest)} 个章节。输出目录：{out_dir}"]
    for s in manifest:
        lines.append(f"- [{s['category']}] {s['title']}（{s['chars']} 字符）")
    return "\n".join(lines)
