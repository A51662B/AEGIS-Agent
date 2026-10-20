import datetime
import uuid
from pydantic import BaseModel


class Message(BaseModel):
    role: str
    type: str
    uuid: str
    timestamp: str
    message: dict
    rowmessage: dict | None


class BoundaryMessage(BaseModel):
    role: str
    type: str
    uuid: str
    timestamp: str
    logitParentUuid: str | None
    head: str | None
    tail: str | None


class PersistentMessage(BaseModel):
    message: Message | BoundaryMessage
    session: str | None
    parentUuid: str | None
    createTime: str | None
    msgType: str


def gettime():
    return str(datetime.datetime.now())


def getBoundaryMessage(head, tail):
    return BoundaryMessage(
        role="boundary",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        head=head,
        tail=tail,
        logitParentUuid=None
    )


def SummaryMessage(message: str):
    return Message(
        role="summary",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message={"role": "user", "content": message},
        rowmessage=None
    )


def UserMessage(message: str):
    return Message(
        role="user",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message={"role": "user", "content": message},
        rowmessage=None
    )

def AsyncMessage(message: str):
    return Message(
        role="async",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message={"role": "user", "content": message},
        rowmessage=None
    )


def InjectedMessage(message: str, kind: str = "rollback"):
    """系统注入的消息：给模型看是 user，但不是真实用户输入。

    与 AsyncMessage 同一个模式——两层 role：
      · role="injected"  ← 内部标记，前端(app.js 的 if/else 链)会自动跳过渲染，
                             trace 也据此不把它当作轮边界
      · message.role="user"  ← 发给 LLM 的仍是 user，语义不变

    kind: 注入原因，目前只有 rollback（中断回滚）。后续可扩展
          error / max_rounds，但先统一记在 role 里，不动 type 字段。
    """
    return Message(
        role="injected",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message={"role": "user", "content": message},
        rowmessage=None
    )


def AssistantMessage(message):
    if message.tool_calls:

        aiMessageContent = [tc.model_dump() for tc in message.tool_calls]
        # aiMessage = {"role": "assistant", "content": None, "tool_calls": aiMessageContent,"reasoning_content":message.reasoning_content}
        aiMessage = {"role": "assistant", "content": None, "tool_calls": aiMessageContent,"reasoning_content":getattr(message, "reasoning_content", None)}
    else:
        aiMessage = {"role": "assistant", "content": message.content,"reasoning_content":getattr(message, "reasoning_content", None)}
    return Message(
        role="assistant",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message=aiMessage,
        rowmessage=message.model_dump()
    )


def AssistantMessageFromParts(content: str, tool_calls=None, reasoning_content=None, rowmessage=None):
    """从流式累积的 content 与 tool_calls 列表构造 assistant 消息

    rowmessage: 流式响应拼出的完整原始记录（供审计），对齐 AssistantMessage 里 message.model_dump() 的语义。
    """
    aiMessage = {"role": "assistant", "content": content}
    if tool_calls:
        aiMessage["tool_calls"] = tool_calls
    if reasoning_content:
        aiMessage["reasoning_content"] = reasoning_content
    return Message(
        role="assistant",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message=aiMessage,
        rowmessage=rowmessage
    )


def SystemMessage(message: str):
    return Message(
        role="system",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message={"role": "system", "content": message},
        rowmessage=None
    )


def ToolMessage(toolcontent, tool_call_id):
    return Message(
        role="tool",
        type="normal",
        uuid=str(uuid.uuid4()),
        timestamp=gettime(),
        message={"role": "tool", "content": toolcontent, "tool_call_id": tool_call_id},
        rowmessage=None
    )


def wrapPersistentMessage(message: Message, session: str | None = None):
    return PersistentMessage(
        message=message,
        parentUuid=None,
        createTime=gettime(),
        msgType=message.role,
        session=session
    )


def messageProcess(messageList):
    res = []
    for message in messageList:
        res.append(message.message)
    return res
