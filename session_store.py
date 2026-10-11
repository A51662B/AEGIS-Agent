import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_ROOT = os.path.join(BASE_DIR, "memory")


class SessionStore:
    """会话存储管理器：负责会话目录结构的创建、枚举、元数据读写和路径解析。

    目录结构：
        memory/
        └── <session_id>/
            ├── meta.json        # 元数据（创建时间、模型等）
            ├── memory.jsonl     # 主记忆
            ├── subagents/       # 子代理记忆（每个 uuid 一个文件）
            │   └── <uuid>.jsonl
            └── workspace/       # 会话专属工作区
    """

    def __init__(self, root: str = MEMORY_ROOT):
        self.root = root

    def session_dir(self, session_id: str) -> str:
        return os.path.join(self.root, session_id)

    def memory_file(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), "memory.jsonl")

    def subagents_dir(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), "subagents")

    def subagent_file(self, session_id: str, subagent_uuid: str) -> str:
        return os.path.join(self.subagents_dir(session_id), f"{subagent_uuid}.jsonl")

    def workspace(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), "workspace")

    def meta_file(self, session_id: str) -> str:
        return os.path.join(self.session_dir(session_id), "meta.json")

    def ensure_session(self, session_id: str, model: str | None = None) -> str:
        """幂等：确保会话目录结构存在，不存在则创建。返回会话目录路径。"""
        d = self.session_dir(session_id)
        if os.path.isdir(d):
            return d
        os.makedirs(self.subagents_dir(session_id), exist_ok=True)
        os.makedirs(self.workspace(session_id), exist_ok=True)
        self.save_meta(session_id, {
            "session_id": session_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
        })
        return d

    def load_meta(self, session_id: str) -> dict | None:
        fp = self.meta_file(session_id)
        if not os.path.exists(fp):
            return None
        with open(fp, encoding="utf-8") as f:
            return json.load(f)

    def save_meta(self, session_id: str, meta: dict) -> None:
        os.makedirs(self.session_dir(session_id), exist_ok=True)
        with open(self.meta_file(session_id), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def list_sessions(self) -> list[dict]:
        """枚举所有会话（读 meta.json 拿元数据），按最后修改时间倒序。"""
        sessions = []
        if not os.path.isdir(self.root):
            return sessions
        for name in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, name)
            if not os.path.isdir(d):
                continue
            meta = self.load_meta(name) or {}
            sessions.append({
                "session_id": name,
                "created_at": meta.get("created_at"),
                "model": meta.get("model"),
                "title": meta.get("title"),
                "mtime": os.path.getmtime(d),
            })
        sessions.sort(key=lambda s: s["mtime"], reverse=True)
        return sessions

    def delete_session(self, session_id: str) -> bool:
        """删除整个会话文件夹（含主记忆、子代理、workspace、元数据）。返回是否删除成功。"""
        import shutil
        d = self.session_dir(session_id)
        if os.path.isdir(d):
            shutil.rmtree(d)
            return True
        return False


# 模块级单例，供 server / file_tools / subAgent 共用
session_store = SessionStore()
