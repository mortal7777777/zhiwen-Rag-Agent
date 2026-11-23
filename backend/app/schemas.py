"""API 请求/响应模型：定义前后端契约，Swagger 文档会自动生成。"""

from __future__ import annotations

from datetime import datetime

from typing import Literal

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


class IndexHealth(IndexStatus):
    """索引健康：manifest 指纹与当前数据目录的差异（增量重建的输入）。"""

    tracked_files: int = 0
    current_files: int = 0
    added_count: int = 0
    changed_count: int = 0
    missing_count: int = 0
    added_files: list[str] = []
    changed_files: list[str] = []
    missing_files: list[str] = []
    stale: bool = False
    last_built_at: str | None = None
    parse_version: str = ""
    parse_upgrade_required: bool = False
    parse_reports: dict = Field(default_factory=dict)
    rebuild: dict = Field(default_factory=dict)
    ingest: dict = Field(default_factory=dict)
    paddle_ocr: dict = Field(default_factory=dict)


# ---------------- Agent ----------------


class AgentAttachmentOut(BaseModel):
    """对话附件元数据（不含正文）。"""

    id: str
    name: str
    size: int
    chars: int
    truncated: bool = False
    warning: str | None = None


class AgentAttachmentUploadResponse(BaseModel):
    """附件上传结果。"""

    attachments: list[AgentAttachmentOut] = Field(default_factory=list)


class AgentKbScope(BaseModel):
    """本轮知识库检索范围：单文档 / 整本书 / 多文档。"""

    type: Literal["doc", "book", "docs"] = Field(
        default="doc", description="doc=单文档 / book=整本书 / docs=多文档"
    )
    value: str = Field(
        default="",
        max_length=500,
        description="doc 传相对路径或文件名；book 传书名（匹配库内文件名）",
    )
    values: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="type=docs 时的文档相对路径列表",
    )


class AgentQuote(BaseModel):
    """用户在文档查看器中划选的原文（直接进上下文，不依赖检索命中）。"""

    text: str = Field(..., min_length=1, max_length=8000)
    context: str = Field(default="", max_length=8000, description="所在段落前后文")
    source: str = Field(default="", max_length=500, description="文档相对路径")
    page: int | None = Field(default=None, ge=1)


class AgentChatRequest(BaseModel):
    """Agent 对话请求。"""

    question: str = Field(..., min_length=1, max_length=4000, description="用户问题")
    images: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="用户上传的图片（data URL 或 base64，最多 6 张）",
    )
    attachment_ids: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="对话附件 ID（POST /api/agent/attachments 返回，最多 5 个）",
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
    kb_scope: AgentKbScope | None = Field(
        default=None, description="限定知识库检索范围（单文档/整本书/多文档）"
    )
    quote: AgentQuote | None = Field(
        default=None, description="查看器划选的原文（直接进上下文）"
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
            "会被拦截；用户确认计划后在同一 run 内继续执行（原生挂起/恢复）。"
        ),
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


class AgentApprovalDecision(BaseModel):
    """一条审批决定（对应 run_suspended 事件里的一个待审批项）。"""

    call_id: str = Field(..., description="待审批项的 call_id")
    approve: bool = True
    reason: str = Field(default="", max_length=200)
    remember_session: bool = Field(default=False, description="本会话内同命令不再询问")
    remember_forever: bool = Field(default=False, description="写入命令白名单，永久放行")


class AgentPlanDecision(BaseModel):
    """计划确认决定（exit_plan_mode 挂起）。"""

    approved: bool = True
    steps: list[str] = Field(
        default_factory=list, description="确认/编辑后的步骤（不传则用挂起时的原计划）"
    )
    reason: str = Field(default="", max_length=200)


class AgentAnswerItem(BaseModel):
    """一个问题的回答（对应 ask_user_question 挂起载荷里的一项）。"""

    index: int = Field(..., description="问题在该次调用 questions 中的下标")
    selected: list[str] = Field(default_factory=list, description="选中的选项 label")
    other: str = Field(default="", max_length=500, description="自定义回答")
    skipped: bool = Field(default=False, description="用户跳过该问题")


class AgentAnswer(BaseModel):
    """一次 ask_user_question 调用的回答集合。"""

    call_id: str = Field(..., description="提问调用的 call_id")
    items: list[AgentAnswerItem] = Field(default_factory=list)


class AgentResumeRequest(BaseModel):
    """恢复挂起的运行（敏感操作审批 / 计划确认 / 用户提问）：新 SSE 流续跑。"""

    conversation_id: int = Field(..., description="挂起所在的会话 ID")
    decisions: list[AgentApprovalDecision] = Field(
        default_factory=list, description="审批决定；缺省项按拒绝处理"
    )
    plan: AgentPlanDecision | None = Field(
        default=None, description="计划确认决定（挂起载荷含 plan 时提交）"
    )
    answers: list[AgentAnswer] = Field(
        default_factory=list, description="用户提问的回答；缺省项按跳过处理"
    )
    stopped: bool = Field(
        default=False, description="用户在挂起期间点了停止：全拒收尾，不再执行"
    )


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
    project_dir: str | None = Field(
        default=None, max_length=500, description="会话工作区（工作目录）"
    )


