import os
from tools.Toolregistry import tool


SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def _scan_skills():
    """扫描 skills 目录，返回 [(名字, 能力描述)] 列表。

    规范：每个 .md 文件是一个 skill；文件名（去 .md）= 名字；文件第一行 `# 之后 = 能力描述。
    """
    result = []
    if not os.path.isdir(SKILLS_DIR):
        return result
    for fname in sorted(os.listdir(SKILLS_DIR)):
        if not fname.endswith(".md"):
            continue
        name = fname[:-3]  
        ability = name  
        path = os.path.join(SKILLS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
            if first.startswith("#"):
                ability = first.lstrip("#").strip()
        except Exception:
            pass
        result.append((name, ability))
    return result


_SKILLS = _scan_skills()


def _build_description():
    desc = (
        "这是一个 skilltool（技能工具），可以调用各种能力的 skill。"
        "参数 skill_name 传入某个 skill 的名字，工具会返回该 skill 的完整文字说明。"
        "当前可用的 skill："
    )
    if not _SKILLS:
        return desc + "（暂无）"
    for name, ability in _SKILLS:
        desc += f"\n- {name}：{ability}"
    return desc


@tool(name="skilltool", description=_build_description())
def skilltool(skill_name: str) -> str:
    """返回指定 skill 的完整文字内容（skill 是操作手册/能力说明，模型读后按其中规则执行）。"""
    if not skill_name:
        return "skill_name 不能为空。可用 skill：" + "、".join(n for n, _ in _SKILLS)

    # 允许传名字或名字加 .md
    target = skill_name if skill_name.endswith(".md") else skill_name + ".md"
    path = os.path.join(SKILLS_DIR, target)
    if not os.path.isfile(path):
        avail = "、".join(n for n, _ in _SKILLS) or "（暂无）"
        return f"未找到名为「{skill_name}」的 skill。当前可用的 skill：{avail}"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
