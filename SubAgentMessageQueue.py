from queue import Queue
import asyncio

class SubAgentMessageQueue:
    def __init__(self, num = 20):
        
        self.q = asyncio.Queue(maxsize=num)
        self.curSubAgentCount = 0

    async def put(self, message):
        await self.q.put(message)

    async def get(self):
        message = await self.q.get()
        self.curSubAgentCount -= 1
        return message

    def empty(self):
        return self.q.empty()

    def qsize(self):
        return self.q.qsize()

    def SubAgentCountPlus(self):
        self.curSubAgentCount += 1

    def getSubAgentCount(self):
        return self.curSubAgentCount

    def clear(self):
        """清空队列并清零计数（中断时用）。"""
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.curSubAgentCount = 0

class SubAgentMessageQueueManager:
    # 内部持有多个 SubAgentMessageQueue 实例
    def __init__(self):
        self.queues: dict[str, SubAgentMessageQueue] = {}

    def createSubAgentMessageQueue(self, sessionId:str):
        if sessionId in self.queues:
            raise RuntimeError(f"sessionId {sessionId} 的消息队列已经存在，禁止重复创建")
        self.queues[sessionId] = SubAgentMessageQueue()

    def getSubAgentMessageQueue(self, sessionId:str):
        if sessionId not in self.queues:
            raise KeyError(f"sessionId {sessionId} 消息队列不存在，请先调用 createSubAgentMessageQueue")
        return self.queues[sessionId]
            

    def releaseSubAgentMessageQueue(self, sessionId:str):
        if sessionId not in self.queues:
            raise KeyError(f"release失败:sessionId {sessionId} 队列不存在")
        queue = self.queues.pop(sessionId)
        queue.clear()

    def hasSubAgentMessageQueue(self, sessionId:str) -> bool:
        return sessionId in self.queues
    

    


queueManager = SubAgentMessageQueueManager()
