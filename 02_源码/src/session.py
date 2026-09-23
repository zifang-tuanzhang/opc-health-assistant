"""会话管理（session）：多轮上下文 + 会话隔离。

来自编排层「能力层」的 session 工具：模型在控制循环里读写会话上下文，
不同会话之间严格隔离（评审核查点：会话间不串数据）。

Step 2（接入层骨架）阶段：先实现内存版（进程内字典），满足「会话隔离 / 多轮承接 /
可重置」三项基本要求，网关限流与持久化后续接入。持久化（进阶3）将在此之上加一层
可选后端（文件/Redis），接口不变。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("opc.session")


@dataclass
class Turn:
    """单轮对话记录（模型视角下的上下文单元）。"""

    role: str  # "user" | "assistant"
    content: str
    ts: float = field(default_factory=time.time)
    retrieval_log: list = field(default_factory=list)  # 该轮检索留痕（可观测）


@dataclass
class Session:
    """一次会话：含上下文历史与元信息。

    注（2026-09-21）：此处原有一个 ``city`` 字段，注释写「当前会话锁定的城市
    （条件澄清后写入）」，但实测**从未被写入、也从未被读取**——是死字段，
    已按「零容忍死代码」删除。地区条件的承接实际由**会话历史文本**承担：
    每轮消息都会 append 进 ``turns``、下一轮作为上下文喂给模型，故无需另设状态位
    （多一个状态位，就多一处可能与历史不一致的地方）。
    """

    session_id: str
    turns: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def append(self, turn: Turn) -> None:
        self.turns.append(turn)
        self.updated_at = time.time()

    def context_window(self, max_turns: int = 12) -> list[Turn]:
        """返回最近 N 轮上下文（控制上下文长度，避免无限膨胀）。"""
        return self.turns[-max_turns:]


class SessionStore:
    """内存会话存储：会话隔离 = 每个 session_id 独立 Session 对象。"""

    def __init__(self) -> None:
        self._store: dict[str, Session] = {}
        self._lock = threading.RLock()

    def get_or_create(self, session_id: Optional[str] = None) -> Session:
        sid = session_id or self.new_id()
        with self._lock:
            sess = self._store.get(sid)
            if sess is None:
                sess = Session(session_id=sid)
                self._store[sid] = sess
            return sess

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._store.get(session_id)

    def new_id(self) -> str:
        return f"s_{uuid.uuid4().hex[:16]}"

    def reset(self, session_id: str) -> bool:
        """重置会话（清空上下文）。返回是否存在该会话。"""
        with self._lock:
            if session_id in self._store:
                del self._store[session_id]
                return True
            return False

    def count(self) -> int:
        with self._lock:
            return len(self._store)


# 模块级单例（全进程共享会话状态）。
session_store = SessionStore()


def get_messages(session_id: str, max_turns: int = 12) -> list[dict]:
    """返回最近 N 轮的 {role, content} 列表（供模型上下文）。"""
    sess = session_store.get(session_id)
    if not sess:
        return []
    return [
        {"role": t.role, "content": t.content}
        for t in sess.context_window(max_turns)
    ]


def append(session_id: str, role: str, content: str) -> None:
    """追加一轮对话记录（自动创建会话）。"""
    sess = session_store.get_or_create(session_id)
    sess.append(Turn(role=role, content=content))


def list_sessions() -> list[dict]:
    """进阶3：返回全部历史会话的轻量清单（供前端「历史记录」入口展示）。

    只暴露可展示的元信息（session_id / 创建·更新时间 / 轮数 / 首问预览），
    不返回完整对话内容（回看由切换到该 session 后继续承接实现，避免冗余搬运）。
    内存版会话：重启即清空，持久化（文件/Redis）后续接入、接口不变。
    """
    with session_store._lock:
        items = list(session_store._store.values())
    out: list[dict] = []
    for s in items:
        first_q = ""
        for t in s.turns:
            if t.role == "user":
                first_q = t.content
                break
        out.append({
            "session_id": s.session_id,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "turns": len(s.turns),
            "first_query": first_q,
        })
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out
