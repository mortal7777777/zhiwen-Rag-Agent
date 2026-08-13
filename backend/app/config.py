"""集中管理所有配置项：路径、OpenSearch、本地模型、DeepSeek、CORS。

设计说明：
- 所有可变配置都通过环境变量覆盖，便于部署和测试；
- 默认复用 day5_2 的本地模型目录（langchain01/local_models），完全离线；
- Settings 是 frozen dataclass，创建后不可修改，避免运行时被意外改动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# rag_knowledge_base/（config.py 位于 backend/app/ 下，向上 2 级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# langchain01/local_models：与 day5_2 共用本地模型
LOCAL_MODELS_ROOT = PROJECT_ROOT.parent / "local_models"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default.resolve()


@dataclass(frozen=True)
class Settings:
    """应用全局配置。"""

    app_name: str = "个人知识库 RAG 智能助手"
    version: str = "1.0.0"

    # ---- 路径 ----
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    meta_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "opensearch_meta")
    embedding_model_dir: Path = field(
        default_factory=lambda: LOCAL_MODELS_ROOT / "bge-base-zh-v1.5"
    )
    reranker_cache_dir: Path = field(
        default_factory=lambda: LOCAL_MODELS_ROOT / "models--BAAI--bge-reranker-v2-m3"
    )

    # ---- OpenSearch ----
    opensearch_url: str = "http://localhost:9200"
    opensearch_index: str = "rag_knowledge_base_v2"

    # ---- 生成模型（DeepSeek，OpenAI 兼容接口）----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    main_model_vision: bool = False  # 主对话模型是否原生支持视觉（DeepSeek V4 Flash 不支持）

    # ---- 视觉模型：日日新 SenseNova（多模态，用于识图 / OCR / 图片理解）----
    sensenova_api_key: str = ""
    sensenova_base_url: str = "https://token.sensenova.cn/v1"  # Token Plan（OpenAI 兼容端点）
    sensenova_model: str = "sensenova-6.8-flash-lite"
    vision_timeout: int = 120
    vision_auto_describe: bool = True   # 用户上传图片时自动用视觉模型生成描述并注入上下文
    vision_max_images: int = 6          # 单次最多识别图片数

    # ---- MySQL（对话记忆 / 会话 / 提示词模板）----
    mysql_url: str = (
        "mysql+pymysql://root:@127.0.0.1:3306/rag_assistant?charset=utf8mb4"
    )

    # ---- 联网搜索 ----
    web_search_provider: str = "duckduckgo"  # duckduckgo | tavily | off
    tavily_api_key: str = ""
    web_search_max_results: int = 6

    # ---- Agent 参数 ----
    agent_max_iterations: int = 6       # ReAct 循环最大轮数（工具调用次数上限）
    agent_max_failures: int = 3         # 工具连续失败重试上限（失败不占迭代预算，超过则强制收尾）
    agent_recursion_limit: int = 30     # LangGraph 图执行最大步数（超过则兜底收尾）
    chat_temperature: float = 0.5       # 普通回答温度
    history_max_messages: int = 60      # 回传给模型的历史消息条数（软窗口，不足预算不压缩）
    agent_title_model: str = "deepseek-v4-flash"

    # ---- 记忆与上下文工程 ----
    summary_enabled: bool = True        # 会话滚动摘要（压缩早期对话）
    summary_max_chars: int = 800        # 摘要最大字符数
    history_max_tokens: int = 32000     # 注入历史消息的 token 预算（估算，为长窗口模型放宽）
    memory_enabled: bool = True         # 长期事实记忆
    # 自动提取记忆的最小回答长度：短问答不触发记忆提取 LLM 调用（省钱提速）
    memory_auto_extract_min_chars: int = 400
    memory_top_k: int = 3               # 每次注入最相关的记忆条数
    memory_min_score: float = 0.35      # 记忆召回相似度阈值（BGE 余弦）
    memory_max_tokens: int = 600        # 每次注入记忆的 token 预算
    memory_candidate_limit: int = 100   # 记忆召回粗筛后的候选上限（避免全量 embedding）
    memory_recent_fallback: int = 50    # 语义不足时用最近 N 条记忆补位
    memory_consolidate_interval_hours: int = 24  # 自动整合整理间隔
    memory_consolidate_threshold: int = 10      # 活跃记忆超过此条数才触发自动整合
    planner_enabled: bool = True        # 复杂问题先规划（Plan-and-Execute 轻量版）

    # ---- CRAG（知识库不足时自动补联网）----
    crag_fallback_enabled: bool = True
    crag_min_score: float = 0.45        # 最佳相关度低于此值触发联网兜底

    # ---- 文档解析增强 ----
    pdf_layout_enabled: bool = True     # 布局感知 PDF 解析（PyMuPDF4LLM，保留标题/段落/表格）
    pdf_vision_ocr_enabled: bool = False  # 扫描件/图片页是否用视觉模型做 OCR（需配置 SenseNova key）
    pdf_vision_ocr_max_pages: int = 20    # 单文件最多 OCR 页数（防止烧额度）
    pdf_parse_workers: int = 0            # 大 PDF 布局解析并行进程数（实测本机并行无增益，默认串行）

    # ---- 嵌入性能 ----
    embedding_fp16: bool = True           # CUDA 下用 fp16 嵌入（实测约 3 倍提速）
    embed_batch_size: int = 128           # 文档嵌入批大小（同时是 OpenSearch 批量写入大小）

    # ---- 检索参数 ----
    recall_k: int = 40       # 每路检索器各取前 N 条
    candidate_pool: int = 24 # 每路 RRF 融合后送重排序的候选数
    rerank_top_k: int = 4    # 精排后最终保留条数

    # ---- 切分参数（Parent-Child）----
    # 实验结论：递归切分 8% 的块能落在句子边界；按段落分组约 47%。
    # parent 按段落分组（约 600 字，最多 900），child 为句内窗口（约 220 字）。
    parent_chunk_size: int = 600
    parent_max_chunk_size: int = 900
    child_chunk_size: int = 220
    child_overlap: int = 40
    max_parents: int = 6       # 检索后最多返回几个 parent 作为上下文

    # ---- 检索增强开关 ----
    query_expansion_enabled: bool = True  # 总开关：是否启用查询扩展
    expansion_multi_query: bool = True    # Multi-Query：改写成多个角度的查询
    expansion_hyde: bool = True           # HyDE：生成假设性文档作为检索查询
    expansion_multi_turn: bool = True     # 多轮补全：结合历史把指代消歧为独立问题
    rewrite_variants: int = 3             # Multi-Query 改写数量
    parent_child_enabled: bool = True    # 小到大检索（child 检索、parent 出上下文）

    # ---- 并发与监控 ----
    max_concurrency: int = 4              # 同时处理的问答请求数（GPU/检索保护）
    llm_max_concurrency: int = 8          # LLM API 调用并发上限（生成阶段不占 GPU 锁）
    tracing_enabled: bool = True          # 结构化 trace（JSONL，本地可观测性）

    # ---- 高级工具（P0：受控执行 / MCP / checkpoint / 记忆文件）----
    # 默认开启：读取自动执行，写入/编辑/删除/命令默认走人工确认（ask），
    # 确认后才会真正执行，因此默认开启是安全的；不想要该能力可关掉总开关。
    advanced_tools_enabled: bool = True
    agent_subagents_enabled: bool = True   # Send 子代理并行总开关
    tool_workspace: str = ""              # 文件工具白名单根目录（空=项目根）
    command_allowlist: str = ""           # 命令白名单（逗号分隔的命令前缀，空=禁止执行）
    command_timeout: int = 60             # 命令执行超时（秒）
    command_sandbox: str = "subprocess"   # subprocess | docker（docker=容器沙箱）
    sandbox_image: str = "python:3.11-slim"  # docker 沙箱镜像
    sandbox_workspace_readonly: bool = False  # 沙箱内工作目录只读（写入走内存 /scratch）
    tool_permission_mode: str = "ask"     # ask=敏感操作人工确认；allow=自动批准（跳过确认）
    permission_timeout: int = 300         # 等待人工确认的超时（秒），超时自动取消
    skill_sandbox_enabled: bool = False   # 技能沙箱执行开关（默认关）
    reasoning_summary_enabled: bool = True  # 最终作答前生成"思考摘要"（已深度思考折叠区）
    project_memory_file: str = ""         # 文件型项目记忆（AGENTS.md）路径；空=项目根/AGENTS.md
    mcp_enabled: bool = True              # MCP 工具接入总开关
    checkpoint_enabled: bool = True       # 会话中断 checkpoint 恢复
    trajectory_compress_enabled: bool = True  # 长任务轨迹摘要压缩

    # ---- CORS（前端开发服务器地址）----
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构建配置，未设置时使用默认值。"""
        origins = tuple(
            item.strip()
            for item in _env(
                "CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if item.strip()
        )
        return cls(
            data_dir=_env_path("DATA_DIR", PROJECT_ROOT / "data"),
            meta_dir=_env_path("META_DIR", PROJECT_ROOT / "opensearch_meta"),
            embedding_model_dir=_env_path(
                "EMBEDDING_MODEL_DIR",
                LOCAL_MODELS_ROOT / "bge-base-zh-v1.5",
            ),
            reranker_cache_dir=_env_path(
                "RERANKER_CACHE_DIR",
                LOCAL_MODELS_ROOT / "models--BAAI--bge-reranker-v2-m3",
            ),
            opensearch_url=_env("OPENSEARCH_URL", "http://localhost:9200"),
            opensearch_index=_env("OPENSEARCH_INDEX", "rag_knowledge_base_v2"),
            deepseek_api_key=_env("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            main_model_vision=_env("MAIN_MODEL_VISION", "0") == "1",
            sensenova_api_key=_env("SENSENOVA_API_KEY", ""),
            sensenova_base_url=_env(
                "SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"
            ),
            sensenova_model=_env("SENSENOVA_MODEL", "sensenova-6.8-flash-lite"),
            vision_timeout=int(_env("VISION_TIMEOUT", "120")),
            vision_auto_describe=_env("VISION_AUTO_DESCRIBE", "1") == "1",
            vision_max_images=int(_env("VISION_MAX_IMAGES", "6")),
            mysql_url=_env(
                "MYSQL_URL",
                "mysql+pymysql://root:@127.0.0.1:3306/rag_assistant?charset=utf8mb4",
            ),
            web_search_provider=_env("WEB_SEARCH_PROVIDER", "duckduckgo"),
            tavily_api_key=_env("TAVILY_API_KEY", ""),
            web_search_max_results=int(_env("WEB_SEARCH_MAX_RESULTS", "6")),
            agent_max_iterations=int(_env("AGENT_MAX_ITERATIONS", "6")),
            agent_max_failures=int(_env("AGENT_MAX_FAILURES", "3")),
            agent_recursion_limit=int(_env("AGENT_RECURSION_LIMIT", "30")),
            chat_temperature=float(_env("CHAT_TEMPERATURE", "0.5")),
            history_max_messages=int(_env("HISTORY_MAX_MESSAGES", "60")),
            agent_title_model=_env("AGENT_TITLE_MODEL", "deepseek-v4-flash"),
            summary_enabled=_env("SUMMARY_ENABLED", "1") == "1",
            summary_max_chars=int(_env("SUMMARY_MAX_CHARS", "800")),
            history_max_tokens=int(_env("HISTORY_MAX_TOKENS", "32000")),
            memory_enabled=_env("MEMORY_ENABLED", "1") == "1",
            memory_auto_extract_min_chars=int(
                _env("MEMORY_AUTO_EXTRACT_MIN_CHARS", "400")
            ),
            memory_top_k=int(_env("MEMORY_TOP_K", "3")),
            memory_min_score=float(_env("MEMORY_MIN_SCORE", "0.35")),
            memory_max_tokens=int(_env("MEMORY_MAX_TOKENS", "600")),
            memory_candidate_limit=int(_env("MEMORY_CANDIDATE_LIMIT", "100")),
            memory_recent_fallback=int(_env("MEMORY_RECENT_FALLBACK", "50")),
            memory_consolidate_interval_hours=int(
                _env("MEMORY_CONSOLIDATE_INTERVAL_HOURS", "24")
            ),
            memory_consolidate_threshold=int(
                _env("MEMORY_CONSOLIDATE_THRESHOLD", "10")
            ),
            planner_enabled=_env("PLANNER_ENABLED", "1") == "1",
            crag_fallback_enabled=_env("CRAG_FALLBACK_ENABLED", "1") == "1",
            crag_min_score=float(_env("CRAG_MIN_SCORE", "0.45")),
            pdf_layout_enabled=_env("PDF_LAYOUT_ENABLED", "1") == "1",
            pdf_vision_ocr_enabled=_env("PDF_VISION_OCR_ENABLED", "0") == "1",
            pdf_vision_ocr_max_pages=int(_env("PDF_VISION_OCR_MAX_PAGES", "20")),
            pdf_parse_workers=int(_env("PDF_PARSE_WORKERS", "0")),
            embedding_fp16=_env("EMBEDDING_FP16", "1") == "1",
            embed_batch_size=int(_env("EMBED_BATCH_SIZE", "128")),
            parent_chunk_size=int(_env("PARENT_CHUNK_SIZE", "600")),
            parent_max_chunk_size=int(_env("PARENT_MAX_CHUNK_SIZE", "900")),
            child_chunk_size=int(_env("CHILD_CHUNK_SIZE", "220")),
            child_overlap=int(_env("CHILD_OVERLAP", "40")),
            max_parents=int(_env("MAX_PARENTS", "6")),
            query_expansion_enabled=_env("QUERY_EXPANSION_ENABLED", "1") == "1",
            expansion_multi_query=_env("EXPANSION_MULTI_QUERY", "1") == "1",
            expansion_hyde=_env("EXPANSION_HYDE", "1") == "1",
            expansion_multi_turn=_env("EXPANSION_MULTI_TURN", "1") == "1",
            rewrite_variants=int(_env("REWRITE_VARIANTS", "3")),
            parent_child_enabled=_env("PARENT_CHILD_ENABLED", "1") == "1",
            max_concurrency=int(_env("MAX_CONCURRENCY", "4")),
            llm_max_concurrency=int(_env("LLM_MAX_CONCURRENCY", "8")),
            tracing_enabled=_env("TRACING_ENABLED", "1") == "1",
            advanced_tools_enabled=_env("ADVANCED_TOOLS_ENABLED", "1") == "1",
            agent_subagents_enabled=_env("AGENT_SUBAGENTS_ENABLED", "1") == "1",
            tool_workspace=_env("TOOL_WORKSPACE", ""),
            command_allowlist=_env("COMMAND_ALLOWLIST", ""),
            command_timeout=int(_env("COMMAND_TIMEOUT", "60")),
            command_sandbox=_env("COMMAND_SANDBOX", "subprocess"),
            sandbox_image=_env("SANDBOX_IMAGE", "python:3.11-slim"),
            sandbox_workspace_readonly=_env("SANDBOX_WORKSPACE_READONLY", "0") == "1",
            tool_permission_mode=_env("TOOL_PERMISSION_MODE", "ask"),
            permission_timeout=int(_env("PERMISSION_TIMEOUT", "300")),
            skill_sandbox_enabled=_env("SKILL_SANDBOX_ENABLED", "0") == "1",
            reasoning_summary_enabled=_env("REASONING_SUMMARY_ENABLED", "1") == "1",
            project_memory_file=_env("PROJECT_MEMORY_FILE", ""),
            mcp_enabled=_env("MCP_ENABLED", "1") == "1",
            checkpoint_enabled=_env("CHECKPOINT_ENABLED", "1") == "1",
            trajectory_compress_enabled=_env("TRAJECTORY_COMPRESS_ENABLED", "1") == "1",
            cors_origins=origins,
        )

    def ensure_dirs(self) -> None:
        """确保数据目录与本地账本目录存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """全局单例：整个应用共享同一份配置。"""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
