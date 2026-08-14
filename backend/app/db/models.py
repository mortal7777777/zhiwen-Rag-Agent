"""SQLAlchemy ORM 模型：会话、消息、提示词模板。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


class Conversation(Base):
    """一次对话会话，对应前端左侧的会话列表项。"""

    __tablename__ = "conversations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False, default="新对话")
    # 会话绑定的提示词模板（跟随会话持久化，切换会话自动恢复）
    template_id = Column(BigInteger, nullable=True)
    # 会话工作目录（CLI 启动目录）：文件/命令工具的工作根，跟随会话保存
    project_dir = Column(String(500), nullable=True)
    # 会话级自定义提示词（可选，优先级高于模板）
    system_prompt = Column(Text, nullable=True)
    # 早期对话滚动摘要（会话压缩后老消息的浓缩，仍保留原文供前端展示）
    summary = Column(Text, nullable=True)
    # 已纳入摘要的最大消息 id，避免重复总结
    summary_up_to_id = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )


class Message(Base):
    """一条对话消息。tool_trace / sources 以 JSON 字符串存储。"""

    __tablename__ = "messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(
        BigInteger,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False)  # user | assistant
    content = Column(Text, nullable=False)
    tool_trace = Column(Text, nullable=True)  # JSON 数组：工具调用过程
    sources = Column(Text, nullable=True)     # JSON 数组：知识库引用
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)

    conversation = relationship("Conversation", back_populates="messages")


class AgentRun(Base):
    """Agent 决策运行记录：问题、计划、工具轨迹（含耗时）、状态、延迟。"""

    __tablename__ = "agent_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, nullable=True, index=True)
    question = Column(Text, nullable=False)
    plan = Column(Text, nullable=True)          # JSON 数组
    tool_trace = Column(Text, nullable=True)    # JSON 数组（含 duration_ms）
    answer_len = Column(Integer, default=0, nullable=False)
    latency_ms = Column(Integer, default=0, nullable=False)
    status = Column(String(20), default="ok", nullable=False)  # ok | error | stopped
    error = Column(Text, nullable=True)
    token_usage = Column(Text, nullable=True)   # JSON
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


class PromptTemplate(Base):
    """预设/自定义提示词模板。is_system=True 为内置模板，只读。"""

    __tablename__ = "prompt_templates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255), default="")
    category = Column(String(50), default="general")
    content = Column(Text, nullable=False)
    is_system = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )


class Memory(Base):
    """长期事实记忆：从对话中提取的用户偏好、背景、项目、决定等。

    category: profile(用户画像) / preference(喜好偏好) / project(工作项目)
              / decision(重要决定) / lesson(教训与应对) / other(其他)
    status:   active(生效) / archived(已归档，被新事实覆盖或过时)
    """

    __tablename__ = "memories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    category = Column(String(50), default="other", nullable=False)
    status = Column(String(20), default="active", nullable=False)
    source_conversation_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )


class AppMeta(Base):
    """应用级键值元数据（如记忆上次整合时间、用户画像摘要）。"""

    __tablename__ = "app_meta"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )
