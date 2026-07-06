"""LLM 客户端封装：指向京东 OpenAI 兼容网关。

职责：
- 单例 OpenAI 客户端（base_url / api_key 来自 config）。
- chat_json()：请求并稳健地解析出 JSON（剥离围栏、修尾逗号，失败重试）。
- ping()：健康检查，供 /api/health 验证网关可达性。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .config import settings


class LLMError(RuntimeError):
    """LLM 调用或解析失败，携带可读信息返回给前端。"""


_client = None


def _get_client():
    global _client
    if _client is None:
        if not (settings.llm_api_key and settings.llm_base_url):
            raise LLMError(
                "LLM 未配置：请在 tools/.env 设置 JD_LLM_API_KEY 与 JD_LLM_BASE_URL"
            )
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("缺少 openai 依赖，请先 pip install -r requirements.txt") from e
        _client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=120.0,
        )
    return _client


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
    client = _get_client()
    last_err: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            return parse_json(content)
        except LLMError as e:
            last_err = e
        except Exception as e:  # 网络/网关错误
            last_err = LLMError(f"LLM 调用失败：{e}")
    raise last_err if last_err else LLMError("LLM 调用失败（未知原因）")


def ping() -> dict:
    """健康检查：发一条极短请求，确认网关可达、密钥有效。"""
    try:
        client = _get_client()
    except LLMError as e:
        return {"ok": False, "error": str(e)}
    try:
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "ping，请只回复 pong"}],
            max_tokens=8,
            temperature=0,
        )
        reply = (resp.choices[0].message.content or "").strip()
        return {"ok": True, "model": settings.llm_model, "reply": reply}
    except Exception as e:
        return {"ok": False, "error": f"{e}"}
