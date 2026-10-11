import os
from Message import *
from ChatModel import ChatModel

chatModel = ChatModel()


class Memory:
    def __init__(self, memory_file: str | None = None, session_id: str = "default", memory_dir: str | None = None):
        self.session_id = session_id
        if memory_file is None:
            if memory_dir is None:
                memory_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
            memory_file = os.path.join(memory_dir, f"{session_id}.jsonl")
        self.memory_file = memory_file
        self.memory_dir = os.path.dirname(memory_file)
        self.messageList = []
        self._currentParentUuid = None
        self._buffer = []

    def append(self, message: Message):
        self.messageList.append(message)
        PM = wrapPersistentMessage(message, session=self.session_id)
        PM.parentUuid = self._currentParentUuid
        self._currentParentUuid = message.uuid
        self._buffer.append(PM)
        self.flush()

    def flush(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.memory_file, 'a', encoding="UTF-8") as f:
            f.write(self._buffer[-1].model_dump_json() + '\n')
        self._buffer.clear()

    def loadMemory(self):
        if not os.path.exists(self.memory_file):
            return

        psMessageDict = {}
        with open(self.memory_file, 'r', encoding="UTF-8") as f:
            for line in f:
                line = line.strip()
                if line == '':
                    continue
                psmessage = PersistentMessage.model_validate_json(line)
                self._currentParentUuid = psmessage.message.uuid
                psMessageDict[psmessage.message.uuid] = psmessage

        if len(psMessageDict) == 0:
            return

        tempmessagelist = []
        summarymessage = None
        curid = self._currentParentUuid

        while curid is not None and curid in psMessageDict:
            pm = psMessageDict[curid]
            if pm.message.role == 'boundary':
                if pm.message.head is None:
                    break
                else:
                    head = pm.message.head
                    tail = pm.message.tail
                    curid = tail
                    while curid is not None and curid in psMessageDict \
                            and curid != psMessageDict[head].parentUuid:
                        tempmessagelist.append(psMessageDict[curid].message)
                        curid = psMessageDict[curid].parentUuid

                    if curid == psMessageDict[head].parentUuid:
                        break

            elif pm.message.role == 'summary':
                summarymessage = pm.message
                curid = pm.parentUuid
            else:
                tempmessagelist.append(pm.message)
                curid = pm.parentUuid

        if summarymessage is not None:
            tempmessagelist.append(summarymessage)

        self.messageList = tempmessagelist
        self.messageList.reverse()

    def checkpoint(self) -> dict:
        """记录当前状态快照，供中断时回滚。返回 {len, parent_uuid, file_size}。"""
        file_size = 0
        if os.path.exists(self.memory_file):
            file_size = os.path.getsize(self.memory_file)
        return {
            "len": len(self.messageList),
            "parent_uuid": self._currentParentUuid,
            "file_size": file_size,
        }

    def rollback(self, cp: dict):
        """回滚到 checkpoint 快照：截断内存列表、恢复链尾指针、截断磁盘文件。幂等。"""
        del self.messageList[cp["len"]:]
        self._currentParentUuid = cp["parent_uuid"]
        self._buffer.clear()
        if os.path.exists(self.memory_file):
            with open(self.memory_file, "r+b") as f:
                f.truncate(cp["file_size"])

    def _compact_input(self, messages, max_chars=2000):
        """构造压缩摘要输入：截断超长 content，避免 read_file 全文等超大消息撑爆 LLM 输入导致压缩失败。"""
        out = []
        for m in messages:
            d = m.message
            content = d.get("content")
            if isinstance(content, str) and len(content) > max_chars:
                nd = dict(d)
                nd["content"] = content[:max_chars] + f"\n...(原文 {len(content)} 字符，压缩时已截断)"
                m = Message(role=m.role, type=m.type, uuid=m.uuid, timestamp=m.timestamp, message=nd, rowmessage=m.rowmessage)
            out.append(m)
        return out

    def compact(self, mode: str, rate: float = 0.5):
        if mode == "full":
            boundarymessage = getBoundaryMessage(head=None, tail=None)
            boundarymessage.logitParentUuid = self._currentParentUuid
            self._currentParentUuid = boundarymessage.uuid
            boundarymessage = wrapPersistentMessage(boundarymessage, session=self.session_id)
            boundarymessage.parentUuid = None
            self.messageList.insert(0, SystemMessage("请你把以下消息做个总结，输出格式为'历史消息总结为:'"))
            summarymessage = chatModel.chat(self._compact_input(self.messageList)).choices[0].message
            summarymessage = SummaryMessage(summarymessage.content)

            self.messageList.clear()
            self.messageList.append(summarymessage)
            summarymessage = wrapPersistentMessage(summarymessage, session=self.session_id)
            summarymessage.parentUuid = self._currentParentUuid
            self._currentParentUuid = summarymessage.message.uuid

            self._buffer.append(boundarymessage)
            self.flush()
            self._buffer.append(summarymessage)
            self.flush()

        else:
            compacthead = 0
            compacttail = int(len(self.messageList) * rate)
            while 1:
                if compacttail < len(self.messageList) and self.messageList[compacttail].role != 'user':
                    compacttail += 1
                else:
                    if compacttail == len(self.messageList):
                        self.compact("full")
                        return
                    else:
                        break

            compactList = self.messageList[compacthead:compacttail]

            head = self.messageList[compacttail].uuid
            tail = self.messageList[-1].uuid
            boundarymessage = getBoundaryMessage(head=head, tail=tail)
            boundarymessage.logitParentUuid = self._currentParentUuid
            self._currentParentUuid = boundarymessage.uuid
            boundarymessage = wrapPersistentMessage(boundarymessage, session=self.session_id)
            boundarymessage.parentUuid = None

            compactList.insert(0, SystemMessage("请你把以下消息做个总结，输出格式为'历史消息总结为:'"))
            summarymessage = chatModel.chat(self._compact_input(compactList)).choices[0].message
            summarymessage = SummaryMessage(summarymessage.content)

            del self.messageList[compacthead:compacttail]
            self.messageList.insert(0, summarymessage)

            summarymessage = wrapPersistentMessage(summarymessage, session=self.session_id)
            summarymessage.parentUuid = self._currentParentUuid
            self._currentParentUuid = summarymessage.message.uuid

            self._buffer.append(boundarymessage)
            self.flush()
            self._buffer.append(summarymessage)
            self.flush()
        return


if __name__ == '__main__':
    memory = Memory()
    memory.loadMemory()
    print(len(memory.messageList))
    print(memory.messageList)
    print(messageProcess(memory.messageList))
