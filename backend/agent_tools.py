"""Agent 工具集：供 ReAct 循环调用的只读探索工具。

全部包装 repo_scanner 的安全原语（路径越界校验在 resolve 内完成），
每个工具返回**字符串观测**，直接作为 message 喂回模型。
工具执行永不抛异常——失败也返回一段可读的错误文本，让 agent 自行纠偏。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import repo_scanner
from .config import settings

# 单次 read_file 返回的字符上限（约 4000 字符，控制单轮 token）
READ_CHAR_CAP = 4000
# search 扫描的文件数上限与命中条数上限
SEARCH_MAX_FILES = 2000
SEARCH_MAX_HITS = 30
# 单个文件参与 search 的字节上限（跳过超大文件）
SEARCH_MAX_FILE_BYTES = 512 * 1024


def list_dir(path: str = "") -> str:
    """列出目录直接子项（不递归）。path 为相对目标库根的路径，空串为根。"""
    try:
        listing = repo_scanner.list_dir(path or "")
    except repo_scanner.PathError as e:
        return f"[错误] {e}"
    lines = [f"目录 {path or '.'} 的内容："]
    for d in listing["dirs"]:
        lines.append(f"  {d['path']}/")
    for f in listing["files"]:
        tag = "" if f["code"] else ("  (二进制)" if f["binary"] else "  (非源码)")
        lines.append(f"  {f['path']}{tag}")
    if not listing["dirs"] and not listing["files"]:
        lines.append("  （空目录）")
    return "\n".join(lines)


def read_file(path: str, start: Optional[int] = None, end: Optional[int] = None) -> str:
    """读取文件文本，可选 [start, end] 行区间（1 基、闭区间）。超长截断。"""
    try:
        text, language = repo_scanner.read_file(path)
    except repo_scanner.PathError as e:
        return f"[错误] {e}"

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    if start is not None or end is not None:
        s = max(1, int(start or 1))
        e = min(total, int(end or total))
        if e < s:
            s, e = e, s
        selected = lines[s - 1 : e]
        header = f"文件 {path}（{language}），第 {s}-{e} 行 / 共 {total} 行："
    else:
        selected = lines
        s = 1
        header = f"文件 {path}（{language}），共 {total} 行："

    body = "\n".join(f"{s + i}\t{ln}" for i, ln in enumerate(selected))
    if len(body) > READ_CHAR_CAP:
        body = body[:READ_CHAR_CAP] + f"\n…（已截断，超过 {READ_CHAR_CAP} 字符；可用 start/end 指定更小区间）"
    return header + "\n" + body


def search(query: str) -> str:
    """在目标库源码里做朴素文本搜索，返回 `path:line: 内容` 命中列表。

    仅扫描源码文件，跳过 _SKIP_DIRS；受扫描文件数与命中条数上限保护。
    """
    q = (query or "").strip()
    if not q:
        return "[错误] 搜索词为空"

    root = settings.target_repo
    hits = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in repo_scanner._SKIP_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            if not repo_scanner.is_code_file(fn):
                continue
            if scanned >= SEARCH_MAX_FILES or len(hits) >= SEARCH_MAX_HITS:
                break
            abs_path = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(abs_path) > SEARCH_MAX_FILE_BYTES:
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    scanned += 1
                    for lineno, line in enumerate(fh, 1):
                        if q in line:
                            rel = repo_scanner.to_rel(Path(abs_path))
                            hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                            if len(hits) >= SEARCH_MAX_HITS:
                                break
            except (OSError, ValueError):
                continue
        if scanned >= SEARCH_MAX_FILES or len(hits) >= SEARCH_MAX_HITS:
            break

    if not hits:
        return f"搜索 “{q}”：无命中（已扫描 {scanned} 个源码文件）。"
    head = f"搜索 “{q}”：命中 {len(hits)} 条"
    if len(hits) >= SEARCH_MAX_HITS:
        head += "（已达上限，可能还有更多）"
    return head + "：\n" + "\n".join(hits)


# ---- 工具注册表：供 agent_loop 分发 --------------------------------------

def dispatch(action: str, action_input) -> str:
    """按 action 名分发到具体工具。action_input 可为 str 或 dict。"""
    if action == "list_dir":
        path = action_input if isinstance(action_input, str) else (action_input or {}).get("path", "")
        return list_dir(path or "")
    if action == "read_file":
        if isinstance(action_input, str):
            return read_file(action_input)
        ai = action_input or {}
        return read_file(ai.get("path", ""), ai.get("start"), ai.get("end"))
    if action == "search":
        query = action_input if isinstance(action_input, str) else (action_input or {}).get("query", "")
        return search(query or "")
    return f"[错误] 未知工具：{action}（可用：list_dir, read_file, search）"
