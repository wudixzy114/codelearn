"""单文件逐块讲解：分块调用 LLM，合并并校验连续性。

关键不变量（供前端双栏对照）：blocks 必须从第 1 行连续覆盖到最后一行，
无空隙、无重叠。任何空隙自动补 kind="raw" 的无讲解块，重叠自动裁剪。
"""
from __future__ import annotations

from typing import Dict, List

from . import cache, llm_client, prompts, repo_scanner
from .config import settings


def _numbered(lines: List[str], start: int, end: int) -> str:
    """生成带绝对行号的代码片段（start/end 为 1 基、闭区间）。"""
    out = []
    for i in range(start, end + 1):
        out.append(f"{i}\t{lines[i - 1]}")
    return "\n".join(out)


def _windows(total: int) -> List[Dict[str, int]]:
    """把 [1, total] 切成若干带 overlap 的窗口（overlap 只作上下文，不重复产块）。"""
    step = max(1, settings.chunk_lines)
    overlap = max(0, settings.chunk_overlap)
    wins: List[Dict[str, int]] = []
    start = 1
    while start <= total:
        end = min(total, start + step - 1)
        ctx_start = max(1, start - overlap)
        wins.append({"start": start, "end": end, "ctx_start": ctx_start})
        start = end + 1
    return wins


def _sanitize_blocks(raw: List[Dict], total: int) -> List[Dict]:
    """排序、去重、修边界，保证从 1 到 total 连续覆盖。"""
    clean: List[Dict] = []
    for b in raw:
        try:
            s = int(b.get("start_line"))
            e = int(b.get("end_line"))
        except (TypeError, ValueError):
            continue
        if e < s:
            s, e = e, s
        s = max(1, min(s, total))
        e = max(1, min(e, total))
        # comment 为主讲解；detail 为选读再解释。兼容旧字段 note。
        comment = (b.get("comment") or b.get("note") or "").strip()
        detail = (b.get("detail") or "").strip()
        kind = (b.get("kind") or "other").strip() or "other"
        clean.append({
            "start_line": s, "end_line": e, "kind": kind,
            "comment": comment, "detail": detail,
        })

    clean.sort(key=lambda x: (x["start_line"], x["end_line"]))

    # 消除重叠 + 补空隙，重建为严格连续序列
    result: List[Dict] = []
    cursor = 1
    for b in clean:
        s, e = b["start_line"], b["end_line"]
        if e < cursor:
            continue  # 完全落在已覆盖区间之内，丢弃
        if s > cursor:
            # 空隙 → 补 raw 块
            result.append({
                "start_line": cursor, "end_line": s - 1,
                "kind": "raw", "comment": "", "detail": "",
            })
        # 裁掉与已覆盖区间重叠的部分
        s = max(s, cursor)
        b["start_line"] = s
        b["end_line"] = e
        result.append(b)
        cursor = e + 1

    if cursor <= total:
        result.append({
            "start_line": cursor, "end_line": total,
            "kind": "raw", "comment": "", "detail": "",
        })
    return result


def _generate_overview(rel: str, language: str, lines: List[str]) -> Dict:
    head = "\n".join(lines[:80])
    try:
        data = llm_client.chat_json(
            prompts.overview_system(settings.language),
            prompts.overview_user(
                settings.language,
                path=rel, language=language,
                total_lines=len(lines), head=head,
            ),
            max_tokens=512,
        )
        return {
            "overview": (data.get("overview") or "").strip(),
            "role": (data.get("role") or "").strip(),
        }
    except llm_client.LLMError:
        return {"overview": "", "role": ""}


def explain_file(rel: str, *, force: bool = False) -> Dict:
    """生成（或读缓存）单文件逐块讲解。"""
    text, language = repo_scanner.read_file(rel)
    lines = text.split("\n")
    # split 后若文件以换行结尾会多一个空串，去掉以对齐真实行数
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    key = cache.content_hash(rel, str(total), text, settings.language, "v2")
    if not force:
        cached = cache.get("files", key)
        if cached is not None:
            return cached

    if total == 0:
        result = {
            "path": rel, "language": language, "total_lines": 0,
            "overview": "空文件。", "role": "", "blocks": [], "cached": False,
        }
        cache.put("files", key, result)
        return result

    # 超大文件保护：截断到 max_file_lines
    truncated = False
    if total > settings.max_file_lines:
        total = settings.max_file_lines
        lines = lines[:total]
        truncated = True

    # 概览
    ov = _generate_overview(rel, language, lines)

    # 逐窗口产块
    raw_blocks: List[Dict] = []
    for win in _windows(total):
        code = _numbered(lines, win["ctx_start"], win["end"])
        try:
            data = llm_client.chat_json(
                prompts.annotate_system(settings.language),
                prompts.annotate_user(
                    settings.language,
                    path=rel, language=language,
                    start=win["start"], end=win["end"], code=code,
                ),
                max_tokens=4096,
            )
            blocks = data.get("blocks") if isinstance(data, dict) else None
            if isinstance(blocks, list):
                # 只保留落在本窗口 [start, end] 的块（overlap 区仅作上下文）
                for b in blocks:
                    try:
                        s = int(b.get("start_line"))
                    except (TypeError, ValueError):
                        continue
                    if win["start"] <= s <= win["end"]:
                        raw_blocks.append(b)
        except llm_client.LLMError:
            # 该窗口失败 → 留给 _sanitize 补 raw 块，不中断整体
            continue

    blocks = _sanitize_blocks(raw_blocks, total)

    result = {
        "path": rel,
        "language": language,
        "total_lines": total,
        "truncated": truncated,
        "overview": ov["overview"],
        "role": ov["role"],
        "blocks": blocks,
        "cached": False,
    }
    cache.put("files", key, result)
    return result


def is_explained(rel: str) -> bool:
    """该文件的逐块讲解是否已缓存就绪（不触发生成）。供预加载 UI。"""
    try:
        text, _ = repo_scanner.read_file(rel)
    except repo_scanner.PathError:
        return False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    key = cache.content_hash(rel, str(len(lines)), text, settings.language, "v2")
    cached = cache.get("files", key)
    return bool(cached and cached.get("blocks"))


def quick_summary(rel: str) -> str:
    """一行摘要：优先复用已缓存的 role/overview，否则轻量生成。供文件夹学习。"""
    try:
        text, language = repo_scanner.read_file(rel)
    except repo_scanner.PathError:
        return ""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    # 命中完整讲解缓存则直接用
    key = cache.content_hash(rel, str(total), text, settings.language, "v2")
    cached = cache.get("files", key)
    if cached and (cached.get("role") or cached.get("overview")):
        return (cached.get("role") or cached.get("overview") or "").strip()

    ov = _generate_overview(rel, language, lines)
    return (ov.get("role") or ov.get("overview") or "").strip()
