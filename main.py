import json
import os
from openai import OpenAI
from Message import *
from Memory import *
from ChatModel import *

memory = Memory()
memory.loadMemory()
chatModel = ChatModel()
toolProxy = ToolProxy()
toolCalling = False

while True:

    if toolCalling == False:
        userMessageContent = input("你：")

        if userMessageContent == "q":
            break
        if userMessageContent == "c":
            memory.compact("part")

        userMessage = UserMessage(userMessageContent)
        memory.append(userMessage)

    completion = chatModel.chat(memory.messageList)
    print(completion)
    # print(memory.messageList)

    message = completion.choices[0].message
    if message.tool_calls :
        toolCalling = True
        aiMessage = AssistantMessage(message)
        memory.append(aiMessage)

        for tool_call in message.tool_calls:
            # funcres = func[tool_call.function.name](**(json.loads(tool_call.function.arguments)))
            funcres = toolProxy.invoke(tool_call.function.name,(json.loads(tool_call.function.arguments)))
            toolMessage = ToolMessage(funcres, tool_call.id)

            memory.append(toolMessage)

    else:
        toolCalling = False
        print("ai：" + message.content, end="\n")
        aiMessage = AssistantMessage(message)
        memory.append(aiMessage)


