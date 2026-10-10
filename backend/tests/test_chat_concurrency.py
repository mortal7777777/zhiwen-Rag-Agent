"""legacy /api/chat 的并发口径（P2-3）：GPU 许可只覆盖检索段。

背景：旧实现由 API 层 `service.acquire()` 把 GPU 许可握到**生成结束**，一个
/api/chat 生成会占住并发名额、拖慢其他会话的检索；Agent 路径则只在检索段
持锁。现在两条路径统一：`RAGService.retrieve_locked()` 自持自放，生成段
改由 `llm_max_concurrency` 约束。
"""

from __future__ import annotations

import threading
import time

from app.api.chat import chat
from app.config import Settings
from app.rag.service import RAGService
from app.schemas import ChatRequest


class _BlockingChat:
    """假生成模型：进入 generate 后阻塞，直到测试放行。"""

    def __init__(self, entered: threading.Event, release: threading.Event):
        self.entered = entered
        self.release = release
        self.llm_permit_free = None

    def generate(self, question, context, history=None):
        self.entered.set()
        self.release.wait(5)
        return "答案"


def _service(semaphore_size: int = 1) -> RAGService:
    svc = RAGService(Settings())
    svc._semaphore = threading.BoundedSemaphore(semaphore_size)
    svc._llm_semaphore = threading.BoundedSemaphore(1)
    svc.retrieve = lambda question, history=None: []  # 检索很快，不碰磁盘
    return svc


def test_gpu_lock_released_before_generation():
    """生成阶段必须已经放开 GPU 许可（否则并发检索会被拖住）。"""
    entered, release = threading.Event(), threading.Event()
    svc = _service()
    svc._chat = _BlockingChat(entered, release)

    result: dict = {}

    def _call():
        try:
            result["response"] = chat(ChatRequest(question="测试问题"), svc)
        except Exception as exc:  # pragma: no cover - 失败时给出原因
            result["error"] = exc

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    assert entered.wait(5), "生成阶段未开始"

    # 生成进行中：GPU 许可应当可以立刻拿到（修复前这里会阻塞到生成结束）
    got = svc._semaphore.acquire(timeout=2)
    if got:
        svc._semaphore.release()
    release.set()
    worker.join(5)

    assert got, "生成阶段仍占着 GPU 许可：其他会话的检索会被阻塞"
    assert "error" not in result, result.get("error")
    assert result["response"].answer == "答案"


def test_retrieve_locked_holds_lock_only_inside_retrieve():
    """检索段内必须持锁；返回后必须已释放。"""
    inside = threading.Event()
    release = threading.Event()
    svc = _service()

    def _slow_retrieve(question, history=None):
        inside.set()
        release.wait(5)
        return []

    svc.retrieve = _slow_retrieve
    worker = threading.Thread(target=lambda: svc.retrieve_locked("q"), daemon=True)
    worker.start()
    assert inside.wait(5)

    # 检索进行中：许可被占（拿不到）
    assert svc._semaphore.acquire(timeout=0.3) is False, "检索段应持有 GPU 许可"
    release.set()
    worker.join(5)

    # 检索结束：许可已自动释放
    assert svc._semaphore.acquire(timeout=2) is True, "检索结束后应释放 GPU 许可"
    svc._semaphore.release()


def test_generation_is_bounded_by_llm_semaphore():
    """生成段由 LLM 并发上限约束（旧路径去掉 GPU 锁后的兜底）。"""
    svc = _service()
    observed: list[bool] = []

    class _ProbeChat:
        def generate(self, question, context, history=None):
            # 服务应该已经持有 LLM 许可：这里再拿应当失败
            got = svc._llm_semaphore.acquire(timeout=0.2)
            observed.append(got)
            if got:
                svc._llm_semaphore.release()
            return "ok"

    svc._chat = _ProbeChat()
    svc.ask("q")
    assert observed == [False], "生成段应持有 LLM 许可"
    # 生成结束后许可必须归还
    assert svc._llm_semaphore.acquire(timeout=1) is True
    svc._llm_semaphore.release()


def test_stream_endpoint_no_longer_uses_legacy_permit():
    """静态契约：api/chat.py 不应再调用已废弃的 acquire()/release()。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "chat.py"
    ).read_text(encoding="utf-8")
    assert "service.acquire()" not in src
    assert "service.release()" not in src
    assert "retrieve_locked" in src
