import os

from tools.Toolregistry import tool
from session_context import current_session
from session_store import session_store


_DEFAULT_WORKSPACE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace"
)

TEXT_EXTS = {
    ".md", ".py", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".csv", ".tsv", ".ini", ".cfg", ".log", ".html", ".css", ".js",
    ".jsx", ".ts", ".tsx", ".xml", ".conf", ".sh", ".bat",
}

MAX_READ_CHARS = 8000


def _get_workspace_root() -> str:
    """当前会话的 workspace 根目录；无会话上下文时回退到默认目录。"""
    sid = current_session.get()
    if sid:
        return session_store.workspace(sid)
    return _DEFAULT_WORKSPACE


def _resolve_read_path(path: str) -> str:
    """读操作：解析为绝对路径。绝对路径直接放行（任意位置都能读）；相对路径相对 workspace。"""
    root = _get_workspace_root()
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(root, path))


def _resolve_write_path(path: str) -> str:
    """写操作：解析为绝对路径，并限制在当前会话的 workspace 内，越界抛异常。"""
    root = _get_workspace_root()
    if os.path.isabs(path):
        full = os.path.normpath(path)
    else:
        full = os.path.normpath(os.path.join(root, path))

    root = os.path.normpath(root)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"路径越界，只能写入 workspace 内的文件: {path}")
    return full


def _check_ext(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXTS:
        raise ValueError(f"不支持的文件类型「{ext}」，只支持文本文件")


def _read_text(path: str) -> str:
    """读文本文件，兼容 UTF-8 / GBK"""
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法以文本方式读取该文件: {path}")


@tool(name="list_directory", description="列出指定目录下的子文件夹和文件。参数 path 是目录路径，可以是任意位置的绝对路径，也可以是相对 workspace 的路径；不传则列出 workspace 根目录")
def list_directory(path="."):
    full = _resolve_read_path(path)
    if not os.path.isdir(full):
        raise FileNotFoundError(f"目录不存在: {path}")
    items = []
    for name in sorted(os.listdir(full)):
        p = os.path.join(full, name)
        tag = "[目录]" if os.path.isdir(p) else "[文件]"
        items.append(f"{tag} {name}")
    return "\n".join(items) if items else f"空目录: {path}"


@tool(name="read_file", description="读取文本文件内容。参数 path 是文件路径，可以是任意位置的绝对路径（可读 workspace 之外的文件），也可以是相对 workspace 的路径")
def read_file(path):
    full = _resolve_read_path(path)
    _check_ext(full)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"文件不存在: {path}")
    content = _read_text(full)
    # if len(content) > MAX_READ_CHARS:
    #     content = content[:MAX_READ_CHARS] + f"\n\n...(文件过大已截断，原共 {len(content)} 字符)"
    return content


@tool(name="write_file", description="创建或覆盖文件。参数 path 是文件路径（只能写入 workspace 内，可用相对 workspace 的路径），content 是完整的文件内容")
def write_file(path, content):
    full = _resolve_write_path(path)
    _check_ext(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入: {path}（{len(content)} 字符）"


@tool(name="append_file", description="向文件末尾追加内容。参数 path 是文件路径（只能写入 workspace 内，可用相对 workspace 的路径），content 是追加的内容")
def append_file(path, content):
    full = _resolve_write_path(path)
    _check_ext(full)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    return f"已追加: {path}（{len(content)} 字符）"


@tool(name="replace_in_file", description="替换文件中的一段文字（只替换第一处）。参数 path 是文件路径（只能写入 workspace 内，可用相对 workspace 的路径），old_text 是要替换的原文，new_text 是替换后的新文")
def replace_in_file(path, old_text, new_text):
    full = _resolve_write_path(path)
    _check_ext(full)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"文件不存在: {path}")
    content = _read_text(full)
    if old_text not in content:
        raise ValueError(f"未找到要替换的内容: {old_text[:60]}")
    content = content.replace(old_text, new_text, 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已替换: {path}"
