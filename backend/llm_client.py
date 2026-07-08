"""LLM 客户端：面向业务层的稳定接口，内部按当前模型分派到 providers 方言层。

职责：
- chat_json() / chat_json_messages()：请求并稳健解析出 JSON（剥离围栏、修尾逗号，失败重试）。
- chat_stream()：流式文本增量。
- ping()：健康检查，供 /api/health 验证网关+当前模型可达性。

公开 API（LLMError / parse_json / chat_json / chat_json_messages / chat_stream / ping）
对上层 annotator / folder_learn / agent_loop / roadmap / main 保持不变；
多模型/多方言的差异全部封装在 providers.py。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import providers
from .config import settings

# 复用 providers 的异常类型：providers 内部 raise 的 LLMError 与此处同一类，
# 故上层 `except llm_client.LLMError` 仍能捕获。
LLMError = providers.LLMError


def _creds():
    """返回 (base_url, api_key)；未配置时抛 LLMError。"""
    if not (settings.llm_api_key and settings.llm_base_url):
        raise LLMError(
            "LLM 未配置：请在 tools/.env 设置 JD_LLM_API_KEY 与 JD_LLM_BASE_URL"
        )
    return settings.llm_base_url, settings.llm_api_key


# ---- JSON 解析容错 -------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏。"""
    t = text.strip()
    if t.startswith("```"):
        t = _FENCE_RE.sub("", t)
    return t.strip()


def _extract_json(text: str) -> str:
    """从可能夹带说明文字的响应里截取最外层的 {...} 或 [...]。"""
    t = _strip_fence(text)
    # 找到第一个 { 或 [ 与其匹配的收尾
    start = None
    for i, ch in enumerate(t):
        if ch in "{[":
            start = i
            break
    if start is None:
        return t
    open_ch = t[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return t[start:]


def _remove_trailing_commas(text: str) -> str:
    """去掉对象/数组里 } ] 前的多余逗号（模型常见小错）。"""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def parse_json(text: str) -> Any:
    """尽力把模型输出解析成 Python 对象；失败抛 LLMError。"""
    candidate = _extract_json(text)
    for attempt in (candidate, _remove_trailing_commas(candidate)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    raise LLMError("无法解析模型返回的 JSON：" + text[:300])


# ---- 对外接口 ------------------------------------------------------------


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    retries: int = 1,
) -> Any:
    """发起一次对话并返回解析后的 JSON 对象。失败自动重试 retries 次。"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return chat_json_messages(
        messages, temperature=temperature, max_tokens=max_tokens, retries=retries
    )


def chat_json_messages(
    messages,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    retries: int = 1,
) -> Any:
    """发起一次多轮对话并返回解析后的 JSON 对象。

    与 chat_json 共用解析/重试逻辑，但接收完整 messages 列表（agent 循环
    需要累积的对话历史，而非固定的 system+user 两条）。
    """
    base_url, api_key = _creds()
    last_err: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            content = providers.complete(
                base_url, api_key, settings.llm_model, messages,
                temperature=temperature, max_tokens=max_tokens,
            )
            return parse_json(content)
        except LLMError as e:
            last_err = e
        except Exception as e:  # 网络/网关错误
            last_err = LLMError(f"LLM 调用失败：{e}")
    raise last_err if last_err else LLMError("LLM 调用失败（未知原因）")


def chat_stream(messages, *, temperature: float = 0.3, max_tokens: int = 2048):
    """流式对话：逐段 yield 文本增量。messages 为 OpenAI 格式的消息列表。

    失败时 yield 一段以 [ERROR] 开头的说明文字，由上层决定如何展示。
    """
    try:
        base_url, api_key = _creds()
    except LLMError as e:
        yield f"[ERROR] {e}"
        return
    try:
        for piece in providers.stream(
            base_url, api_key, settings.llm_model, messages,
            temperature=temperature, max_tokens=max_tokens,
        ):
            if piece:
                yield piece
    except Exception as e:
        yield f"[ERROR] LLM 流式调用失败：{e}"


def ping() -> dict:
    """健康检查：发一条极短请求，确认网关可达、密钥有效、当前模型可用。

    注意：给足 max_tokens。Gemini 3 等“会思考”的模型会先消耗思考 token，
    预算太小会饿死可见输出，导致健康检查误判为不可用。这里只要网关正常返回
    即视为健康（reply 可能为空）。
    """
    try:
        base_url, api_key = _creds()
    except LLMError as e:
        return {"ok": False, "error": str(e)}
    try:
        reply = providers.complete(
            base_url, api_key, settings.llm_model,
            [{"role": "user", "content": "ping，请只回复 pong"}],
            max_tokens=512, temperature=0,
        )
        return {"ok": True, "model": settings.llm_model, "reply": (reply or "").strip()}
    except Exception as e:
        return {"ok": False, "model": settings.llm_model, "error": f"{e}"}
