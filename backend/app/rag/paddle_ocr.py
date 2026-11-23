"""PP-StructureV3 Worker 客户端 + 页级缓存 + PDF 路由画像。

缓存键：文件指纹（大小+修改时间）+ 页码 + DPI + 管线版本。
缓存目录：{meta_dir}/ocr_cache/{版本}/{文件指纹}/p0001_d200.json + 图片块。
第二次重建索引时零 Worker 调用；修改文件后指纹变化自动重算。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..runtime_config import effective_or

logger = logging.getLogger(__name__)

CACHE_VERSION = "paddle-structure-v2-2026-11-18"
VLM_CACHE_VERSION = "vlm-page-ocr-v1-2026-11-18"
HEALTH_TTL_S = 20.0
# 启动失败后的冷却：重建时每个文件都会尝试懒启动，不冷却会反复等满
# start_timeout（paddle 环境坏掉时 N 个文件 = N×120s）
START_FAIL_COOLDOWN_S = 60.0


@dataclass
class PageOCR:
    page: int
    markdown: str
    text_chars: int
    native_chars: int
    images: list[dict] = field(default_factory=list)
    ocr_conf: float | None = None
    image_ocr_chars: int = 0
    elapsed_s: float = 0.0
    from_cache: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.markdown.strip()) or bool(self.images)


def _file_key(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    raw = f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def profile_pdf(path: Path, sample_pages: int = 80) -> dict:
    """快速画像（抽样页）：稀疏页比例、平均字符、图片面积、课件版式。"""
    import pymupdf

    try:
        with pymupdf.open(str(path)) as doc:
            pages = doc.page_count
            if pages <= 0:
                return {"pages": 0, "sampled": 0}
            step = max(1, pages // max(1, sample_pages))
            indices = list(range(0, pages, step))[:sample_pages]
            chars: list[int] = []
            img_fracs: list[float] = []
            aspects: list[float] = []
            for i in indices:
                page = doc[i]
                chars.append(len(page.get_text().strip()))
                rect = page.rect
                area = max(1.0, rect.width * rect.height)
                img_area = 0.0
                for info in page.get_images(full=True):
                    try:
                        for r in page.get_image_rects(info[0], transform=False):
                            img_area += abs(r.width * r.height)
                    except Exception:
                        continue
                img_fracs.append(min(1.0, img_area / area))
                aspects.append(rect.width / rect.height if rect.height else 0.0)
    except Exception as exc:
        logger.warning("PDF 画像失败 %s：%s", path.name, exc)
        return {"pages": 0, "sampled": 0, "error": str(exc)[:200]}

    n = max(1, len(chars))
    return {
        "pages": pages,
        "sampled": len(chars),
        "avg_chars": sum(chars) / n,
        "sparse_ratio": sum(1 for c in chars if c < 30) / n,
        "image_area_ratio": sum(img_fracs) / n,
        "slide_ratio": sum(1 for a in aspects if 1.5 <= a <= 2.0) / n,
    }


def choose_pdf_backend(profile: dict, requested: str) -> str:
    """auto：扫描件/课件/图片密度高 → paddle；健康文本层书籍 → pymupdf。"""
    requested = (requested or "auto").strip().lower()
    if requested in ("pymupdf", "paddle"):
        return requested
    if profile.get("pages", 0) <= 0:
        return "pymupdf"
    if profile.get("sparse_ratio", 0.0) >= 0.05:
        return "paddle"
    if profile.get("slide_ratio", 0.0) >= 0.5:
        return "paddle"
    if profile.get("image_area_ratio", 0.0) >= 0.30:
        return "paddle"
    # 图片型课件：字符偏少且图片占比不低（判断/言语/申论都属于这一类）
    if (
        profile.get("avg_chars", 0.0) < 700
        and profile.get("image_area_ratio", 0.0) >= 0.04
    ):
        return "paddle"
    return "pymupdf"


class PaddleOCRClient:
    """Worker 的轻量 HTTP 客户端；Worker 未启动时 available()=False。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._healthy = False
        self._healthy_until = 0.0
        self._last_error = ""
        self._start_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._start_proc: subprocess.Popen | None = None
        self._health_data: dict = {}
        self._settings_checked = False
        self._auto_token = uuid.uuid4().hex
        self._spawned_with_auto_token = False

    @property
    def base_url(self) -> str:
        return str(
            effective_or(self.settings, "paddle_ocr_url", "http://127.0.0.1:8090")
        ).rstrip("/")

    @property
    def dpi(self) -> int:
        return int(effective_or(self.settings, "paddle_ocr_dpi", 200))

    @property
    def timeout(self) -> float:
        return float(effective_or(self.settings, "paddle_ocr_timeout", 120))

    @property
    def max_image_ocr(self) -> int:
        return int(effective_or(self.settings, "paddle_ocr_max_image_ocr", 6))

    @property
    def lazy_start(self) -> bool:
        return bool(effective_or(self.settings, "paddle_ocr_lazy_start", True))

    @property
    def idle_timeout_s(self) -> int:
        return int(effective_or(self.settings, "paddle_ocr_idle_timeout_s", 600))

    @property
    def pages_per_process(self) -> int:
        return int(
            effective_or(self.settings, "paddle_ocr_pages_per_process", 300)
        )

    @property
    def device(self) -> str:
        value = str(effective_or(self.settings, "paddle_ocr_device", "cpu")).lower()
        return value if value in ("cpu", "gpu") else "cpu"

    @property
    def token(self) -> str:
        """共享令牌：优先环境变量 PADDLE_OCR_TOKEN；懒启动子进程时自动注入实例级令牌。"""
        env_tok = os.environ.get("PADDLE_OCR_TOKEN", "").strip()
        if env_tok:
            return env_tok
        if self._spawned_with_auto_token:
            return self._auto_token
        return ""

    def _headers(self) -> dict[str, str]:
        return {"X-OCR-Token": self.token} if self.token else {}

    @property
    def start_timeout(self) -> float:
        return float(effective_or(self.settings, "paddle_ocr_timeout", 120))

    @property
    def last_error(self) -> str:
        return self._last_error

    def _probe_health(self) -> bool:
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                resp = client.get(f"{self.base_url}/health")
            data = resp.json() if resp.status_code == 200 else {}
            self._health_data = data if isinstance(data, dict) else {}
            self._healthy = bool(data.get("status") == "ok" and data.get("model_ready"))
            if self._healthy:
                self._last_error = ""
            else:
                self._last_error = f"worker 未就绪：{resp.status_code} {data}"
        except Exception as exc:
            self._healthy = False
            self._last_error = f"{type(exc).__name__}: {exc}"
        self._healthy_until = time.monotonic() + HEALTH_TTL_S
        return self._healthy

    def available(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now < self._healthy_until:
            return self._healthy
        return self._probe_health()

    def _spawn_worker(self) -> None:
        """按需拉起 Worker（独立进程；优先复用已有日志）。"""
        backend = Path(__file__).resolve().parents[2]
        script = backend / "scripts" / "dev" / "paddle_ocr_worker.py"
        if not script.exists():
            raise FileNotFoundError(f"Worker 脚本不存在：{script}")
        log_dir = backend / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
        env["PADDLE_OCR_DEVICE"] = self.device
        env["PADDLE_OCR_PORT"] = str(urlparse(self.base_url).port or 8090)
        env["PADDLE_OCR_IDLE_TIMEOUT_S"] = str(self.idle_timeout_s)
        env["PADDLE_OCR_PAGES_PER_PROCESS"] = str(self.pages_per_process)
        if not env.get("PADDLE_OCR_TOKEN", "").strip():
            env["PADDLE_OCR_TOKEN"] = self._auto_token
            self._spawned_with_auto_token = True
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        out = open(log_dir / "paddle_ocr_worker.log", "ab")
        err = open(log_dir / "paddle_ocr_worker.err.log", "ab")
        try:
            self._start_proc = subprocess.Popen(
                [sys.executable, "-u", str(script)],
                cwd=str(backend),
                env=env,
                stdout=out,
                stderr=err,
                creationflags=flags,
                close_fds=True,
            )
        finally:
            out.close()
            err.close()
        logger.info(
            "已按需启动 OCR Worker（pid=%s, device=%s, idle=%ss）",
            self._start_proc.pid,
            self.device,
            self.idle_timeout_s,
        )

    def ensure_started(self) -> bool:
        """Worker 不可用时按需拉起并等待就绪（懒启动）。

        启动失败（子进程退出 / 就绪超时）后进入 START_FAIL_COOLDOWN_S 冷却，
        避免重建期间每个文件都重新拉起并等满 start_timeout。
        """
        if self.available(force=True):
            return True
        if not self.lazy_start:
            return False
        with self._start_lock:
            if self.available(force=True):
                return True
            if time.monotonic() < self._cooldown_until:
                return False  # 近期启动失败，避免每个文件重复等待
            try:
                self._spawn_worker()
            except Exception as exc:
                self._last_error = f"启动 Worker 失败：{exc}"
                self._cooldown_until = time.monotonic() + START_FAIL_COOLDOWN_S
                logger.warning(self._last_error)
                return False
            proc = self._start_proc
            deadline = time.monotonic() + self.start_timeout
            failed = False
            while time.monotonic() < deadline:
                time.sleep(2.0)
                # 子进程秒退（缺少 paddle/import 错误）不必再等满超时
                if proc is not None and proc.poll() is not None:
                    self._last_error = (
                        f"OCR Worker 启动后退出（码 {proc.returncode}），"
                        "请查看 backend/logs/paddle_ocr_worker.err.log"
                    )
                    logger.warning(self._last_error)
                    failed = True
                    break
                if self.available(force=True):
                    return True
            if not failed:
                self._last_error = "OCR Worker 启动超时"
                logger.warning(self._last_error)
            self._cooldown_until = time.monotonic() + START_FAIL_COOLDOWN_S
            return False

    def _params_mismatch(self) -> bool:
        """运行中的 Worker 参数是否与设置页当前值不一致。"""
        data = self._health_data or {}
        try:
            idle = int(data.get("idle_timeout_s"))
            pages = int(data.get("max_pages_per_process"))
        except (TypeError, ValueError):
            return False
        device = str(data.get("device") or "").strip().lower()
        return (
            idle != self.idle_timeout_s
            or pages != self.pages_per_process
            or (bool(device) and device != self.device)
        )

    def _sync_worker_settings(self) -> None:
        """预启动的 Worker（start_all）用启动时环境变量：与设置不一致时重启一次。

        每个客户端实例只检查一次（设置保存会重建客户端）；仅懒启动模式生效，
        关闭懒启动时由用户完全掌管 Worker 进程。
        """
        if self._settings_checked or not self.lazy_start:
            return
        # Worker 还没起来时不标记"已检查"：等它就绪（可能由 start_all 预热）
        # 后再对齐，避免把自己刚拉起的实例误判成参数不一致
        if not self.available(force=True):
            return
        self._settings_checked = True
        if not self._params_mismatch():
            return
        data = self._health_data or {}
        logger.info(
            "OCR Worker 参数与设置不一致（device %s→%s，idle %s→%s，pages %s→%s），重启以应用设置",
            data.get("device"),
            self.device,
            data.get("idle_timeout_s"),
            self.idle_timeout_s,
            data.get("max_pages_per_process"),
            self.pages_per_process,
        )
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                resp = client.post(
                    f"{self.base_url}/shutdown", headers=self._headers()
                )
            if resp.status_code >= 400:
                logger.warning(
                    "Worker 拒绝 /shutdown（HTTP %s），继续使用当前实例",
                    resp.status_code,
                )
                return
        except Exception as exc:
            logger.warning("请求 OCR Worker 退出失败：%s", exc)
            return
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not self.available(force=True):
                return
        logger.warning("OCR Worker 未在 10s 内退出，继续使用当前实例")

    def _cache_dir(self, pdf_path: Path) -> Path:
        return (
            Path(self.settings.meta_dir)
            / "ocr_cache"
            / CACHE_VERSION
            / _file_key(pdf_path)
        )

    def cached_pages(self, pdf_path: Path) -> int:
        """已缓存的页数（Worker 不可用时仍可复用这些页）。"""
        try:
            return sum(1 for _ in self._cache_dir(Path(pdf_path)).glob("p*_d*.json"))
        except OSError:
            return 0

    def load_vlm_text(self, pdf_path: Path, page: int) -> str:
        path = self._cache_dir(pdf_path) / f"{VLM_CACHE_VERSION}_p{page:04d}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("text") or "")
        except Exception:
            return ""

    def save_vlm_text(self, pdf_path: Path, page: int, text: str) -> None:
        cache_dir = self._cache_dir(pdf_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"{VLM_CACHE_VERSION}_p{page:04d}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(target)

    def image_path(self, pdf_path: Path, image: dict) -> Path:
        return self._cache_dir(pdf_path) / str(image.get("file") or "")

    def image_rel_path(self, pdf_path: Path, image: dict) -> str:
        """图片相对 meta_dir 的路径（写入索引元数据，避免绝对路径）。"""
        path = self.image_path(pdf_path, image)
        try:
            return path.relative_to(Path(self.settings.meta_dir)).as_posix()
        except ValueError:
            return path.as_posix()

    def _load_cache(self, pdf_path: Path, page: int) -> PageOCR | None:
        cache_dir = self._cache_dir(pdf_path)
        cache_file = cache_dir / f"p{page:04d}_d{self.dpi}.json"
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("cache_version") != CACHE_VERSION:
                return None
            for img in data.get("images") or []:
                if not (cache_dir / img.get("file", "")).exists():
                    return None
            return PageOCR(
                page=page,
                markdown=data.get("markdown") or "",
                text_chars=int(data.get("text_chars") or 0),
                native_chars=int(data.get("native_chars") or 0),
                images=data.get("images") or [],
                ocr_conf=data.get("ocr_conf"),
                image_ocr_chars=int(data.get("image_ocr_chars") or 0),
                elapsed_s=float(data.get("elapsed_s") or 0.0),
                from_cache=True,
            )
        except Exception:
            return None

    def _save_cache(self, pdf_path: Path, result: PageOCR) -> None:
        cache_dir = self._cache_dir(pdf_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": CACHE_VERSION,
            "pipeline_version": CACHE_VERSION,
            "page": result.page,
            "dpi": self.dpi,
            "markdown": result.markdown,
            "text_chars": result.text_chars,
            "native_chars": result.native_chars,
            "images": result.images,
            "ocr_conf": result.ocr_conf,
            "image_ocr_chars": result.image_ocr_chars,
            "elapsed_s": result.elapsed_s,
        }
        target = cache_dir / f"p{result.page:04d}_d{self.dpi}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)

    def _post_page(self, payload: dict) -> dict | None:
        try:
            with httpx.Client(
                timeout=self.timeout, trust_env=False
            ) as client:
                resp = client.post(
                    f"{self.base_url}/ocr/page",
                    json=payload,
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                self._healthy = False
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(
                    "PaddleOCR 第 %s 页失败：%s",
                    payload.get("page"),
                    self._last_error,
                )
                return None
            return resp.json()
        except Exception as exc:
            self._healthy = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "PaddleOCR 第 %s 页请求失败：%s",
                payload.get("page"),
                self._last_error,
            )
            return None

    def ocr_page(self, pdf_path: Path, page: int, *, force: bool = False) -> PageOCR | None:
        """单页解析：缓存优先；Worker 崩溃时按需自动重启并重试一次。"""
        pdf_path = Path(pdf_path)
        if not force:
            cached = self._load_cache(pdf_path, page)
            if cached is not None:
                return cached
        self._sync_worker_settings()
        if not self.available() and not self.ensure_started():
            return None
        payload = {
            "pdf_path": str(pdf_path.resolve()),
            "page": int(page),
            "dpi": self.dpi,
            "image_dir": str(self._cache_dir(pdf_path)),
            "max_image_ocr": self.max_image_ocr,
            "ocr_images": True,
        }
        data = self._post_page(payload)
        if data is None and self.lazy_start:
            logger.info("Worker 请求失败，尝试自动重启并重试第 %d 页", page)
            if self.ensure_started():
                data = self._post_page(payload)
        if data is None:
            return None

        result = PageOCR(
            page=page,
            markdown=data.get("markdown") or "",
            text_chars=int(data.get("text_chars") or 0),
            native_chars=int(data.get("native_chars") or 0),
            images=data.get("images") or [],
            ocr_conf=data.get("ocr_conf"),
            image_ocr_chars=int(data.get("image_ocr_chars") or 0),
            elapsed_s=float(data.get("elapsed_s") or 0.0),
        )
        pipeline = str(data.get("pipeline_version") or "")
        if pipeline == CACHE_VERSION:
            self._save_cache(pdf_path, result)
        else:
            # Worker 升级但客户端 CACHE_VERSION 未同步（或旧 worker 还在跑）：
            # 本次结果仍可用，但不能写成新版本缓存，否则旧管线产物会被永久复用
            self._last_error = (
                f"Worker 管线版本不匹配（{pipeline or '未上报'} != {CACHE_VERSION}），"
                "本次不入页缓存"
            )
            logger.warning(self._last_error)
        return result
