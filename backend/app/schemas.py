"""API 请求/响应模型：定义前后端契约，Swagger 文档会自动生成。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """多轮对话中的一条历史消息。"""

    role: str = Field(..., pattern="^(user|assistant)$", description="user 或 assistant")
    content: str


class ChatRequest(BaseModel):
    """问答请求。"""

    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    history: list[ChatMessage] = Field(default_factory=list, description="历史对话")


class SourceItem(BaseModel):
    """回答引用的知识块。"""

    content: str
    score: float | None = None
    source: str | None = None
    page: int | None = None


class ChatResponse(BaseModel):
    """问答响应。"""

    answer: str
    sources: list[SourceItem] = Field(default_factory=list)


class DocumentInfo(BaseModel):
    """知识库中的一个文件。"""

    name: str
    relative_path: str
    size: int
    modified: str


class IndexStatus(BaseModel):
    """索引状态。"""

    index_name: str
    doc_count: int = 0
    chunk_count: int = 0
    parent_count: int = 0
    data_dir: str
    error: str | None = None


# ---------------- Agent ----------------


class AgentChatRequest(BaseModel):
    """Agent 对话请求。"""

    question: str = Field(..., min_length=1, max_length=4000, description="用户问题")
    images: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="用户上传的图片（data URL 或 base64，最多 6 张）",
    )
    conversation_id: int | None = Field(
        default=None, description="会话 ID；不传则新建会话"
    )
    use_web_search: bool = Field(
        default=False, description="是否启用联网搜索工具"
    )
    use_knowledge_base: bool = Field(
        default=False, description="是否启用知识库检索工具"
    )
    template_id: int | None = Field(
        default=None, description="预设提示词模板 ID；不传使用默认"
    )
    system_prompt: str | None = Field(
        default=None, description="会话级自定义提示词（优先级最高）"
    )
    tool_mode: str = Field(
        default="auto",
        pattern="^(auto|knowledge|web|none)$",
        description=(
            "工具模式：auto=模型按需决定 / knowledge=仅知识库 / "
            "web=仅联网 / none=不启用工具"
        ),
    )
    plan_only: bool = Field(
        default=False,
        description=(
            "计划模式：只读探索并输出执行计划，敏感操作（写文件/编辑/删除/命令）"
            "会被拦截；用户确认计划后以 resume_plan 复用同一计划执行。"
        ),
    )
    resume_plan: list[str] | None = Field(
        default=None,
        description="计划模式确认后复用已确认的计划步骤（不再重新规划）",
    )


class AgentChatResponse(BaseModel):
    """Agent 对话响应（非流式）。"""

    conversation_id: int | None = None
    title: str | None = None
    answer: str
    sources: list[dict] = Field(default_factory=list)
    tool_trace: list[dict] = Field(default_factory=list)


# ---------------- 视觉（SenseNova 识图）----------------


class VisionDescribeRequest(BaseModel):
    """识图请求：一张或多张图片 + 问题。"""

    images: list[str] = Field(
        ..., min_length=1, max_length=6, description="图片（data URL 或 base64）"
    )
    question: str = Field(
        default="请详细描述这张图片的内容，包括可见的文字、图表、人物、场景等。",
        max_length=1000,
    )
    max_new_tokens: int = Field(default=1024, ge=16, le=4096)


class VisionDescribeResponse(BaseModel):
    """识图响应。"""

    answer: str
    model: str
    provider: str = "sensenova"


# ---------------- 会话与消息 ----------------


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    template_id: int | None = Field(default=None, description="绑定的提示词模板 ID")


class ConversationRename(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    template_id: int | None = Field(default=None, description="绑定的提示词模板 ID（0=默认）")


class ConversationOut(BaseModel):
    id: int
    title: str
    template_id: int | None = None
    system_prompt: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    tool_trace: list[dict] | None = None
    sources: list[dict] | None = None
    created_at: datetime


# ---------------- 提示词模板 ----------------


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=255)
    category: str = Field(default="general", max_length=50)
    content: str = Field(..., min_length=1)


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=50)
    content: str | None = Field(default=None, min_length=1)


class TemplateOut(BaseModel):
    id: int
    name: str
    description: str
    category: str
    content: str
    is_system: bool
    created_at: datetime
    updated_at: datetime