class ConversationUpdate(BaseModel):
    """PATCH /conversations/{id} 的载荷：全部字段可选，只改传了的。

    title 置空跳过（保留旧名）——置顶切换不需要重发标题；
    template_id 语义不变（0=切回默认模板，None=不修改）；
    pinned=True/False 置顶/取消置顶（2026-11-19 P3）。
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    template_id: int | None = Field(default=None, description="绑定的提示词模板 ID（0=默认）")
    pinned: bool | None = Field(default=None, description="置顶（置顶组排列表最前）")
    project_dir: str | None = Field(
        default=None,
        max_length=500,
        description="会话工作区（工作目录）；null=不修改，\"\"=清除",
    )


class ConversationOut(BaseModel):
    id: int
    title: str
    template_id: int | None = None
    project_dir: str | None = None
    system_prompt: str | None = None
    prompt_bundle_hash: str | None = None
    prompt_locked_at: datetime | None = None
    # 会话级 fork 来源（6.9）：普通会话为 None
    forked_from_conversation_id: int | None = None
    forked_from_message_id: int | None = None
    pinned: bool = False
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ConversationSearchOut(ConversationOut):
    """会话搜索结果：在会话字段外带正文命中数与首个命中片段。"""

    snippet: str = ""
    match_count: int = 0


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


class ConversationForkRequest(BaseModel):
    """会话级分支：复制目标消息（含）之前的消息到新会话。"""
    message_id: int


# ---------------- 提示词模板 ----------------


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=255)
    category: str = Field(default="general", max_length=50)
    content: str = Field(..., min_length=1, max_length=50000)


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=50)
    content: str | None = Field(default=None, min_length=1, max_length=50000)
    version_note: str | None = Field(default=None, max_length=500)


class TemplateOut(BaseModel):
    id: int
    name: str
    description: str
    category: str
    content: str
    is_system: bool
    system_key: str | None = None
    archived: bool = False
    content_hash: str | None = None
    latest_version_id: int | None = None
    latest_version_no: int | None = None
    version_count: int = 0
    conversation_count: int = 0
    draft_content: str | None = None
    draft_note: str = ""
    draft_updated_at: datetime | None = None
    has_draft: bool = False
    created_at: datetime
    updated_at: datetime


class PromptVersionRestore(BaseModel):
    version_id: int
    note: str = Field(default="", max_length=500)


class PromptFragmentUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)
    note: str = Field(default="", max_length=500)


class PromptLintRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)


class PromptPreviewRequest(BaseModel):
    conversation_id: int | None = None
    template_id: int | None = None
    template_content: str | None = Field(default=None, max_length=50000)
    fragment_key: str | None = Field(default=None, max_length=100)
    fragment_content: str | None = Field(default=None, max_length=20000)
    use_knowledge_base: bool = True
    use_web_search: bool = False
    advanced_tools: bool = True
    plan_only: bool = False
    include_live_context: bool = True


class PromptDraftSave(BaseModel):
    content: str = Field(..., min_length=1, max_length=50000)
    note: str = Field(default="", max_length=500)


class PromptDraftPublish(BaseModel):
    note: str = Field(default="", max_length=500)


class PromptBatchApply(BaseModel):
    conversation_ids: list[int] | None = None


class PromptPinRequest(BaseModel):
    version_id: int
    fragment_version_ids: dict[str, int] | None = None


class PromptTrialABRequest(BaseModel):
    template_id: int
    version_a_id: int
    version_b_id: int
    question: str = Field(..., min_length=1, max_length=4000)


class PromptTrialEvalRequest(BaseModel):
    template_id: int
    version_id: int
    limit: int = Field(default=3, ge=1, le=10)


class PromptImportRequest(BaseModel):
    data: dict


# ---------------- 子代理类型注册表 ----------------


class AgentTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    mode: str | None = Field(default=None, max_length=20)
    system_prompt: str | None = Field(default=None, max_length=20000)
    tools: list[str] | None = None
    max_rounds: int | None = Field(default=None, ge=1, le=20)
    model_provider_id: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    thinking: str | None = Field(default=None, max_length=20)
    llm_budget: int | None = Field(default=None, ge=1, le=200)
    read_only: bool | None = None
    enabled: bool | None = None


class AgentTypeCreate(AgentTypeUpdate):
    key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    name: str = Field(..., min_length=1, max_length=100)
    mode: str = Field(default="research", max_length=20)


class AgentTypeOut(BaseModel):
    key: str
    name: str
    description: str = ""
    mode: str
    system_prompt: str = ""
    tools: list[str] = []
    max_rounds: int | None = None
    model_provider_id: str = ""
    model: str = ""
    thinking: str = "inherit"
    llm_budget: int | None = None
    read_only: bool = True
    enabled: bool = True
    version: int = 1
    updated_at: datetime | None = None
    is_builtin: bool = False
