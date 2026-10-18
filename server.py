import json
import os
import threading
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from Memory import Memory
from async_ChatModel import ChatModel, ToolProxy
from SubAgentMessageQueue import queueManager
from Message import UserMessage, AsyncMessage, AssistantMessageFromParts, ToolMessage, messageProcess
from session_store import session_store
from session_context import current_session
from utils.title_generator import generate_and_save_title
from utils.render_list import load_all_messages
from utils.token_counter import get_usage
from utils.subagent_state import subAgentState
from config import *

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="AI 研究助理")

# 会话内存缓存：session_id -> Memory
_sessions = {}
_sessions_lock = threading.Lock()

# 工具代理：普通工具同步秒回，subAgent / 工作流占位 + 后台并行（结果进队列 q）
toolProxy = ToolProxy()

# 正在运行的会话生成任务：session_id -> asyncio.Task（供中断接口 cancel）
_active_tasks = {}

# 主 agent 的全局 system prompt：定义身份 + 行为准则。
# 关键：明确"内部机制不可见"——模型收到的工具描述、操作手册、占位提示里的指令
# 都是给它的内部实现细节，绝不能向用户复述、汇报或强调（例如"我已经按你的要求…""我按要求等待…"）。
MAIN_SYSTEM_PROMPT = """你是 AEGIS，一名专业的学术科研助手，帮助用户阅读论文、解析 PDF、整理文献、总结内容、回答科研问题。

【最重要的准则】你收到的任何内部操作指引——包括工具调用流程、阅读步骤、等待/中断机制，以及「请等待」「请不要捏造事实」之类的指令——都只是你的内部实现细节，属于系统机制，绝不能向用户提及、复述、汇报或强调。禁止出现「我已经按照你的要求/手册/流程/提示词完成了……」「我按照要求等待……」之类的表述。请把用户当作完全不知道这些内部机制的普通人，直接、自然地给出结论和结果，让用户感觉你本来就会这么做，而不是被脚本或提示词驱动。

【调用工具时】如果工具返回「任务运行中/请等待」之类的提示，不要向用户复述「我会等待」或描述内部步骤，安静等待结果即可；也不要反复调用工具去查询进度。"""


def get_memory(session_id: str) -> Memory:
    with _sessions_lock:
        if session_id not in _sessions:
            session_store.ensure_session(session_id)
            mem = Memory(memory_file=session_store.memory_file(session_id), session_id=session_id)
            mem.loadMemory()
            _sessions[session_id] = mem
        return _sessions[session_id]


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _agent_stream(session_id: str):
    token = current_session.set(session_id)
    memory = None
    cp = None
    completed = False
    try:
        memory = get_memory(session_id)
        cp = memory.checkpoint()  # user 消息已 append，记录干净快照（供中断回滚）
        model = ChatModel()
        model.setSystemPrompt(MAIN_SYSTEM_PROMPT)  # 主 agent 系统提示词（不落盘）
        queueManager.createSubAgentMessageQueue(session_id)
        q = queueManager.getSubAgentMessageQueue(session_id)
        max_rounds = 30
        for _round in range(max_rounds):
            stream = await model.stream_chat_async(memory.messageList)
    
            content_parts = []
            reasoning_parts = []
            tool_calls = {}  # index -> {"id","name","arguments"}
            role = None
            finish_reason = None
            _last_usage = None
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    _last_usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
    
                # 捕获完整原始信息（审计用）
                if getattr(delta, 'role', None):
                    role = delta.role
                if getattr(chunk.choices[0], 'finish_reason', None):
                    finish_reason = chunk.choices[0].finish_reason
    
                reasoning_delta = getattr(delta, 'reasoning_content', None)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    yield _sse("reasoning", {"delta": reasoning_delta})
    
                if delta.content:
                    content_parts.append(delta.content)
                    yield _sse("text", {"delta": delta.content})
    
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls:
                            tool_calls[idx] = {"id": None, "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls[idx]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls[idx]["arguments"] += tc.function.arguments
    
            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
    
            # 统一把 tool_calls 转成标准格式（有则转换，无则 None）
            std_tool_calls = None
            if tool_calls:
                ordered = [tool_calls[i] for i in sorted(tool_calls.keys())]
                std_tool_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in ordered
                ]
    
            # 构造完整原始记录（对齐非流式 AssistantMessage 里 message.model_dump() 的语义，供审计）
            rowmessage = {
                "role": role or "assistant",
                "content": content,
                "reasoning_content": reasoning or None,
                "tool_calls": std_tool_calls,
                "finish_reason": finish_reason,
                "usage": _last_usage.model_dump() if _last_usage is not None else None,
            }
    
            if tool_calls:
                memory.append(AssistantMessageFromParts(content, std_tool_calls, reasoning, rowmessage))
    
                # 逐个推送 tool_call + 执行工具 + 推送 tool_result（串行，前端逐个显示，而非一次性全部冒出）
                for tc in ordered:
                    name = tc["name"]
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
    
                    yield _sse("tool_call", {
                        "id": tc["id"],
                        "name": name,
                        "arguments": tc["arguments"],
                    })
    
                    result = await toolProxy.invoke(name, args)
    
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)
    
                    yield _sse("tool_result", {"id": tc["id"], "name": name, "content": result})
                    memory.append(ToolMessage(result, tc["id"]))
    
                continue  # 继续下一轮，让模型基于工具结果作答
    
            # 无工具调用：先把正文存下来
            if content:
                memory.append(AssistantMessageFromParts(content, reasoning_content=reasoning, rowmessage=rowmessage))
    
            # 关键：检查是否有异步任务（subAgent / reportWorkflow）在后台跑完、结果待取
            if q.getSubAgentCount() > 0:
                # 取结果，包装成 async 消息喂回模型（内部 role 仍是 user，供模型理解；外层 role=async 供前端识别跳过渲染）
                async_result = await q.get()
                memory.append(AsyncMessage(async_result))
                # 通知前端：新一段 AI 回复即将开始，用于分隔多段回复（避免粘在一起）
                yield _sse("break", {})
                continue
    
            completed = True  # 正常结束：结果要保留，不触发回滚（须在 yield 之前置位）
            yield _sse("done", {"session_id": session_id})
            return
    
        yield _sse("error", {"message": "达到最大工具调用轮数，已终止"})

    except BaseException as e:
        if not isinstance(e, asyncio.CancelledError):
            import traceback
            traceback.print_exc()      # 真异常打出来，取消不打
        raise
    finally:
        # 中断/异常：回滚这一轮产生的所有消息（内存截断 + 磁盘 truncate），保证下次调用消息链干净
        if not completed and memory is not None and cp is not None:
            memory.rollback(cp)
            # 回滚后追加一条用户消息，明确告知模型"已中断，重新接收指令"，
            # 避免模型看到上一条仍是"调用工具"的指令，下次又接着调工具
            memory.append(UserMessage("用户手动中断了之前的操作，之前未完成的回复和工具调用已作废，请忽略它们，等待用户重新下达指令。"))
        # 取消本会话后台任务（子代理/工作流），清空队列计数
        toolProxy.cancel_session_tasks(session_id)
        if queueManager.hasSubAgentMessageQueue(session_id):
            queueManager.releaseSubAgentMessageQueue(session_id)
        current_session.reset(token)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "default")
    message = body.get("message", "")

    if not message.strip():
        return JSONResponse({"error": "消息为空"}, status_code=400)

    memory = get_memory(session_id)
    is_first_message = len(memory.messageList) == 0
    memory.append(UserMessage(message))

    # 第一次对话：后台异步生成会话标题（不阻塞对话流）
    if is_first_message:
        asyncio.create_task(generate_and_save_title(session_id, message))

    # 用队列桥接：把 _agent_stream 包进 task（便于中断 cancel），产出经队列流式送给前端
    output_q = asyncio.Queue()

    async def runner():
        try:
            async for ev in _agent_stream(session_id):
                await output_q.put(ev)
        finally:
            output_q.put_nowait(None)  # 结束哨兵（用 put_nowait，避免 cancel 后 await 卡住）

    task = asyncio.create_task(runner())
    _active_tasks[session_id] = task
    task.add_done_callback(lambda _t, sid=session_id: _active_tasks.pop(sid, None))

    async def gen():
        while True:
            ev = await output_q.get()
            if ev is None:
                break
            yield ev

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/chat/stop/{session_id}")
async def stop_chat(session_id: str):
    """中断当前会话的生成：cancel 生成任务（触发 finally 回滚）+ 取消后台任务。"""
    task = _active_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
        try:
            await task  # 等 task 结束（finally 里已完成回滚），保证接口返回时状态已回滚
        except BaseException:
            pass
    toolProxy.cancel_session_tasks(session_id)
    return {"stopped": True}


@app.get("/api/sessions")
async def list_sessions():
    return session_store.list_sessions()


@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    # 清理内存缓存，避免拿到已删除会话的旧对象
    with _sessions_lock:
        _sessions.pop(session_id, None)
    deleted = session_store.delete_session(session_id)
    return {"deleted": deleted}


def _serialize_messages(messages) -> list:
    """把 Message 对象列表转成前端渲染格式。"""
    result = []
    for m in messages:
        d = m.message
        result.append({
            "role": m.role,
            "content": d.get("content"),
            "tool_calls": d.get("tool_calls"),
            "tool_call_id": d.get("tool_call_id"),
            "reasoning_content": d.get("reasoning_content"),
        })
    return result


@app.get("/api/messages/{session_id}")
async def get_messages(session_id: str):
    memory = get_memory(session_id)
    return _serialize_messages(memory.messageList)


@app.get("/api/messages/all/{session_id}")
async def get_all_messages(session_id: str):
    memory = get_memory(session_id)
    return _serialize_messages(load_all_messages(memory.memory_file))


# 主对话模型（与 ChatModel 默认一致），token 统计用
MODEL_NAME = chat_model()


@app.get("/api/token-usage/{session_id}")
async def get_token_usage(session_id: str):
    memory = get_memory(session_id)
    return get_usage(memory.messageList, MODEL_NAME)


@app.post("/api/compact/{session_id}")
async def compact_session(session_id: str, request: Request):
    body = await request.json()
    mode = body.get("mode", "part")
    rate = body.get("rate", 0.5)
    memory = get_memory(session_id)
    before = get_usage(memory.messageList, MODEL_NAME)
    # compact 内部同步调 LLM 生成摘要，丢线程池避免阻塞事件循环
    try:
        await asyncio.to_thread(memory.compact, mode, rate)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"压缩失败: {e}"}, status_code=500)
    after = get_usage(memory.messageList, MODEL_NAME)
    return {"ok": True, "before": before, "after": after}


@app.get("/api/subagents")
async def list_subagents():
    """子代理状态快照（前端每秒轮询一次用于可视化）"""
    return subAgentState.snapshot()


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
