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
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import relationship

from .database import Base


class Conversation(Base):
    """一次对话会话，对应前端左侧的会话列表项。"""

    __tablename__ = "conversations"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
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
    # 会话首次运行时锁定的提示词 bundle（模板版本 + 各片段版本，JSON）
    prompt_bundle = Column(Text, nullable=True)
    prompt_bundle_hash = Column(String(64), nullable=True)
    prompt_locked_at = Column(DateTime, nullable=True)
    # 会话级 fork 来源（6.9）：从哪个会话的哪条消息分支出来；普通会话为 NULL。
    # 不建外键：源会话删除后仍保留溯源 id（前端按 id 查不到时显示"来源已删除"）。
    forked_from_conversation_id = Column(BigInteger, nullable=True)
    forked_from_message_id = Column(BigInteger, nullable=True)
    # 会话置顶（P3·2026-11-19）：置顶组排列表最前，组内仍按更新时间；
    # server_default 兼容迁移给存量行补 False
    pinned = Column(Boolean, nullable=False, default=False, server_default=false())
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

    # 与 Conversation.id 同样的 sqlite 变体：BIGINT 主键在 SQLite 下不会自增
    # （测试用内存库 create_all 建表，MySQL 侧仍是 BIGINT 不变）
    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
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


class DocumentMeta(Base):
    """知识库文档的自定义元数据：分类 / 标签 / 备注（按相对路径唯一）。

    分类由用户在知识库管理页维护，用于前端筛选与后续定向检索；
    文档本体仍在 data/ 目录，删除文档时对应元数据残留无害（按需清理）。
    """

    __tablename__ = "document_meta"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    relative_path = Column(String(500), nullable=False, unique=True, index=True)
    category = Column(String(100), default="", nullable=False)
    tags = Column(String(500), default="", nullable=False)
    notes = Column(Text, nullable=True)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


class DocumentVersion(Base):
    """文档历史版本（只存归档行；当前版本 = data/ 里的文件本身）。

    归档文件落盘位置：{data_dir.parent}/data_versions/{doc_relative_path}/{file_name}。
    版本号是标识、当前/归档是状态（当前版本不一定是最大号）。
    """

    __tablename__ = "document_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    doc_relative_path = Column(String(500), nullable=False, index=True)
    version_no = Column(String(50), nullable=False)
    file_name = Column(String(255), nullable=False)
    original_name = Column(String(255), nullable=False)
    note = Column(Text, nullable=True)
    size = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    __table_args__ = (
        UniqueConstraint("doc_relative_path", "version_no", name="uq_doc_version"),
    )


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

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255), default="")
    category = Column(String(50), default="general")
    content = Column(Text, nullable=False)
    is_system = Column(Boolean, default=False, nullable=False)
    # 系统模板稳定标识（如 general/coding/default）；用户模板为 NULL
    system_key = Column(String(100), nullable=True)
    archived = Column(Boolean, default=False, nullable=False)
    draft_content = Column(Text, nullable=True)
    draft_note = Column(Text, default="")
    draft_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )
    __table_args__ = (
        UniqueConstraint("system_key", name="uq_prompt_templates_system_key"),
    )


class PromptTemplateVersion(Base):
    """提示词模板的不可变历史版本（最新版本 = prompt_templates.content）。"""

    __tablename__ = "prompt_template_versions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    template_id = Column(
        BigInteger,
        ForeignKey("prompt_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "template_id", "version_no", name="uq_prompt_template_version"
        ),
    )


class PromptFragment(Base):
    """系统提示词中的固定片段（工具规则/计划模式/todos/技能/KB 清单）。"""

    __tablename__ = "prompt_fragments"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    key = Column(String(100), nullable=False, unique=True, index=True)
    title = Column(String(200), default="")
    description = Column(String(255), default="")
    scope = Column(String(20), default="static")  # static | dynamic
    editable = Column(Boolean, default=True, nullable=False)
    is_customized = Column(Boolean, default=False, nullable=False)
    # 用户上次编辑时对应的代码默认哈希；用于发现“默认已升级”
    base_default_hash = Column(String(64), nullable=True)
    draft_content = Column(Text, nullable=True)
    draft_note = Column(Text, default="")
    draft_updated_at = Column(DateTime, nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )


