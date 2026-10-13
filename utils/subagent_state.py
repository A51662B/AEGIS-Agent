import time


class SubAgentState:
    def __init__(self):
        self._states = {}

    def start(self, sid:str, max_rounds, label, round=0,kind="subAgent"):
        self._states[sid] = {
            "id": sid, "kind": kind, "label": label,  # kind 给工作流预留
            "status": "running",  # running/done/failed/cancelled
            "round": round, "max_rounds": max_rounds,
            "last_tool": None, "last_text": "",
            "started_at": time.time(), "updated_at": time.time(),
        }

    def update(self,sid ,round=None, last_tool=None, last_text=None, finish=None):
        if sid not in self._states:
            return
        if round is not None:
            self._states[sid]["round"] = round
        if last_tool is not None:
            self._states[sid]["last_tool"] = last_tool
        if last_text is not None:
            self._states[sid]["last_text"] = last_text
        if finish is not None:
            self._states[sid]["status"] = finish

    def snapshot(self) -> list:
        """给前端读的全量快照（按启动时间排序）"""
        return sorted(self._states.values(), key=lambda s: s["started_at"])


subAgentState = SubAgentState()