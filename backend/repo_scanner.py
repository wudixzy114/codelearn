"""目标库扫描：安全路径解析、目录树、语言识别、文件读取、紧凑 repo map。

所有对外暴露的路径都是相对目标库根的 posix 相对路径；内部转成绝对路径前
都会做越界校验，杜绝路径穿越（../../etc/passwd 之类）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import settings

# ---- 语言 / 文件类型 -----------------------------------------------------

_EXT_LANG = {
    ".c": "c", ".h": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".cu": "cpp", ".cuh": "cpp",
    ".hip": "cpp", ".py": "python", ".pyi": "python", ".rs": "rust",
    ".go": "go", ".java": "java", ".kt": "kotlin", ".js": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".sh": "bash", ".bash": "bash", ".rb": "ruby", ".php": "php",
    ".proto": "protobuf", ".cmake": "cmake", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".json": "json", ".md": "markdown", ".txt": "text",
    ".in": "text", ".cfg": "ini", ".ini": "ini",
}

# 可作为源码讲解的扩展
_CODE_EXTS = {
    ".c", ".h", ".hpp", ".hh", ".hxx", ".cc", ".cpp", ".cxx", ".cu", ".cuh",
    ".hip", ".py", ".pyi", ".rs", ".go", ".java", ".kt", ".js", ".jsx",
    ".ts", ".tsx", ".sh", ".bash", ".rb", ".php", ".proto", ".cmake",
}

_SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv",
    ".idea", ".vscode", "build", "dist", ".cache", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "third_party",
}

_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip",
    ".tar", ".gz", ".so", ".dylib", ".dll", ".o", ".a", ".bin", ".pt",
    ".pth", ".safetensors", ".woff", ".woff2", ".ttf", ".eot",
}


def lang_for(path: str) -> str:
    return _EXT_LANG.get(Path(path).suffix.lower(), "text")


def is_code_file(path: str) -> bool:
    return Path(path).suffix.lower() in _CODE_EXTS


def is_binary(path: str) -> bool:
    return Path(path).suffix.lower() in _BINARY_EXTS


# ---- 安全路径解析 --------------------------------------------------------

class PathError(ValueError):
    """相对路径非法或越界。"""


def resolve(rel: str) -> Path:
    """把相对目标库的路径解析为绝对路径，并确保不越界。"""
    root = settings.target_repo
    rel = (rel or "").strip().lstrip("/")
    abs_path = (root / rel).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError:
        raise PathError(f"路径越界：{rel}")
    return abs_path


def to_rel(abs_path: Path) -> str:
    return abs_path.resolve().relative_to(settings.target_repo).as_posix()


# ---- 目录树（懒加载，单层） ---------------------------------------------

def list_dir(rel: str = "") -> Dict:
    """列出某目录的直接子项（不递归），供前端文件树懒加载。"""
    base = resolve(rel)
    if not base.is_dir():
        raise PathError(f"不是目录：{rel}")
    dirs: List[Dict] = []
    files: List[Dict] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if entry.name in _SKIP_DIRS:
                continue
            dirs.append({"name": entry.name, "path": to_rel(entry), "type": "dir"})
        elif entry.is_file():
            files.append({
                "name": entry.name,
                "path": to_rel(entry),
                "type": "file",
                "language": lang_for(entry.name),
                "code": is_code_file(entry.name),
                "binary": is_binary(entry.name),
            })
    # 目录在前，文件在后
    return {"path": rel, "dirs": dirs, "files": files}


def read_file(rel: str) -> Tuple[str, str]:
    """读取文本文件，返回 (内容, 语言)。二进制/超大文件拒绝。"""
    abs_path = resolve(rel)
    if not abs_path.is_file():
        raise PathError(f"不是文件：{rel}")
    if is_binary(rel):
        raise PathError(f"二进制文件不支持讲解：{rel}")
    data = abs_path.read_bytes()
    if b"\x00" in data[:4096]:
        raise PathError(f"疑似二进制文件：{rel}")
    text = data.decode("utf-8", errors="replace")
    return text, lang_for(rel)


# ---- 目录聚合统计（供 repo map / 文件夹学习） ---------------------------

def count_recursive(rel: str = "") -> Dict[str, int]:
    """递归统计某目录下代码文件数量（按语言）。"""
    base = resolve(rel)
    counts: Dict[str, int] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            if is_code_file(fn):
                counts[lang_for(fn)] = counts.get(lang_for(fn), 0) + 1
                total += 1
    counts["_total"] = total
    return counts


# ---- 紧凑 repo map（喂给路线图生成器） -----------------------------------

_ENTRY_HINTS = (
    "main.cpp", "main.py", "main.rs", "main.go", "main.c",
    "app.py", "__main__.py", "index.js", "index.ts", "server.py",
    "launch_server.py", "cli.py", "manage.py",
)


def _head(rel: str, max_chars: int) -> str:
    try:
        text, _ = read_file(rel)
    except PathError:
        return ""
    return text[:max_chars]


def discover_docs() -> Dict[str, str]:
    """探测常见文档，返回 {相对路径: 内容摘录}。有则作为路线图种子。"""
    root = settings.target_repo
    docs: Dict[str, str] = {}

    # 顶层 README / AGENTS / CLAUDE
    for name in ("README.md", "README_zh.md", "AGENTS.md", "CLAUDE.md",
                 "readme.md", "README.rst"):
        p = root / name
        if p.is_file():
            docs[name] = _head(name, 3000)

    # mkdocs 导航（本身即学习顺序）
    for name in ("mkdocs_en.yml", "mkdocs.yml", "mkdocs_zh.yml"):
        p = root / name
        if p.is_file():
            docs[name] = _head(name, 4000)

    # 架构类文档
    for cand in ("docs/en/dev_guide/code_arch.md",
                 "docs/dev_guide/code_arch.md",
                 "docs/architecture.md",
                 "docs/en/features/overview.md",
                 "ARCHITECTURE.md"):
        p = root / cand
        if p.is_file():
            docs[cand] = _head(cand, 4000)

    return docs


def top_level_map(max_depth: int = 2, per_dir_files: int = 12) -> List[Dict]:
    """构建分层目录概览：每个目录的文件数、代表性文件、是否有 CMakeLists。

    只下探 max_depth 层，避免 repo map 过大。
    """
    root = settings.target_repo
    result: List[Dict] = []

    def walk(cur: Path, depth: int):
        rel = to_rel(cur) if cur != root else ""
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        sub_dirs = [
            e for e in entries
            if e.is_dir() and e.name not in _SKIP_DIRS
            and not e.name.startswith(".")
        ]
        code_files = [e.name for e in entries if e.is_file() and is_code_file(e.name)]
        entry_files = [n for n in code_files if n.lower() in _ENTRY_HINTS]
        has_cmake = (cur / "CMakeLists.txt").is_file()
        counts = count_recursive(rel)

        result.append({
            "path": rel or ".",
            "depth": depth,
            "total_code_files": counts.get("_total", 0),
            "languages": {k: v for k, v in counts.items() if k != "_total"},
            "sample_files": code_files[:per_dir_files],
            "entry_points": entry_files,
            "has_cmake": has_cmake,
            "subdirs": [e.name for e in sub_dirs],
        })
        if depth < max_depth:
            for sd in sub_dirs:
                walk(sd, depth + 1)

    walk(root, 0)
    return result
