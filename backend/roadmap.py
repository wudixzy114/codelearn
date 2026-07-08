"""学习路线图生成：agent 自主探索真实源码 → LLM 输出有序步骤。

旧版只把目录结构图 + 文档摘录单次喂给模型（从不读源码）。现改为 ReAct 循环：
以结构图+文档为起点地图，让模型用 list_dir/read_file/search 主动读关键文件后再规划，
使路线图有真实代码依据。生成过程以事件流 yield，供上层做 SSE 进度展示。
"""
from __future__ import annotations

import json
from typing import Dict, Iterator, List

from . import agent_loop, cache, llm_client, prompts, repo_scanner
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


def _finalize(data: Dict, repo_map: str) -> Dict:
    """校验/补全 agent 产出的路线图，落盘缓存。"""
    if not isinstance(data, dict) or "steps" not in data:
        raise llm_client.LLMError("路线图返回结构异常")

    data["steps"] = _validate_files(data.get("steps") or [])
    data["repo_name"] = settings.target_repo.name
    data["language"] = settings.language
    data["repo_sig"] = _repo_sig(repo_map)
    data["cached"] = False

    cache.put_roadmap(data)
    return data


def _repo_sig(repo_map: str) -> str:
    """repo 结构签名：结构变化 → 签名变化 → 缓存失效。"""
    return cache.content_hash(repo_map, settings.language, "agent-v1")


def _cache_is_fresh(cached: Dict, repo_map: str) -> bool:
    """已缓存路线图是否仍与当前结构匹配。旧缓存无 repo_sig 视为陈旧。"""
    return bool(cached) and cached.get("repo_sig") == _repo_sig(repo_map)


# 单条 finding 摘录上限 + 总摘录上限（控制收尾调用的 token）
_FINDING_CHAR_CAP = 3000
_FINDINGS_TOTAL_CAP = 24000


def _make_finalizer(repo_map: str):
    """构造收尾回调：在全新、无工具的上下文里，把探索所得源码交给模型产出路线图。

    这样可避免模型被 ReAct 阶段自己的 action 历史带偏而持续调用工具、不肯收尾。
    """
    def finalize(findings: List[str], model: str = None) -> Dict:
        clipped = []
        total = 0
        for f in findings:
            piece = f[:_FINDING_CHAR_CAP]
            if total + len(piece) > _FINDINGS_TOTAL_CAP:
                break
            clipped.append(piece)
            total += len(piece)
        findings_text = "\n\n".join(clipped) or "（未成功读取到源码，请仅依据结构图规划。）"
        return llm_client.chat_json(
            prompts.roadmap_system(settings.language),
            prompts.roadmap_finalize_user(
                settings.language,
                repo_name=settings.target_repo.name,
                repo_map=repo_map,
                findings=findings_text,
            ),
            max_tokens=6000,
            model=model,
        )
    return finalize


def generate(force: bool = False) -> Dict:
    """生成或读取缓存的学习路线图（非流式；跑完 agent 循环直接返回）。"""
    repo_map = _build_repo_map_text()
    if not force:
        cached = cache.get_roadmap()
        if cached is not None and _cache_is_fresh(cached, repo_map):
            cached["cached"] = True
            return cached

    docs = _build_docs_text()
    data = agent_loop.run_to_result(
        prompts.roadmap_agent_system(settings.language),
        prompts.roadmap_agent_seed(
            settings.language,
            repo_name=settings.target_repo.name,
            repo_map=repo_map,
            docs=docs,
        ),
        finalize=_make_finalizer(repo_map),
    )
    if data is None:
        raise llm_client.LLMError("路线图生成失败：agent 未产出结果")
    return _finalize(data, repo_map)


def generate_stream(force: bool = False) -> Iterator[str]:
    """流式生成路线图：yield 文本片段，供 main._sse 包成 event-stream。

    进度事件 → `[[progress]] 描述`；最终结果 → `[[result]] <JSON>`；
    错误 → `[ERROR] ...`。前端据前缀分派。
    """
    repo_map = _build_repo_map_text()
    if not force:
        cached = cache.get_roadmap()
        if cached is not None and _cache_is_fresh(cached, repo_map):
            cached["cached"] = True
            yield "[[result]] " + json.dumps(cached, ensure_ascii=False)
            return

    docs = _build_docs_text()
    result_data = None
    for ev in agent_loop.run(
        prompts.roadmap_agent_system(settings.language),
        prompts.roadmap_agent_seed(
            settings.language,
            repo_name=settings.target_repo.name,
            repo_map=repo_map,
            docs=docs,
        ),
        finalize=_make_finalizer(repo_map),
    ):
        if ev["type"] == "progress":
            yield "[[progress]] " + ev["msg"]
        elif ev["type"] == "error":
            yield "[ERROR] " + ev["msg"]
            return
        elif ev["type"] == "result":
            result_data = ev["data"]

    if result_data is None:
        yield "[ERROR] 路线图生成失败：agent 未产出结果"
        return
    try:
        final = _finalize(result_data, repo_map)
    except llm_client.LLMError as e:
        yield "[ERROR] " + str(e)
        return
    yield "[[result]] " + json.dumps(final, ensure_ascii=False)


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
        "repo_sig": _repo_sig(_build_repo_map_text()),
        "edited": True,
        "cached": False,
    }
    cache.put_roadmap(data)
    return data
