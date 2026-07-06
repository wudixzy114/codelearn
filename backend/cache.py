"""磁盘缓存：讲解结果按 (相对路径 + 内容哈希 + 语言) 缓存。

内容变化 → 哈希变化 → 自动失效重算。缓存目录见 config.settings.cache_dir。
布局：
    .cache/<repo>/files/<sha1>.json     单文件逐块讲解
    .cache/<repo>/folders/<sha1>.json   文件夹级学习
    .cache/<repo>/roadmap.json          路线图（整库级，单独管理）
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .config import settings


def content_hash(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _bucket(name: str) -> Path:
    d = settings.cache_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def get(bucket: str, key: str) -> Optional[Any]:
    fp = _bucket(bucket) / f"{key}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def put(bucket: str, key: str, value: Any) -> None:
    fp = _bucket(bucket) / f"{key}.json"
    fp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


# ---- 路线图（整库级，固定文件名，但按语言区分） -------------------------

def roadmap_path() -> Path:
    return settings.cache_dir / f"roadmap.{settings.language}.json"


def get_roadmap() -> Optional[Any]:
    fp = roadmap_path()
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def put_roadmap(value: Any) -> None:
    roadmap_path().write_text(
        json.dumps(value, ensure_ascii=False, indent=2), "utf-8"
    )
