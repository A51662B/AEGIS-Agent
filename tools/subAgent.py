import json
import uuid
from tools.Toolregistry import tool
from SubAgentMessageQueue import *

@tool(name = "subAgent", description = "这是一个异步的子agent，可以与主agent同步工作，你可以开启子agent来帮助你完成任务，你只需要向他提供字符串格式的系统提示词，说明清楚你的需求，他会帮你完成，"
                                       "一般在子任务比较复杂的时候可以使用，他会给你返回基于字符串格式的结果,不允许在子代理中开嵌套子代理，那样会使得程序崩溃")
async def subAgent(systemprompte:str):
    from async_ChatModel import ChatModel,func
    from Memory import Memory
    from Message import AssistantMessage
    from Message import ToolMessage
    from Message import SystemMessage
    from session_context import current_session
    from session_store import session_store

    parent_sid = current_session.get() or "default"
    subuuid = uuid.uuid4()
    memory_file = session_store.subagent_file(parent_sid, str(subuuid))

    memory = Memory(memory_file=memory_file, session_id=f"subagent_{subuuid}")
    chatModel = ChatModel()

    messageList = []
    systemMessage = SystemMessage(systemprompte)
    messageList.append(systemMessage)
    memory.messageList = messageList
    for _ in range(15):
        completion =await chatModel.chat_async(memory.messageList)

        message = completion.choices[0].message
        if message.tool_calls is not None :
            toolCalling = True
            aiMessage = AssistantMessage(message)
            memory.append(aiMessage)

            for tool_call in message.tool_calls:
                funcres = func[tool_call.function.name](**(json.loads(tool_call.function.arguments)))
                toolMessage = ToolMessage(funcres, tool_call.id)
                memory.append(toolMessage)

        else:
            aiMessage = AssistantMessage(message)
            memory.append(aiMessage)
            await q.put(message.content)
            return
            # return message.content




