import asyncio
import json
import os
from openai import OpenAI
from Message import *
from Memory import *
from async_ChatModel import *
from SubAgentMessageQueue import *
from session_context import current_session
memory = Memory()
memory.loadMemory()
chatModel = ChatModel()
toolProxy = ToolProxy()
toolCalling = False
CLI_SESSION_ID = "default"

async def run():
    toolCalling = False
    # CLI 只有一个隐式会话：必须设置 current_session 并建好队列，
    # 否则 ToolProxy.invoke / subAgent 内部取队列时会 KeyError。
    current_session.set(CLI_SESSION_ID)
    queueManager.createSubAgentMessageQueue(CLI_SESSION_ID)
    q = queueManager.getSubAgentMessageQueue(CLI_SESSION_ID)
    while True:
        if toolCalling == False and q.getSubAgentCount() == 0:
            userMessageContent = input("你：")

            if userMessageContent == "q":
                break
            if userMessageContent == "c":
                memory.compact("part")
            userMessage = UserMessage(userMessageContent)
            memory.append(userMessage)

        elif toolCalling == False and q.getSubAgentCount() > 0:
            userMessage = AsyncMessage(await q.get())
            memory.append(userMessage)

        completion =await chatModel.chat_async(memory.messageList)
        print(completion)
        # print(memory.messageList)
        message = completion.choices[0].message
        if message.tool_calls :
            toolCalling = True
            aiMessage = AssistantMessage(message)
            memory.append(aiMessage)
            for tool_call in message.tool_calls:
                # funcres = func[tool_call.function.name](**(json.loads(tool_call.function.arguments)))
                funcres = await toolProxy.invoke(tool_call.function.name,(json.loads(tool_call.function.arguments)))
                toolMessage = ToolMessage(funcres, tool_call.id)
                memory.append(toolMessage)

        else:
            toolCalling = False
            print("ai：" + message.content, end="\n")
            aiMessage = AssistantMessage(message)
            memory.append(aiMessage)
    queueManager.releaseSubAgentMessageQueue(CLI_SESSION_ID)


if __name__ == "__main__":
    asyncio.run(run())
