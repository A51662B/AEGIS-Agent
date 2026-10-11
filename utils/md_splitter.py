# -*- coding: utf-8 -*-
"""把 MinerU 转出的 full.md 按章节切分成小 md 文件，并清理噪音。

针对 MinerU 输出的鲁棒性设计：
- 所有章节标题都是 `##`（层级靠编号 `1.` / `2.1.` / `3.1.1.` 区分，不靠 # 数量），
  所以按 `^#{1,4}` 标题行切，不依赖 # 层级。
- 无编号章节（Abstract / Acknowledgments / References）单独成文件。
- 噪音清理：图片链接 `![](images/...)` 整行删掉、压缩连续空行。
- 标题可能挤在一起（`## 3.1. A 3.1.1. B`）→ 拆成两个章节。

用法:
    from utils.md_splitter import split_markdown
    sections = split_markdown("path/full.md", output_dir=None)
    # sections 是清单列表，每个元素含 index/title/num/top_num/category/file/chars
"""
import os
import re
import json
import shutil

# 章节名 -> 阅读类别 的关键词映射（用户阅读习惯，可自行增删）
CATEGORY_RULES = [
    ("摘要",      ["abstract", "摘要"]),
    ("技术介绍",  ["introduction", "引言", "introduc"]),
    ("相关研究",  ["related work", "相关工作", "related", "background", "背景"]),
    ("技术讲解",  ["method", "approach", "model", "architecture", "方法", "模型"]),
    ("实验",      ["experiment", "dataset", "result", "实验", "数据集", "结果"]),
    ("消融",      ["ablation", "消融"]),
    ("结论",      ["conclusion", "结论"]),
    ("致谢",      ["acknowledg", "致谢"]),
    ("引用",      ["reference", "引用", "参考文献"]),
]
DEFAULT_CATEGORY = "其他"

TITLE_RE = re.compile(r"^#{1,4}\s+(.*)$")
# 行内图片链接（段落里的 ![](...)）
IMG_INLINE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 编号标题：1. / 2.1. / 3.1.1. 前缀
NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")
# 标题文字里夹着的"第二个编号"（如 "3.1. A 3.1.1. B"），用于拆分挤在一起的标题
SECOND_NUM_RE = re.compile(r"\s+(\d+\.\d+(?:\.\d+)*)\.\s+")


def _classify(name: str) -> str:
    """根据章节名关键词判断阅读类别。"""
    low = name.lower()
    for cat, kws in CATEGORY_RULES:
        for kw in kws:
            if kw in low:
                return cat
    return DEFAULT_CATEGORY


def _parse_title(title: str):
    """解析标题 → (num, top_num, name, category)。
    num: 完整编号（"3.1.1"），无编号为 None
    top_num: 顶层编号（"3"），无编号为 None
    name: 去编号后的标题名
    """
    m = NUM_RE.match(title)
    if m:
        num = m.group(1)
        name = m.group(2)
        top_num = num.split(".")[0]
    else:
        num = None
        top_num = None
        name = title
    return num, top_num, name, _classify(name)


def _safe_name(idx: int, name: str) -> str:
    """生成安全的文件名（去非法字符、截断）。"""
    safe = re.sub(r'[\\/:*?"<>|]', "", name)
    safe = re.sub(r"\s+", "_", safe.strip())
    safe = safe[:60]
    return f"{idx:02d}_{safe}.md" if safe else f"{idx:02d}.md"


def _clean_lines(lines):
    """清理章节正文：删图片链接、压缩连续空行。"""
    out = []
    blank = 0
    for line in lines:
        line = IMG_INLINE_RE.sub("", line).rstrip()
        if not line.strip():
            blank += 1
            if blank <= 1:  # 最多保留一个空行
                out.append("")
            continue
        blank = 0
        out.append(line)
    # 去掉结尾多余空行
    while out and out[-1] == "":
        out.pop()
    return out


