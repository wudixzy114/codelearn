"""FastAPI 应用：API 路由 + 静态前端。

启动：CODELEARN_TARGET=<repo> uvicorn backend.main:app
LLM 调用是同步阻塞的，放进线程池执行，避免卡住事件循环。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import annotator, folder_learn, llm_client, repo_scanner, roadmap
from .config import FRONTEND_DIR, settings

app = FastAPI(title="CodeLearn", version="0.1.0")


async def _run(fn, *args, **kwargs):
    """把阻塞函数丢到线程池执行。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---- 配置 / 健康 ---------------------------------------------------------

@app.get("/api/config")
async def get_config():
    return settings.as_public_dict()


class LangBody(BaseModel):
    language: str


@app.post("/api/config/language")
async def set_language(body: LangBody):
    settings.set_language(body.language)
    return settings.as_public_dict()


@app.get("/api/health")
async def health():
    result = await _run(llm_client.ping)
    return {
        "server": "ok",
        "target_repo": str(settings.target_repo),
        "target_exists": settings.target_repo.is_dir(),
        "llm": result,
    }


# ---- 文件树 --------------------------------------------------------------

@app.get("/api/tree")
async def tree(path: str = Query("")):
    try:
        return await _run(repo_scanner.list_dir, path)
    except repo_scanner.PathError as e:
        raise HTTPException(400, str(e))


# ---- 文件内容（原始 + 已缓存讲解） --------------------------------------

@app.get("/api/file")
async def file(path: str = Query(...)):
    try:
        text, language = await _run(repo_scanner.read_file, path)
    except repo_scanner.PathError as e:
        raise HTTPException(400, str(e))
    # 顺带附上已有缓存讲解（不触发生成）
    import hashlib

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    from . import cache as _cache

    key = _cache.content_hash(path, str(len(lines)), text, settings.language, "v2")
    explanation = _cache.get("files", key)
    return {
        "path": path,
        "language": language,
        "content": text,
        "total_lines": len(lines),
        "explanation": explanation,  # 可能为 None
    }


# ---- 单文件讲解（按需生成） ---------------------------------------------

class ExplainBody(BaseModel):
    path: str
    force: bool = False


@app.post("/api/explain")
async def explain(body: ExplainBody):
    try:
        return await _run(annotator.explain_file, body.path, force=body.force)
    except repo_scanner.PathError as e:
        raise HTTPException(400, str(e))
    except llm_client.LLMError as e:
        raise HTTPException(502, str(e))


# ---- 讲解就绪状态（供预加载 UI 显示徽标，不触发生成） -------------------

class StatusBody(BaseModel):
    paths: List[str] = []


@app.post("/api/explain/status")
async def explain_status(body: StatusBody):
    """批量查询哪些文件的讲解已缓存就绪，返回 {path: bool}。"""
    def check() -> dict:
        out = {}
        for p in body.paths:
            out[p] = annotator.is_explained(p)
        return out

    return await _run(check)


# ---- 文件夹学习 ----------------------------------------------------------

class FolderBody(BaseModel):
    path: str = ""
    force: bool = False


@app.post("/api/folder")
async def folder(body: FolderBody):
    try:
        return await _run(folder_learn.learn_folder, body.path, force=body.force)
    except repo_scanner.PathError as e:
        raise HTTPException(400, str(e))
    except llm_client.LLMError as e:
        raise HTTPException(502, str(e))


# ---- 路线图 --------------------------------------------------------------

@app.get("/api/roadmap")
async def get_roadmap():
    try:
        return await _run(roadmap.generate, False)
    except llm_client.LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/api/roadmap/cached")
async def get_roadmap_cached():
    """只返回已缓存的路线图，从不触发生成。供页面启动时轻量加载。"""
    from . import cache as _cache

    data = _cache.get_roadmap()
    if data is None:
        raise HTTPException(404, "尚无缓存路线图")
    data["cached"] = True
    return data


@app.post("/api/roadmap/regenerate")
async def regenerate_roadmap():
    try:
        return await _run(roadmap.generate, True)
    except llm_client.LLMError as e:
        raise HTTPException(502, str(e))


class RoadmapSaveBody(BaseModel):
    roadmap: dict


@app.post("/api/roadmap/save")
async def save_roadmap(body: RoadmapSaveBody):
    """保存用户微调后的路线图（重排/删除/改标题），不重新生成、不调用 LLM。"""
    data = await _run(roadmap.save_edited, body.roadmap)
    return data


# ---- 静态前端 ------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# /static/* → frontend/（放在最后，避免吃掉 /api）
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
