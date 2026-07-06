"""文件夹级递归学习：目录概览 + 各文件一行摘要 + 建议阅读顺序。

为控制成本：子文件摘要优先复用单文件讲解缓存里的 role/overview；
只对尚无缓存的文件做“轻量一行摘要”生成（可并发）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from . import annotator, cache, llm_client, prompts, repo_scanner
from .config import settings

# 单个文件夹里最多为多少文件生成一行摘要（超出的只列路径，避免爆量）
_MAX_SUMMARIES = 40


def _summarize_children(code_files: List[Dict]) -> List[Dict]:
    """并发为目录下代码文件生成一行摘要。"""
    targets = code_files[:_MAX_SUMMARIES]

    def one(f: Dict) -> Dict:
        summary = ""
        try:
            summary = annotator.quick_summary(f["path"])
        except Exception:
            summary = ""
        return {"path": f["path"], "name": f["name"], "summary": summary}

    results: List[Dict] = []
    if targets:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(one, targets))

    # 超出上限的文件只列路径
    for f in code_files[_MAX_SUMMARIES:]:
        results.append({"path": f["path"], "name": f["name"], "summary": ""})
    return results


def learn_folder(rel: str, *, force: bool = False) -> Dict:
    """生成或读取缓存的文件夹学习结果。"""
    listing = repo_scanner.list_dir(rel)
    code_files = [f for f in listing["files"] if f["code"]]
    subdirs = listing["dirs"]

    # 缓存 key：目录路径 + 直接子代码文件名集合（结构变则失效）
    sig = "|".join(sorted(f["path"] for f in code_files))
    key = cache.content_hash(rel or ".", sig, settings.language, "v1")
    if not force:
        cached = cache.get("folders", key)
        if cached is not None:
            cached["cached"] = True
            return cached

    child_summaries = _summarize_children(code_files)

    file_list_text = "\n".join(
        f"{c['path']} : {c['summary'] or '（无摘要）'}" for c in child_summaries
    ) or "（本目录无直接代码文件）"
    subdir_text = "\n".join(d["path"] for d in subdirs) or "（无子目录）"

    overview = ""
    suggested_order: List[str] = []
    notes = ""
    try:
        data = llm_client.chat_json(
            prompts.folder_system(settings.language),
            prompts.folder_user(
                settings.language,
                path=rel or settings.target_repo.name,
                n_files=len(code_files),
                n_subdirs=len(subdirs),
                file_list=file_list_text,
                subdir_list=subdir_text,
            ),
            max_tokens=2000,
        )
        if isinstance(data, dict):
            overview = (data.get("overview") or "").strip()
            notes = (data.get("notes") or "").strip()
            order = data.get("suggested_order") or []
            valid = {c["path"] for c in child_summaries}
            suggested_order = [p for p in order if isinstance(p, str) and p in valid]
    except llm_client.LLMError:
        overview = ""

    result = {
        "path": rel,
        "overview": overview,
        "notes": notes,
        "files": child_summaries,
        "subdirs": subdirs,
        "suggested_order": suggested_order,
        "cached": False,
    }
    cache.put("folders", key, result)
    return result
