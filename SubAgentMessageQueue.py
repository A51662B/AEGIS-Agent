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



q = SubAgentMessageQueue()
