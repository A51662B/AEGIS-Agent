import asyncio
import inspect
import json
import uuid

from tools.Toolregistry import tool
from SubAgentMessageQueue import *
from utils.subagent_state import subAgentState


class SubAgentToolProxy:
    """子代理专用的工具调度器。

    与主代理 ToolProxy 的区别：
      - 不 spawn 后台 task、不碰会话队列（子代理没有消费者，拿了结果也没人等）
      - 工作流类长耗时工具丢到 to_thread 里跑完再取结果，不阻塞事件循环
      - 任何异常都转成字符串返回给模型，避免一次工具失败杀死整个子代理
      - 禁止再开子代理（会递归）
    """

    def __init__(self, funcs: dict, workflow_tools=frozenset()):
        self.func = funcs
        self.workflow_tools = set(workflow_tools)

    async def invoke(self, toolName: str, params) -> str:
        if toolName not in self.func:
            return f"未知工具: {toolName}"

        # ★ 必须在 coroutine 判断之前：subAgent 本身是 async def，
        #   一旦走到 await f(...) 就会真的递归开一个子代理。
        if toolName == "subAgent":
            return "子代理中不允许再开启子代理，请直接完成当前任务，或把任务拆得更小。"

        if not isinstance(params, dict):
            return "工具参数必须是 JSON 对象（键值对）"

        f = self.func[toolName]
        try:
            if inspect.iscoroutinefunction(f):
                result = await f(**params)
            elif toolName in self.workflow_tools:
                result = await asyncio.to_thread(f, **params)
            else:
                result = f(**params)
        except TypeError as e:
            return f"工具参数错误: {e}"
        except Exception as e:
            # 注意：asyncio.CancelledError 是 BaseException 的子类，不会被这里吞掉
            return f"工具执行出错: {e}"

        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return result


@tool(name = "subAgent", description = "这是一个异步的子agent，可以与主agent同步工作，你可以开启子agent来帮助你完成任务，你只需要向他提供字符串格式的系统提示词，说明清楚你的需求，他会帮你完成，"
                                       "一般在子任务比较复杂的时候可以使用，他会给你返回基于字符串格式的结果,不允许在子代理中开嵌套子代理，那样会使得程序崩溃")
async def subAgent(systemprompte:str):
    from async_ChatModel import ChatModel, func, ToolProxy
    from Memory import Memory
    from Message import AssistantMessage
    from Message import ToolMessage
    from Message import SystemMessage
    from session_context import current_session
    from session_store import session_store

    MAX_ROUNDS = 15
    parent_sid = current_session.get() or "default"
    
    try:
        q = queueManager.getSubAgentMessageQueue(parent_sid)
    except KeyError as e:
        raise RuntimeError(f"获取父会话 {parent_sid} 的子Agent消息队列失败，队列不存在") from e
    
    subuuid = uuid.uuid4()
    memory_file = session_store.subagent_file(parent_sid, str(subuuid))
    sid = f"{parent_sid}:{subuuid}"
    asyncio.current_task()._subagent_id = sid
    memory = Memory(memory_file=memory_file, session_id=f"subagent_{subuuid}")
    chatModel = ChatModel()
    toolProxy = SubAgentToolProxy(func, ToolProxy.WORKFLOW_TOOLS)
    subAgentState.start(sid, MAX_ROUNDS, systemprompte[:80])
    messageList = []
    systemMessage = SystemMessage(systemprompte)
    messageList.append(systemMessage)
    memory.messageList = messageList
    for i in range(MAX_ROUNDS):
        subAgentState.update(sid, round=i + 1)
        completion =await chatModel.chat_async(memory.messageList)

        message = completion.choices[0].message
        if message.tool_calls is not None :
            aiMessage = AssistantMessage(message)
            memory.append(aiMessage)

            for tool_call in message.tool_calls:
                name = tool_call.function.name
                subAgentState.update(sid, last_tool=name)
                try:
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                except Exception as e:
                    funcres = f"工具参数解析失败（arguments 不是合法 JSON）: {e}"
                else:
                    try:
                        funcres = await toolProxy.invoke(name, args)
                    except Exception as e:
                        funcres = f"工具调用异常: {e}"
                memory.append(ToolMessage(funcres, tool_call.id))

        else:
            aiMessage = AssistantMessage(message)
            memory.append(aiMessage)
            subAgentState.update(sid, last_text=(message.content or "")[:80])
            await q.put(message.content)
            return
    raise RuntimeError(
        f"子代理在 {MAX_ROUNDS} 轮内未能给出结论（任务可能过于复杂）可以把它拆成更具体的子任务再试。")

