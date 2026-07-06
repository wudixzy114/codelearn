"""学习路线图生成：构建紧凑 repo map + 文档种子 → LLM 输出有序步骤。"""
from __future__ import annotations

import json
from typing import Dict, List

from . import cache, llm_client, prompts, repo_scanner
from .config import settings


def _build_repo_map_text() -> str:
    """把结构化 repo map 压成紧凑文本，控制 token。"""
    entries = repo_scanner.top_level_map(max_depth=2, per_dir_files=10)
    lines: List[str] = []
    for e in entries:
        indent = "  " * e["depth"]
        langs = ", ".join(f"{k}:{v}" for k, v in sorted(e["languages"].items()))
        head = f"{indent}{e['path']}/  [{e['total_code_files']} 代码文件"
        if langs:
            head += f"; {langs}"
        head += "]"
        if e["has_cmake"]:
            head += " (CMakeLists)"
        lines.append(head)
        if e["entry_points"]:
            lines.append(f"{indent}  入口: {', '.join(e['entry_points'])}")
        if e["sample_files"]:
            lines.append(f"{indent}  文件: {', '.join(e['sample_files'])}")
    return "\n".join(lines)


def _build_docs_text() -> str:
    docs = repo_scanner.discover_docs()
    if not docs:
        return "（未发现现有文档，请仅依据结构图与依赖关系规划。）"
    parts: List[str] = []
    for name, excerpt in docs.items():
        parts.append(f"----- {name} -----\n{excerpt}")
    return "\n\n".join(parts)


def _validate_files(steps: List[Dict]) -> List[Dict]:
    """校验每步的文件是否真实存在，剔除幻觉路径，并标注存在性。"""
    for step in steps:
        files = step.get("files") or []
        checked: List[Dict] = []
        for f in files:
            if not isinstance(f, str):
                continue
            f = f.strip()
            try:
                p = repo_scanner.resolve(f)
                exists = p.exists()
            except repo_scanner.PathError:
                exists = False
            if exists:
                checked.append({
                    "path": f,
                    "is_dir": repo_scanner.resolve(f).is_dir(),
                })
        step["files"] = checked
    return steps


def generate(force: bool = False) -> Dict:
    """生成或读取缓存的学习路线图。"""
    if not force:
        cached = cache.get_roadmap()
        if cached is not None:
            cached["cached"] = True
            return cached

    repo_map = _build_repo_map_text()
    docs = _build_docs_text()

    data = llm_client.chat_json(
        prompts.roadmap_system(settings.language),
        prompts.roadmap_user(
            settings.language,
            repo_name=settings.target_repo.name,
            repo_map=repo_map,
            docs=docs,
        ),
        max_tokens=6000,
    )

    if not isinstance(data, dict) or "steps" not in data:
        raise llm_client.LLMError("路线图返回结构异常")

    data["steps"] = _validate_files(data.get("steps") or [])
    data["repo_name"] = settings.target_repo.name
    data["language"] = settings.language
    data["cached"] = False

    cache.put_roadmap(data)
    return data


def save_edited(edited: Dict) -> Dict:
    """保存用户微调后的路线图（重排/删除步骤或文件、改标题），不调用 LLM。

    仅做结构规整与文件存在性校验，然后落盘覆盖缓存。
    """
    steps_in = edited.get("steps") or []
    steps: List[Dict] = []
    for s in steps_in:
        if not isinstance(s, dict):
            continue
        # 文件项统一成字符串路径喂给 _validate_files
        raw_files = s.get("files") or []
        norm_files = []
        for f in raw_files:
            if isinstance(f, str):
                norm_files.append(f)
            elif isinstance(f, dict) and f.get("path"):
                norm_files.append(f["path"])
        steps.append({
            "title": (s.get("title") or "").strip(),
            "goal": (s.get("goal") or "").strip(),
            "description": (s.get("description") or "").strip(),
            "files": norm_files,
        })

    data = {
        "title": (edited.get("title") or "").strip(),
        "summary": (edited.get("summary") or "").strip(),
        "steps": _validate_files(steps),
        "repo_name": settings.target_repo.name,
        "language": settings.language,
        "edited": True,
        "cached": False,
    }
    cache.put_roadmap(data)
    return data
