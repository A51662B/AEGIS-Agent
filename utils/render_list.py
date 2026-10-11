# -*- coding: utf-8 -*-
"""前端消息渲染列表：独立于 Memory，加载记忆文件中的所有历史消息供前端展示。

与 Memory.loadMemory（喂给模型的上下文）的分工：
- loadMemory 沿 parentUuid 链回溯，遇到 boundary 就停止，丢弃被压缩的消息（= 有效上下文）
- 本模块加载所有历史消息（含被压缩的），跳过 boundary/summary 标记节点本身（= 完整展示）

为什么按文件顺序而不是沿链回溯：
记忆文件是 append-only 的（每次 append 都 flush 追加），文件顺序 == 时间顺序 == 链顺序。
按文件顺序过滤 boundary/summary 标记节点，等价于"沿链遍历所有消息、跳过标记节点"，
但更简单、更健壮（能正确处理 full 压缩时 boundary 断链的情况）。
"""
import os

from Message import PersistentMessage


def load_all_messages(memory_file: str) -> list:
    """读记忆文件，按时间顺序返回所有真实消息（跳过 boundary/summary 标记节点）。"""
    if not os.path.exists(memory_file):
        return []

    messages = []
    with open(memory_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line == '':
                continue
            pm = PersistentMessage.model_validate_json(line)
            # 跳过压缩标记节点（boundary 是存储标记，summary 是摘要，都不是真实对话消息）
            if pm.message.role not in ('boundary', 'summary'):
                messages.append(pm.message)
    return messages
