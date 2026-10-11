import os

_DEFAULT_CHAT_MODEL = "deepseek-flash"


def chat_model() -> str:
    """主对话模型：读 CHAT_MODEL，缺省 deepseek-flash。"""
    return os.getenv("CHAT_MODEL") or _DEFAULT_CHAT_MODEL


def report_model() -> str:
    """报告工作流模型：REPORT_MODEL 优先，否则回落到主对话模型。"""
    return os.getenv("REPORT_MODEL") or chat_model()


def title_model() -> str:
    """会话标题模型：TITLE_MODEL 优先，否则回落到主对话模型。"""
    return os.getenv("TITLE_MODEL") or chat_model()
