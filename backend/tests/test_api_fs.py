"""目录浏览接口：只列目录 + 仅本机限定（直接调端点函数，不经 HTTP）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.fs import browse_dirs


def _req(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_browse_requires_localhost(tmp_path):
    (tmp_path / "sub").mkdir()
    with pytest.raises(HTTPException) as exc:
        browse_dirs(_req("10.0.0.5"), str(tmp_path))
    assert exc.value.status_code == 403


def test_browse_lists_only_dirs(tmp_path):
    (tmp_path / "sub_a").mkdir()
    (tmp_path / "sub_b").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    res = browse_dirs(_req("127.0.0.1"), str(tmp_path))

    names = [Path(p).name for p in res["dirs"]]
    assert names == ["sub_a", "sub_b"]
    assert res["path"] == str(tmp_path.resolve())
    assert res["parent"]
    assert res["truncated"] is False


def test_browse_missing_dir_404(tmp_path):
    with pytest.raises(HTTPException) as exc:
        browse_dirs(_req("::1"), str(tmp_path / "nope"))
    assert exc.value.status_code == 404


def test_browse_empty_lists_roots():
    res = browse_dirs(_req("127.0.0.1"), "")
    assert res["dirs"], "至少应返回盘符 / 根目录"
