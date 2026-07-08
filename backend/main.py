"""FastAPI 应用：API 路由 + 静态前端。

工作区（目标代码库）可在 UI 里随时打开任意文件夹，无需重启。
初始工作区可选，由环境变量 CODELEARN_TARGET 或上次打开的记录决定；都没有则空。
启动：uvicorn backend.main:app（可选 CODELEARN_TARGET=<repo>）
LLM 调用是同步阻塞的，放进线程池执行，避免卡住事件循环。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import annotator, folder_learn, llm_client, prompts, repo_scanner, roadmap
from .config import FRONTEND_DIR, WorkspaceError, settings

app = FastAPI(title="CodeLearn", version="1.0.1")


@app.exception_handler(WorkspaceError)
async def _workspace_error_handler(_request, exc: WorkspaceError):
    """未打开工作区 / 工作区非法：统一 400，避免直接读 cache_dir 的路由 500。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


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


class ModelBody(BaseModel):
    model: str


@app.post("/api/config/model")
async def set_model(body: ModelBody):
    """切换当前 LLM 模型（须属于注册表），返回更新后的公开配置。"""
    try:
        settings.set_model(body.model)
    except WorkspaceError as e:
        raise HTTPException(400, str(e))
    return settings.as_public_dict()


# ---- 工作区：打开任意文件夹 + 浏览宿主文件系统 --------------------------

class OpenWorkspaceBody(BaseModel):
    path: str


@app.post("/api/workspace/open")
async def open_workspace(body: OpenWorkspaceBody):
    """校验并切换到新工作区，返回更新后的公开配置。"""
    try:
        await _run(settings.open_workspace, body.path)
    except WorkspaceError as e:
        raise HTTPException(400, str(e))
    return settings.as_public_dict()


@app.get("/api/workspace/browse")
async def browse_workspace(path: str = Query("")):
    """列出宿主某目录的子目录，供选择器导航（不沙箱，仅列目录）。"""
    try:
        return await _run(repo_scanner.list_host_dir, path)
    except repo_scanner.PathError as e:
        raise HTTPException(400, str(e))


@app.get("/api/health")
async def health():
    result = await _run(llm_client.ping)
    repo = settings.target_repo
    return {
        "server": "ok",
        "target_repo": str(repo) if repo else None,
        "target_exists": bool(repo and repo.is_dir()),
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


# ---- 右侧对话分栏：详解 / 通识问答 / 引用问答（均流式返回） -------------

def _sse(gen):
    """把文本增量生成器包装成 text/event-stream。前端用 EventSource 或 fetch 流读。"""
    def event_stream():
        for piece in gen:
            # SSE 规范：data: 行，空行分隔事件。转义换行为多条 data。
            for line in piece.split("\n"):
                yield f"data: {line}\n"
            yield "\n"
        yield "event: done\ndata: [DONE]\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")


class DetailBody(BaseModel):
    path: str
    start_line: int
    end_line: int
    comment: str = ""


@app.post("/api/detail")
async def detail(body: DetailBody):
    """函数/代码块「深入详解」——注释旁小按钮触发，流式返回长篇讲解。"""
    try:
        text, language = repo_scanner.read_file(body.path)
    except repo_scanner.PathError as e:
        raise HTTPException(400, str(e))
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    s = max(1, body.start_line)
    e = min(len(lines), max(s, body.end_line))
    snippet = "\n".join(lines[s - 1 : e])

    lang = settings.language
    messages = [
        {"role": "system", "content": prompts.detail_system(lang)},
        {"role": "user", "content": prompts.detail_user(
            lang, path=body.path, language=language,
            start=s, end=e, comment=body.comment or "（无）", code=snippet,
        )},
    ]
    return _sse(llm_client.chat_stream(messages, temperature=0.3, max_tokens=2048))


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    messages: List[ChatMessage] = []       # 历史对话（不含本次注入的上下文）
    file_path: Optional[str] = None        # 当前所在文件（可选）
    selection: Optional[str] = None        # 选中的代码文本（引用问答时）
    sel_start: Optional[int] = None
    sel_end: Optional[int] = None


# 文件级问答注入的内容上限（字符）。超出则只给头部并注明。
_CHAT_FILE_CHAR_CAP = 12000


def _file_context_with_content(lang: str, path: str, language: str) -> str:
    """构建带文件实际内容的问答上下文；读失败退回仅路径提示。"""
    try:
        text, _ = repo_scanner.read_file(path)
    except repo_scanner.PathError:
        # 读不到（二进制/越界等）→ 退回旧的仅路径提示，避免整条请求失败
        return prompts.file_context(lang, path=path, language=language, code="（无法读取该文件内容）")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    numbered = "\n".join(f"{i + 1}\t{ln}" for i, ln in enumerate(lines))

    if len(numbered) <= _CHAT_FILE_CHAR_CAP:
        return prompts.file_context(lang, path=path, language=language, code=numbered)
    # 过大：截到上限（按行边界），走 truncated 模板
    clipped = numbered[:_CHAT_FILE_CHAR_CAP]
    nl = clipped.rfind("\n")
    if nl > 0:
        clipped = clipped[:nl]
    shown = clipped.count("\n") + 1
    return prompts.file_context_truncated(
        lang, path=path, language=language, code=clipped, total=total, shown=shown,
    )


@app.post("/api/chat")
async def chat(body: ChatBody):
    """右侧对话：三种任务合一——
    - 无 file_path、无 selection → 纯通识问答；
    - 有 file_path、无 selection → 带文件上下文的问答；
    - 有 selection → 引用问答，把选中代码注入上下文。
    """
    lang = settings.language
    msgs = [{"role": "system", "content": prompts.chat_system(lang)}]

    # 注入上下文（作为一条 system 补充，放在历史之前）
    ctx = None
    if body.selection and body.file_path:
        language = repo_scanner.lang_for(body.file_path)
        ctx = prompts.quote_context(
            lang, path=body.file_path, language=language,
            start=body.sel_start or 0, end=body.sel_end or 0,
            code=body.selection[:6000],
        )
    elif body.file_path:
        language = repo_scanner.lang_for(body.file_path)
        ctx = _file_context_with_content(lang, body.file_path, language)
    if ctx:
        msgs.append({"role": "system", "content": ctx})

    for m in body.messages[-12:]:
        if m.role in ("user", "assistant") and m.content:
            msgs.append({"role": m.role, "content": m.content})

    if not any(m["role"] == "user" for m in msgs):
        raise HTTPException(400, "缺少用户提问")

    return _sse(llm_client.chat_stream(msgs, temperature=0.4, max_tokens=2048))


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
    """流式生成路线图：SSE 逐条推送探索进度，末条携带最终 JSON。

    命中新鲜缓存时会立即只推一条 result 事件。
    """
    return _sse(roadmap.generate_stream(False))


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
    """强制重新生成（忽略缓存），同样流式返回进度 + 最终结果。"""
    return _sse(roadmap.generate_stream(True))


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
