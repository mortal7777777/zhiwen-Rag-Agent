"""数据访问层（按域拆分的包；本文件保持原 `db.repository` 导入路径与命名空间）。

- 各子模块是纯移动的领域实现，互不 import；
- 公共序列化/哈希辅助在 `common.py`；
- 对外仍可 `from app.db import repository as repo` 并访问全部原有函数。
"""

from __future__ import annotations

from .common import _json_dumps, _json_loads, content_hash
from .conversations import (
    add_message,
    clear_messages,
    create_conversation,
    delete_conversation,
    delete_messages_from,
    fork_conversation,
    get_conversation,
    get_message,
    get_messages_by_ids,
    get_summary_state,
    list_conversations,
    list_message_rows_up_to,
    list_messages,
    list_messages_with_id,
    load_history_messages,
    rename_conversation,
    reset_summary_if_stale,
    save_summary,
    search_conversations,
    update_conversation,
)
from .documents import (
    add_document_version,
    delete_document_meta,
    delete_document_version,
    delete_document_versions_all,
    get_document_version,
    list_document_meta,
    list_document_versions,
    rename_document_meta,
    rename_document_versions,
    upsert_document_meta,
    version_no_exists,
)
from .memories import (
    add_memory,
    bump_memory_hits,
    delete_memory,
    get_meta,
    list_all_memories,
    list_memories,
    set_meta,
    update_memory,
)
from .prompts import (
    archive_template,
    create_prompt_trial,
    create_template,
    delete_template,
    discard_prompt_fragment_draft,
    discard_template_draft,
    get_conversation_prompt_lock,
    get_prompt_fragment,
    get_prompt_fragment_version,
    get_prompt_trial,
    get_template,
    get_template_by_system_key,
    get_template_version,
    import_template_bundle,
    list_prompt_fragment_versions,
    list_prompt_fragments,
    list_prompt_trials,
    list_template_versions,
    list_templates,
    publish_prompt_fragment,
    publish_prompt_fragment_draft,
    publish_template_content,
    publish_template_draft,
    reset_prompt_fragment,
    restore_prompt_fragment_version,
    restore_template_version,
    save_prompt_fragment_draft,
    save_template_draft,
    seed_prompt_fragments,
    seed_templates,
    set_conversation_prompt_lock,
    update_template,
)
from .runs import create_agent_run, get_agent_run, list_agent_runs
from .agent_types import (
    create_agent_type,
    delete_agent_type,
    get_agent_type_row,
    list_agent_types,
    reset_agent_type,
    row_to_spec,
    seed_agent_types,
    update_agent_type,
)

__all__ = [name for name in globals() if not name.startswith("__")]
