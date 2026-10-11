# -*- coding: utf-8 -*-
"""会话标题自动生成器：第一次对话时，用模型把第一句话总结成不超过 5 个字的标题。"""
from dotenv import load_dotenv
load_dotenv()

_TITLE_PROMPT = (
    "把下面这句话概括成一个不超过 5 个字的简短标题。"
    "只输出标题本身，不要引号、不要标点、不要换行、不要任何解释：\n\n{message}"
)


async def generate_title(message: str) -> str:
    """用模型把一句话总结成不超过 5 个字的标题。"""
    from async_ChatModel import get_async_client
    from config import title_model

    model = title_model()
    client = get_async_client()
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": _TITLE_PROMPT.format(message=message)}],
        max_tokens=50,
        temperature=0.3,
        # 关掉思考模式：标题是简单任务，不需要 reasoning，避免 token 被思考占用导致 content 为空
        extra_body={"thinking": {"type": "disabled"}},
    )
    title = (resp.choices[0].message.content or "").strip()
    # 清理常见包裹符号
    title = title.strip("《》【】「」\"'“”‘’[]()（）<> ")
    # 去掉换行
    title = title.replace("\n", "").replace("\r", "")
    if len(title) > 5:
        title = title[:5]
    return title or "新会话"


async def generate_and_save_title(session_id: str, message: str) -> None:
    """后台生成标题并写回 meta.json。失败静默（不影响对话）。"""
    from session_store import session_store
    try:
        title = await generate_title(message)
        meta = session_store.load_meta(session_id) or {}
        meta["title"] = title
        session_store.save_meta(session_id, meta)
    except Exception:
        pass