class PromptFragmentVersion(Base):
    """提示词片段的不可变历史版本（最新版本 = prompt_fragments.content）。"""

    __tablename__ = "prompt_fragment_versions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    fragment_id = Column(
        BigInteger,
        ForeignKey("prompt_fragments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "fragment_id", "version_no", name="uq_prompt_fragment_version"
        ),
    )


class PromptTrial(Base):
    """提示词 A/B 试跑或轻量评测记录。"""

    __tablename__ = "prompt_trials"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind = Column(String(20), default="ab", nullable=False)  # ab | eval
    template_id = Column(
        BigInteger,
        ForeignKey("prompt_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version_a_id = Column(BigInteger, nullable=True)
    version_b_id = Column(BigInteger, nullable=True)
    question = Column(Text, default="")
    status = Column(String(20), default="done", nullable=False)
    result = Column(Text, nullable=True)  # JSON
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


class Memory(Base):
    """长期事实记忆：从对话中提取的用户偏好、背景、项目、决定等。

    category: profile(用户画像) / preference(喜好偏好) / project(工作项目)
              / decision(重要决定) / lesson(教训与应对) / other(其他)
    status:   active(生效) / archived(已归档，被新事实覆盖或过时)

    时间字段语义（2026-09-30 修正，此前三者混淆导致冲突判定失效）：
    - created_at：事实**首次入库**时间（语句发生的近似时间，用于判定新旧与
      "最近的事实"排序）；
    - updated_at：**内容/分类被编辑或合并**的时间——命中计数累加、归档/恢复等
      管理动作都不刷新（不要用 onupdate，否则召回也会刷新它）；
    - last_hit_at：最后一次被注入上下文的时间（仅观测/面板展示——**不作归档依据**，
      归档只允许"被合并的重复项"与"被新事实明确覆盖的旧项"，见 consolidate_memories）。
    """

    __tablename__ = "memories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    category = Column(String(50), default="other", nullable=False)
    status = Column(String(20), default="active", nullable=False)
    source_conversation_id = Column(BigInteger, nullable=True)
    # 被注入上下文的累计次数与最后一次时间：仅供观测与面板展示。
    # 注意：命中次数**不作为整合归档依据**（2026-09-30 收紧：旧记忆可能只是还没被召回）
    hit_count = Column(Integer, default=0, nullable=False)
    last_hit_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    # 注意：不带 onupdate。内容变更由 repository.update_memory 显式写入，
    # 命中计数（bump_memory_hits）不得改写它。
    updated_at = Column(
        DateTime,
        default=datetime.now,
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


class AgentType(Base):
    """子代理类型注册表（2026-11-21，Phase A 6.1）。

    mode 仍只允许 research/explore/review/execute（决定只读语义与工具大类）；
    system_prompt 是追加在 SUBAGENT_BASE_PROMPT 之后的模式段；tools 为空表示
    用该 mode 的默认工具集，非空时只能是 mode 工具集的子集（收窄不扩权）。
    model_* / thinking / llm_budget 为空表示继承主模型与全局预算。
    """

    __tablename__ = "agent_types"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    key = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255), default="")
    mode = Column(String(20), default="research", nullable=False)
    system_prompt = Column(Text, default="")
    tools = Column(Text, default="[]")  # JSON 数组
    max_rounds = Column(Integer, nullable=True)
    model_provider_id = Column(String(100), default="")
    model = Column(String(200), default="")
    thinking = Column(String(20), default="inherit")  # inherit | on | off
    llm_budget = Column(Integer, nullable=True)
    read_only = Column(Boolean, default=True, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )
