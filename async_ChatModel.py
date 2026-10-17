import asyncio
import json
import os
from dotenv import load_dotenv
from openai import OpenAI, AsyncOpenAI
from Message import messageProcess
from tools import subAgent
from tools.Toolregistry import *
from SubAgentMessageQueue import *
from session_context import current_session
from utils.subagent_state import subAgentState
from config import *
load_dotenv()

_client = None
_async_client = None

def get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
    return _client


def get_async_client():
    global _async_client
    if _async_client is None:
        _async_client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
    return _async_client

tools = toolregistry.getToolDescription()
func = toolregistry.getToolMap()

class ToolProxy:
    # 同步工作流工具 -> 前端占位提示（后台 to_thread 执行，结果进队列 q）
    WORKFLOW_TOOLS = {
        "reportWorkflow": "论文报告编写工作流正在运行",
        "pdfToMarkdown": "PDF 转 Markdown 工作流正在运行",
    }

    def __init__(self, funcs = func):
        self.func = funcs
        self._tasks = set()

    def _harvest_terminal_state(self, t:asyncio.Task):
        sid = getattr(t, "_subagent_id", None)
        if t.cancelled():
            subAgentState.update(sid,finish="cancelled")
            return
        elif t.exception() is not None:
            subAgentState.update(sid,finish="failed")
        else:
            subAgentState.update(sid,finish="done")

    def _spawn(self, coro):
        """创建后台 task 并标记所属会话，完成后自动从 _tasks 清理（防内存泄漏）。"""
        task = asyncio.create_task(coro)
        task._session_id = current_session.get()
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._harvest_terminal_state)
        return task

    def cancel_session_tasks(self, session_id):
        """取消指定会话的所有后台任务（子代理/工作流），并清空队列计数。"""
        for task in list(self._tasks):
            if getattr(task, "_session_id", None) == session_id and not task.done():
                task.cancel()
        if queueManager.hasSubAgentMessageQueue(session_id):
            q = queueManager.getSubAgentMessageQueue(session_id)
            q.clear()

    async def invoke(self, toolName: str, params: dict):
        q = queueManager.getSubAgentMessageQueue(current_session.get())
        # 未知工具：模型幻觉调用了不存在的工具
        if toolName not in self.func:
            return f"未知工具: {toolName}"

        # 子代理：异步执行，立即返回占位提示
        if toolName == "subAgent":
            try:
                q.SubAgentCountPlus()
                self._spawn(self.subAgentExecutor(params))
                return "（子代理任务已在后台启动，结果稍后自动返回，无需额外操作）"
            except Exception as e:
                return f"子代理启动失败: {e}"

        if toolName in self.WORKFLOW_TOOLS:
            try:
                q.SubAgentCountPlus()
                self._spawn(self.workflowExecutor(toolName, params))
                return f"（{self.WORKFLOW_TOOLS[toolName]}，结果稍后自动返回，无需额外操作）"
            except Exception as e:
                return f"工作流启动失败: {e}"

        if toolName == "run_bash":
            try:
                result = await self.func[toolName](**params)
                return result
            except Exception as e :
                return f"工具执行出错: {e}"
        # 普通工具：同步执行，捕获所有异常
        try:
            result = self.func[toolName](**params)
        except TypeError as e:
            return f"工具参数错误: {e}"
        except Exception as e:
            return f"工具执行出错: {e}"

        # 结果统一转字符串
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return result

    async def subAgentExecutor(self, params: dict):
        q = queueManager.getSubAgentMessageQueue(current_session.get())
        # 子代理在后台 task 里跑，异常不会传到 invoke，必须在这里捕获
        try:
            await self.func["subAgent"](**params)
            # 正常：subAgent 内部已经 q.put 了结果
        except Exception as e:
            # 关键：异常也必须 q.put 回队列，否则主循环 await q.get() 会永远卡住
            await q.put(f"子代理执行出错: {e}")
            raise

    async def workflowExecutor(self, toolName: str, params: dict):
        q = queueManager.getSubAgentMessageQueue(current_session.get())
        # 同步工作流（reportWorkflow / pdfToMarkdown 等）在后台 task 里用 to_thread 跑，
        # 异常不会传到 invoke，必须在这里捕获
        try:
            result = await asyncio.to_thread(self.func[toolName], **params)
            await q.put(result)
        except Exception as e:
            # 关键：异常也必须 q.put 回队列，否则主循环 await q.get() 会永远卡住
            await q.put(f"工作流执行出错: {e}")



class ChatModel:
    def __init__(self, model=chat_model(), max_completion_tokens=1024, temperature=1.0,
                 top_p=0.95, frequency_penalty=0, stop=None, presence_penalty=0, tools=tools):
        self.model = model
        self.max_completion_tokens = max_completion_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.tools = tools
        self.stop = stop
        self._system_prompts = []  # 系统提示词列表（可能多处追加，真正请求时拼成一个字符串注入）

    def setSystemPrompt(self, prompt: str | list | None):
        """设置（覆盖）系统提示词。传字符串或字符串列表；传 None 清空。

        会在每次请求时拼成一个字符串、注入到消息列表最前面（不落盘、不改 memory）。
        """
        if prompt is None:
            self._system_prompts = []
        elif isinstance(prompt, str):
            self._system_prompts = [prompt]
        else:
            self._system_prompts = list(prompt)

    def addSystemPrompt(self, prompt: str):
        """追加一条系统提示词（不清空已有）。方便多处各自加提示词后统一注入。"""
        if prompt:
            self._system_prompts.append(prompt)

    def _with_system(self, message):
        """把 messageProcess 结果包一层：若设置了系统提示词（可能多条），拼成一个字符串插到最前面。"""
        processed = messageProcess(message)
        if self._system_prompts:
            full = "\n\n".join(self._system_prompts)
            return [{"role": "system", "content": full}] + processed
        return processed

    def chat(self, message):
        completion = get_client().chat.completions.create(
            model=self.model,
            messages=self._with_system(message),
            # messages=message,
            max_completion_tokens=self.max_completion_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            stream=False,
            stop=self.stop,
            frequency_penalty=self.frequency_penalty,
            presence_penalty=self.presence_penalty,
            tools=self.tools,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        return completion

    async def chat_async(self, message):
        completion = await get_async_client().chat.completions.create(
            model=self.model,
            messages=self._with_system(message),
            # messages=message,
            max_completion_tokens=self.max_completion_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            stream=False,
            stop=self.stop,
            frequency_penalty=self.frequency_penalty,
            presence_penalty=self.presence_penalty,
            tools=self.tools,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        return completion

    def stream_chat(self, message):
        stream = get_client().chat.completions.create(
            model=self.model,
            messages=self._with_system(message),
            max_completion_tokens=self.max_completion_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            stream=True,
            stop=self.stop,
            frequency_penalty=self.frequency_penalty,
            presence_penalty=self.presence_penalty,
            tools=self.tools,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        return stream

    async def stream_chat_async(self, message):
        """异步流式：不阻塞事件循环，保证 SSE 逐 chunk 实时推送"""
        stream = await get_async_client().chat.completions.create(
            model=self.model,
            messages=self._with_system(message),
            max_completion_tokens=self.max_completion_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            stream=True,
            stop=self.stop,
            frequency_penalty=self.frequency_penalty,
            presence_penalty=self.presence_penalty,
            tools=self.tools,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        return stream


