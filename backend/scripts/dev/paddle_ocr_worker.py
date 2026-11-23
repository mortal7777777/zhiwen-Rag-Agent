"""PP-StructureV3 本地 OCR Worker（常驻 HTTP 服务）。

为什么独立进程：
- PaddlePaddle 的 CUDA/cuDNN 依赖与主后端 torch 环境在 Windows 上会冲突
  （paddle 先加载后 torch 的 shm.dll 报 WinError 127），因此 Worker 里
  必须先 import torch 再 import paddle/paddleocr；
- 模型加载慢（CPU 初始化约 20s），常驻进程只加载一次。

默认 CPU：RTX 4060 8GB 与后端 BGE 模型共存时 GPU 峰值会超过显存
（实测 PP-StructureV3 默认产线 7.9GB / 8.2GB），CPU 单页 5–13s 可接受。

启动：
  python scripts/dev/paddle_ocr_worker.py
环境变量：
  PADDLE_OCR_DEVICE=cpu|gpu   （默认 cpu）
  PADDLE_OCR_PORT=8090
  PADDLE_PDX_MODEL_SOURCE=bos|modelscope|huggingface （默认 bos）
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")

# Windows DLL 顺序：必须先 torch 再 paddle/paddleocr（见模块 docstring）。
import torch  # noqa: F401,E402

# 脚本以 backend 为 cwd 但 sys.path[0] 是脚本目录，显式补后端根以便复用 IR 工具
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import fitz  # noqa: E402  (PyMuPDF)
from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from PIL import Image  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.rag.ir import insert_image_markers  # noqa: E402

LOG = logging.getLogger("paddle_ocr_worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

DEVICE = os.environ.get("PADDLE_OCR_DEVICE", "cpu").strip().lower() or "cpu"
PORT = int(os.environ.get("PADDLE_OCR_PORT", "8090"))
IDLE_TIMEOUT_S = int(os.environ.get("PADDLE_OCR_IDLE_TIMEOUT_S", "600") or 0)
MAX_PAGES = int(os.environ.get("PADDLE_OCR_PAGES_PER_PROCESS", "300") or 0)
TOKEN = os.environ.get("PADDLE_OCR_TOKEN", "").strip()
PIPELINE_VERSION = "paddle-structure-v2-2026-11-18"


def _check_token(x_ocr_token: str | None = Header(default=None)) -> None:
    """可选共享令牌：设置 PADDLE_OCR_TOKEN 后拒绝未带头的本机其它进程调用。"""
    if TOKEN and (x_ocr_token or "") != TOKEN:
        raise HTTPException(status_code=401, detail="OCR token 无效")

_engine = None
_ocr = None
_activity_lock = threading.Lock()
_active_requests = 0
_last_ocr_at = time.monotonic()
_pages_since_start = 0
_rotate_started = False


def _ensure_models() -> None:
    """加载版面解析产线与通用 OCR（单图块文字识别）模型。"""
    global _engine, _ocr
    if _engine is None:
        from paddleocr import PPStructureV3

        LOG.info("初始化 PP-StructureV3（device=%s）", DEVICE)
        _engine = PPStructureV3(device=DEVICE)
    if _ocr is None:
        from paddleocr import PaddleOCR

        LOG.info("初始化 PaddleOCR（device=%s）", DEVICE)
        _ocr = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=DEVICE,
        )


app = FastAPI(title="ZhiWen Paddle OCR Worker")


class PageRequest(BaseModel):
    pdf_path: str
    page: int  # 1-based
    dpi: int = 200
    image_dir: str
    max_image_ocr: int = 6
    ocr_images: bool = True


def _result_json(res) -> dict:
    data = getattr(res, "json", None)
    if isinstance(data, dict):
        return data.get("res", data)
    try:
        data = dict(res)
    except Exception:
        return {}
    return data.get("res", data)


def _result_markdown(res, md_dir: Path) -> str:
    md = getattr(res, "markdown", None)
    if isinstance(md, str) and md.strip():
        return md
    md_dir.mkdir(parents=True, exist_ok=True)
    res.save_to_markdown(save_path=str(md_dir))
    files = sorted(md_dir.glob("*.md"))
    return files[0].read_text(encoding="utf-8", errors="replace") if files else ""


_IMG_MARK_RE = re.compile(r"\[\[IMG:[^\]]+\]\]")


def _ocr_image(path: Path) -> tuple[str, float | None]:
    """对图片块做通用 OCR（脑图/方格纸/截图里的文字）。"""
    try:
        out = list(_ocr.predict(str(path)))
    except Exception as exc:  # 单图失败不影响整页
        LOG.warning("图片 OCR 失败 %s: %s", path.name, exc)
        return "", None
    texts: list[str] = []
    scores: list[float] = []
    for res in out:
        data = _result_json(res)
        rec_texts = data.get("rec_texts") or []
        rec_scores = data.get("rec_scores") or []
        texts.extend(str(t) for t in rec_texts if str(t).strip())
        scores.extend(float(s) for s in rec_scores if isinstance(s, (int, float)))
    conf = (sum(scores) / len(scores)) if scores else None
    return "\n".join(texts), conf


@app.get("/health")
def health() -> dict:
    with _activity_lock:
        active = _active_requests
        idle = time.monotonic() - _last_ocr_at
    return {
        "status": "ok",
        "pipeline": "PP-StructureV3",
        "pipeline_version": PIPELINE_VERSION,
        "device": DEVICE,
        "model_ready": _engine is not None,
        "pid": os.getpid(),
        "idle_timeout_s": IDLE_TIMEOUT_S,
        "idle_seconds": round(idle, 1),
        "active_requests": active,
        "pages_since_start": _pages_since_start,
        "max_pages_per_process": MAX_PAGES,
    }


def _exit_when_idle(reason: str) -> None:
    """等在途页结束后退出（供客户端按新设置重启实例）。"""
    while True:
        time.sleep(0.2)
        with _activity_lock:
            active = _active_requests
        if active == 0:
            LOG.info("Worker 按请求退出（%s）", reason)
            os._exit(0)


@app.post("/shutdown", dependencies=[Depends(_check_token)])
def shutdown() -> dict:
    """客户端在设置变更后重启 Worker 用（仅本机调用）。"""
    threading.Thread(
        target=_exit_when_idle,
        args=("settings-sync",),
        name="ocr-shutdown",
        daemon=True,
    ).start()
    return {"ok": True}


def _rotate_after_idle() -> None:
    """达到单进程页数上限后，等在途页结束再退出，由客户端按需重启。"""
    while True:
        time.sleep(0.5)
        with _activity_lock:
            active = _active_requests
        if active == 0:
            LOG.info(
                "Worker 已处理 %d 页（上限 %d），主动轮换退出",
                _pages_since_start,
                MAX_PAGES,
            )
            os._exit(0)


@app.post("/ocr/page", dependencies=[Depends(_check_token)])
def ocr_page(req: PageRequest) -> dict:
    global _active_requests, _pages_since_start, _rotate_started
    with _activity_lock:
        _active_requests += 1
        _last_ocr_at = time.monotonic()
    try:
        return _ocr_page_impl(req)
    finally:
        rotate = False
        with _activity_lock:
            _active_requests = max(0, _active_requests - 1)
            _last_ocr_at = time.monotonic()
            _pages_since_start += 1
            if (
                MAX_PAGES > 0
                and _pages_since_start >= MAX_PAGES
                and not _rotate_started
            ):
                _rotate_started = True
                rotate = True
        if rotate:
            threading.Thread(
                target=_rotate_after_idle, name="ocr-rotate", daemon=True
            ).start()


def _ocr_page_impl(req: PageRequest) -> dict:
    pdf = Path(req.pdf_path)
    if not pdf.is_file():
        raise HTTPException(status_code=400, detail=f"PDF 不存在：{req.pdf_path}")
    out_dir = Path(req.image_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 并发请求各用各的临时文件/目录：按 glob 清理会误删其它在途页的渲染图
    suffix = uuid.uuid4().hex[:8]
    png = out_dir / f"_page_{req.page:04d}_{suffix}.png"
    md_dir = out_dir / f"_md_{req.page:04d}_{suffix}"

    t0 = time.perf_counter()
    try:
        with fitz.open(str(pdf)) as doc:
            if req.page < 1 or req.page > doc.page_count:
                raise HTTPException(status_code=400, detail=f"页码越界：{req.page}/{doc.page_count}")
            page = doc[req.page - 1]
            native_chars = len(page.get_text().strip())
            pix = page.get_pixmap(matrix=fitz.Matrix(req.dpi / 72, req.dpi / 72))
            pix.save(str(png))

        _ensure_models()
        results = list(_engine.predict(str(png)))
        if not results:
            raise HTTPException(status_code=500, detail="PP-StructureV3 无输出")
        res = results[0]
        data = _result_json(res)
        blocks = data.get("parsing_res_list") or []
        rec_scores = (data.get("overall_ocr_res") or {}).get("rec_scores") or []
        md = _result_markdown(res, md_dir)

        images: list[dict] = []
        slots: list[str | None] = []
        ocr_texts: dict[str, str] = {}
        with Image.open(png) as im:
            for idx, block in enumerate(blocks):
                if block.get("block_label") not in ("image", "figure", "chart"):
                    continue
                bbox = block.get("block_bbox") or []
                if len(bbox) != 4:
                    slots.append(None)
                    continue
                x0, y0, x1, y1 = (int(v) for v in bbox)
                w, h = x1 - x0, y1 - y0
                if w < 40 or h < 40:
                    slots.append(None)
                    continue
                fid = f"p{req.page:04d}_img{idx:02d}"
                crop_path = out_dir / f"{fid}.png"
                im.crop((x0, y0, x1, y1)).save(crop_path)
                ocr_text = ""
                ocr_conf = None
                if req.ocr_images and len(images) < max(0, req.max_image_ocr):
                    ocr_text, ocr_conf = _ocr_image(crop_path)
                images.append(
                    {
                        "id": fid,
                        "file": crop_path.name,
                        "bbox": [x0, y0, x1, y1],
                        "label": block.get("block_label"),
                        "ocr_text": ocr_text,
                        "ocr_conf": ocr_conf,
                        "width": w,
                        "height": h,
                    }
                )
                slots.append(fid)
                ocr_texts[fid] = ocr_text

        # 标记插回原图位置：一页多题时 image_refs 才会归属正确的 parent
        md, placed = insert_image_markers(md, slots, ocr_texts)
        for img in images:
            if img["id"] in placed:
                continue
            md += f"\n\n[[IMG:{img['id']}]]"
            extra = ocr_texts.get(img["id"]) or ""
            if extra:
                md += "\n" + extra

        plain = _IMG_MARK_RE.sub("", md)
        text_chars = len(re.sub(r"\s", "", plain))
        conf = None
        if rec_scores:
            vals = [float(s) for s in rec_scores if isinstance(s, (int, float))]
            conf = (sum(vals) / len(vals)) if vals else None

        return {
            "page": req.page,
            "dpi": req.dpi,
            "pipeline_version": PIPELINE_VERSION,
            "device": DEVICE,
            "native_chars": native_chars,
            "markdown": md.strip(),
            "text_chars": text_chars,
            "blocks": [
                {
                    "label": b.get("block_label"),
                    "bbox": b.get("block_bbox"),
                    "order": b.get("block_order"),
                }
                for b in blocks
            ],
            "images": [{k: v for k, v in img.items() if k != "ocr_text"} for img in images],
            "image_ocr_chars": sum(len(img["ocr_text"]) for img in images),
            "ocr_conf": conf,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        }
    finally:
        # 只清理本次请求自己的临时文件，避免并发请求互删
        try:
            png.unlink()
        except OSError:
            pass
        shutil.rmtree(md_dir, ignore_errors=True)


def _idle_monitor() -> None:
    """空闲超时后退出进程，让懒启动在下次解析时重新拉起。"""
    if IDLE_TIMEOUT_S <= 0:
        return
    interval = max(5, min(30, IDLE_TIMEOUT_S // 4 or 5))
    while True:
        time.sleep(interval)
        with _activity_lock:
            active = _active_requests
            idle = time.monotonic() - _last_ocr_at
        if active == 0 and idle >= IDLE_TIMEOUT_S:
            LOG.info("Worker 空闲 %.0f 秒（阈值 %ss），自动退出", idle, IDLE_TIMEOUT_S)
            os._exit(0)


def main() -> None:
    import uvicorn

    global _last_ocr_at

    LOG.info("预热模型（首次启动需下载/加载，约 20–60s）")
    _ensure_models()
    # 空闲计时从“模型就绪”开始，否则预热 40s > 短超时阈值会在启动瞬间退出
    with _activity_lock:
        _last_ocr_at = time.monotonic()
    if IDLE_TIMEOUT_S > 0:
        threading.Thread(
            target=_idle_monitor, name="ocr-idle-monitor", daemon=True
        ).start()
    LOG.info(
        "模型就绪，监听 127.0.0.1:%s（idle_timeout=%ss）", PORT, IDLE_TIMEOUT_S
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
