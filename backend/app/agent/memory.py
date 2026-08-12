"""LangChain 记忆层：把 MySQL 中的会话消息封装成 BaseChatMessageHistory。

对话记忆是"类 DeepSeek 网页端"的核心：每条 user/assistant 消息都持久化到
MySQL 的 messages 表，下次提问时按最近 N 条加载并回传给模型。
"""

from __future__ import annotations

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy.orm import Session

from ..db import repository as repo


class MySQLChatMessageHistory(BaseChatMessageHistory):
    """基于 MySQL 的 LangChain 聊天历史。"""

    def __init__(
        self,
        conversation_id: int,
        db: Session,
        max_messages: int = 20,
    ):
        super().__init__()
        self.conversation_id = conversation_id
        self.db = db
        self.max_messages = max_messages

    @property
    def messages(self) -> list[BaseMessage]:
        """按时间正序返回最近 max_messages 条消息（LangChain BaseMessage）。"""
        rows = repo.load_history_messages(
            self.db, self.conversation_id, self.max_messages
        )
        result: list[BaseMessage] = []
        for row in rows:
            if row["role"] == "user":
                result.append(HumanMessage(content=row["content"]))
            elif row["role"] == "assistant":
                result.append(AIMessage(content=row["content"]))
        return result

    def add_message(self, message: BaseMessage) -> None:
        """把一条 LangChain 消息写入 MySQL。"""
        if isinstance(message, HumanMessage):
            repo.add_message(
                self.db, self.conversation_id, "user", message.content
            )
        elif isinstance(message, AIMessage):
            repo.add_message(
                self.db, self.conversation_id, "assistant", message.content
            )

    def add_user_message(self, message) -> None:
        content = message.content if isinstance(message, HumanMessage) else str(message)
        repo.add_message(self.db, self.conversation_id, "user", content)

    def add_ai_message(self, message) -> None:
        content = message.content if isinstance(message, AIMessage) else str(message)
        repo.add_message(self.db, self.conversation_id, "assistant", content)

    def add_assistant_message(
        self,
        content: str,
        tool_trace: list | None = None,
        sources: list | None = None,
    ) -> None:
        """保存 assistant 消息，附带工具轨迹与引用来源（前端展示用）。"""
        repo.add_message(
            self.db,
            self.conversation_id,
            "assistant",
            content,
            tool_trace=tool_trace,
            sources=sources,
        )

    def clear(self) -> None:
        """清空该会话的消息。"""
        repo.clear_messages(self.db, self.conversation_id)
