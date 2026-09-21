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
    category: str = ""
    tags: str = ""


class DocumentMetaIn(BaseModel):
    """保存文档自定义分类/标签/备注。"""

    relative_path: str = Field(..., min_length=1, max_length=500)
    category: str = Field(default="", max_length=100)
    tags: str = Field(default="", max_length=500)
    notes: str | None = Field(default=None, max_length=4000)


class UploadConflictItem(BaseModel):
    """上传查重的一条冲突（仅警告，供前端确认）。"""

    kind: str  # same_name | identical | similar_name | near_duplicate
    existing_name: str = ""
    existing_relative_path: str = ""
    similarity: float | None = None
    message: str = ""


class UploadFileCheckOut(BaseModel):
    """单个上传文件的查重报告。"""

    file_name: str
    would_replace: bool = False
    suggested_version_no: str = "v1"
    conflicts: list[UploadConflictItem] = Field(default_factory=list)


class UploadCheckOut(BaseModel):
    """上传预检/冲突响应（dry_run 或 409 detail 共用结构）。"""

    files: list[UploadFileCheckOut] = Field(default_factory=list)
    has_conflict: bool = False


class DocumentRenameIn(BaseModel):
    """重命名请求（仅同目录改基名）。"""

    relative_path: str = Field(..., min_length=1, max_length=500)
    new_name: str = Field(..., min_length=1, max_length=200)


class DocumentVersionOut(BaseModel):
    """一条历史版本（归档行）。"""

    id: int
    version_no: str
    note: str = ""
    size: int = 0
    created_at: str = ""
    file_name: str
    original_name: str
    file_exists: bool = True


class DocumentVersionsOut(BaseModel):
    """某文档的版本列表（当前版本 + 历史版本）。"""

    doc_relative_path: str
    name: str
    current: dict | None = None
    suggested_version_no: str = "v1"
    versions: list[DocumentVersionOut] = Field(default_factory=list)


class DocumentVersionsArchiveOut(DocumentVersionsOut):
    documents: list[DocumentInfo] = Field(default_factory=list)


class DocumentVersionsRestoreOut(DocumentVersionsOut):
    documents: list[DocumentInfo] = Field(default_factory=list)
    new_relative_path: str | None = None


class DocumentVersionsDeleteOut(BaseModel):
    versions: list[DocumentVersionOut] = Field(default_factory=list)


class DocumentVersionArchiveIn(BaseModel):
    """把 source 文档归档为 target 文档的历史版本。"""

    source_path: str = Field(..., min_length=1, max_length=500)
    target_path: str = Field(..., min_length=1, max_length=500)
    version_no: str = Field(..., min_length=1, max_length=50)
    note: str = Field(default="", max_length=500)


class DocumentVersionRestoreIn(BaseModel):
    """恢复某历史版本为当前；current 版本将被归档（版本号用户指定）。"""

    new_version_no: str = Field(..., min_length=1, max_length=50)
    note: str = Field(default="", max_length=500)


class IndexStatus(BaseModel):
    """索引状态。"""

    index_name: str
    physical_index: str | None = None
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
    project_dir: str | None = Field(
        default=None,
        description="CLI 启动目录；用于加载该目录下的 AGENTS.md 项目记忆",
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
    command_sandbox: str | None = Field(
        default=None,
        pattern="^(subprocess|docker)$",
        description=(
            "请求级命令沙箱覆盖（CLI --sandbox）：subprocess=宿主执行 / "
            "docker=容器沙箱；不传则跟随设置页 command_sandbox"
        ),
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


class RewindRequest(BaseModel):
    """消息级回退：删除该消息及其之后的全部消息（类 Claude Code rewind）。"""
    message_id: int


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