def split_markdown(md_path: str, output_dir: str = None) -> list:
    """切分 full.md 成章节文件，返回清单列表。

    参数:
        md_path: full.md 的绝对路径
        output_dir: 输出目录（默认 `<md所在目录>/<md名去后缀>_split/`）

    返回:
        清单列表，每项 {index, title, num, top_num, category, chars, lines, file}
    """
    md_path = os.path.abspath(md_path)
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"文件不存在: {md_path}")
    if output_dir is None:
        base = os.path.splitext(os.path.basename(md_path))[0]
        output_dir = os.path.join(os.path.dirname(md_path), base + "_split")
    # 清空旧输出（避免上次运行的残留文件）
    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    with open(md_path, encoding="utf-8") as f:
        lines = f.read().split("\n")

    # 1. 识别标题行（保留行号 + 标题文字 + 级别）
    headings = []  # [(line_idx, raw_title, level)]
    for i, line in enumerate(lines):
        m = TITLE_RE.match(line)
        if m:
            level = len(line) - len(line.lstrip("#"))
            headings.append((i, m.group(1).strip(), level))

    if not headings:
        raise ValueError(f"未在 {md_path} 中找到任何标题（# 开头），无法切分")

    # 2. 按"顶层大节"切分（子章节归入所属顶层，保持连贯性；消融子章节单独拎出）
    chunks = []  # [(title, body_lines, is_appendix, is_h1)]
    seen_h1 = 0
    current_top = -1  # 当前顶层 chunk 索引（子章节归入这里，消融拎出不影响它）
    for k, (line_i, raw_title, level) in enumerate(headings):
        if level == 1:
            seen_h1 += 1
        is_appendix = seen_h1 > 1  # 第二个一级标题之后属于附录（arXiv Appendix）
        is_h1 = (level == 1)

        end = headings[k + 1][0] if k + 1 < len(headings) else len(lines)
        num, top_num, name, _ = _parse_title(raw_title)

        # 伪标题：正文被 MinerU 误识别成标题（无编号 + 以冒号结尾），降级为正文归入当前顶层
        if raw_title.rstrip().endswith(":") and num is None and current_top >= 0:
            prev = chunks[current_top]
            body = prev[1] + [raw_title] + lines[line_i + 1:end]
            chunks[current_top] = (prev[0], body, prev[2], prev[3])
            continue

        # 是否开新章节：顶层标题（无编号 或 单数字编号）或 消融子章节
        is_top = num is None or "." not in num
        is_ablation = "ablation" in name.lower() or "消融" in name

        if is_top:
            chunks.append((raw_title, lines[line_i + 1:end], is_appendix, is_h1))
            current_top = len(chunks) - 1
        elif is_ablation:
            # 消融单独拎出，但不更新 current_top（其后子章节仍归回顶层大节）
            chunks.append((raw_title, lines[line_i + 1:end], is_appendix, is_h1))
        elif current_top >= 0:
            # 普通子章节：归入当前顶层大节（保留子标题行，维持连贯性）
            prev = chunks[current_top]
            body = prev[1] + [lines[line_i]] + lines[line_i + 1:end]
            chunks[current_top] = (prev[0], body, prev[2], prev[3])
        else:
            # 兜底：首个标题就是子章节（无顶层），单独成章
            chunks.append((raw_title, lines[line_i + 1:end], is_appendix, is_h1))
            current_top = len(chunks) - 1

    # 3. 清理 + 写文件 + 生成清单
    manifest = []
    for idx, (title, body, is_appendix, is_h1) in enumerate(chunks):
        num, top_num, name, category = _parse_title(title)
        if is_h1:
            category = "标题"
        elif is_appendix:
            category = "附录"
        clean = _clean_lines(body)
        text = "\n".join(clean)
        chars = len(text)

        fname = _safe_name(idx, name)
        fpath = os.path.join(output_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"## {title}\n\n" if not NUM_RE.match(title) else f"## {title}\n\n")
            f.write(text)
            if text:
                f.write("\n")

        manifest.append({
            "index": idx,
            "title": title,
            "num": num,
            "top_num": top_num,
            "category": category,
            "appendix": is_appendix,
            "chars": chars,
            "file": fname,
        })

    # 4. 写清单
    manifest_path = os.path.join(output_dir, "_sections.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[split] 共切出 {len(manifest)} 个章节 -> {output_dir}")
    return manifest


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python md_splitter.py <full.md路径> [输出目录]")
        sys.exit(1)
    md = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    result = split_markdown(md, out)
    for s in result:
        print(f"  [{s['num'] or '-'}] {s['category']:<6} {s['title'][:40]:<42} {s['chars']}字符 -> {s['file']}")
