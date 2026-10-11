import os
from dotenv import load_dotenv
from openai import OpenAI, AsyncOpenAI
from Message import messageProcess
from tools.Toolregistry import *
from config import chat_model

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
    def __init__(self, funcs = func):
        self.func = func


    def invoke(self,toolName:str, params:dict):
        if toolName == "subAgent":
            self.subAgentExecutor(params)
        return self.func[toolName](**params)

    def subAgentExecutor(self,params: dict):

        self.func["subAgent"](**params)


class ChatModel:
    def __init__(self, model=None, max_completion_tokens=1024, temperature=1.0,
                 top_p=0.95, frequency_penalty=0, stop=None, presence_penalty=0, tools=tools):
        self.model = model or chat_model()
        self.max_completion_tokens = max_completion_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.tools = tools
        self.stop = stop

    def chat(self, message):
        completion = get_client().chat.completions.create(
            model=self.model,
            messages=messageProcess(message),
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
            messages=messageProcess(message),
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
            messages=messageProcess(message),
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


