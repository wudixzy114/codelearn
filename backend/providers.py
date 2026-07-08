"""多模型 provider 层：同一网关（llm-gw）下三种 API 方言的统一封装。

本模块是**纯无状态**的方言库——不依赖 config/settings，凭据由调用方传入，
因此 config.py 可安全地反向 import 这里的模型注册表而不形成循环依赖。

三种方言（均共用 Bearer key，主机相同，仅路径/请求体/响应体不同）：
- openai    : POST {root}/v1/chat/completions        —— DeepSeek 走这条
- gemini    : POST {root}/v1/responses               —— contents/parts 格式
- anthropic : POST {root}/anthropic/v1/messages      —— Messages 格式

内部统一以 OpenAI 风格的 messages（[{"role","content"}]）作为输入表示，
再由各 provider 映射成自己的请求体；响应/流式再统一解析回纯文本。

方言细节均已对活网关探针确认（2026-07）：
- Gemini 的流式**不是** SSE，而是逐行的裸 JSON 对象（无 `data:` 前缀）。
- Gemini 3 Flash 会先消耗“思考”token，max_tokens 过小会饿死可见输出，
  故健康检查需给足预算（见 llm_client.ping）。
"""
from __future__ import annotations

import json
from typing import Iterator, List, Optional, Tuple

import httpx


class LLMError(RuntimeError):
    """LLM 调用或解析失败，携带可读信息返回给前端。"""


_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


# ---- 模型注册表 ----------------------------------------------------------
# 每条：id（网关模型名，也是对外唯一标识）、label（下拉展示名）、provider（方言）。
MODELS: List[dict] = [
    {
        "id": "Gemini-3-Flash-Preview-joybuilder",
        "label": "Gemini 3 Flash",
        "provider": "gemini",
    },
    {
        "id": "Claude-Sonnet-4.6-joybuilder",
        "label": "Claude Sonnet 4.6",
        "provider": "anthropic",
    },
    {
        "id": "DeepSeek-V4-Pro-joybuilder",
        "label": "DeepSeek V4 Pro",
        "provider": "openai",
    },
]

# 默认模型：注册表首项（Gemini）。
DEFAULT_MODEL_ID: str = MODELS[0]["id"]

_BY_ID = {m["id"]: m for m in MODELS}


def is_valid_model(model_id: str) -> bool:
    return model_id in _BY_ID


def provider_for(model_id: str) -> Optional[str]:
    spec = _BY_ID.get(model_id)
    return spec["provider"] if spec else None


def _resolve(model_id: str) -> dict:
    spec = _BY_ID.get(model_id)
    if spec is None:
        raise LLMError(f"未知模型：{model_id}")
    return spec


# ---- 网关根地址 ----------------------------------------------------------

def gateway_root(base_url: Optional[str]) -> str:
    """从 settings.llm_base_url 推导网关根：去掉结尾的 /v1。

    例：http://llm-gw.jd.local/v1 → http://llm-gw.jd.local
    """
    b = (base_url or "").strip().rstrip("/")
    if not b:
        raise LLMError("LLM 网关地址为空：请在 .env 设置 JD_LLM_BASE_URL")
    if b.endswith("/v1"):
        b = b[:-3]
    return b.rstrip("/")


# ---- 消息映射（OpenAI 风格 → 各方言）------------------------------------

def _split_system(messages) -> Tuple[str, List[dict]]:
    """anthropic 用：抽出所有 system 文本拼成顶层 system 串，其余为对话消息。"""
    sys_parts: List[str] = []
    conv: List[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            if content:
                sys_parts.append(content)
        elif role in ("user", "assistant"):
            conv.append({"role": role, "content": content})
    return "\n\n".join(sys_parts), conv


def _to_gemini(messages) -> Tuple[str, List[dict]]:
    """gemini 用：system 文本单列，其余映射为 contents（assistant→model）。"""
    sys_parts: List[str] = []
    contents: List[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            if content:
                sys_parts.append(content)
            continue
        g_role = "model" if role == "assistant" else "user"
        contents.append({"role": g_role, "parts": [{"text": content}]})
    return "\n\n".join(sys_parts), contents


def _build_request(
    spec: dict,
    messages,
    *,
    temperature: float,
    max_tokens: int,
    stream: bool,
    base_url: Optional[str],
    api_key: Optional[str],
) -> Tuple[str, dict, dict]:
    """构造 (url, headers, body)。"""
    if not api_key:
        raise LLMError("LLM 未配置：请在 .env 设置 JD_LLM_API_KEY")
    root = gateway_root(base_url)
    provider = spec["provider"]
    model = spec["id"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if provider == "openai":
        url = f"{root}/v1/chat/completions"
        body: dict = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if stream:
            body["stream"] = True

    elif provider == "anthropic":
        url = f"{root}/anthropic/v1/messages"
        sys_txt, conv = _split_system(messages)
        body = {
            "model": model,
            "messages": conv,
            "max_tokens": max_tokens,      # anthropic 必填
            "temperature": temperature,
        }
        if sys_txt:
            body["system"] = sys_txt
        if stream:
            body["stream"] = True

    elif provider == "gemini":
        url = f"{root}/v1/responses"
        sys_txt, contents = _to_gemini(messages)
        body = {
            "model": model,
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if sys_txt:
            body["system_instruction"] = {"parts": [{"text": sys_txt}]}
        if stream:
            body["stream"] = True

    else:  # pragma: no cover
        raise LLMError(f"未知 provider：{provider}")

    return url, headers, body


# ---- 响应解析（非流式）---------------------------------------------------

def _parse_complete(provider: str, data: dict) -> str:
    """把各方言的完整响应体解析成纯文本。"""
    try:
        if provider == "openai":
            return data["choices"][0]["message"].get("content") or ""
        if provider == "anthropic":
            blocks = data.get("content") or []
            return "".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            )
        if provider == "gemini":
            cands = data.get("candidates") or []
            if not cands:
                return ""
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMError(f"无法解析 {provider} 响应：{json.dumps(data)[:300]}")
    raise LLMError(f"未知 provider：{provider}")


# ---- 流式解析（逐行）-----------------------------------------------------

def _parse_stream_line(provider: str, raw: str) -> Optional[str]:
    """把一行流式数据解析成文本增量；非文本/控制行返回 None。

    openai/anthropic 为 SSE（`data: {...}`，anthropic 另有 `event:` 行）；
    gemini 为裸 JSON 行（无 `data:` 前缀）。三者都尽量防御：解析失败即跳过。
    """
    line = raw.strip()
    if not line:
        return None

    if provider in ("openai", "anthropic"):
        if not line.startswith("data:"):
            return None                       # 跳过 event: 等控制行
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if provider == "openai":
            try:
                return obj["choices"][0]["delta"].get("content") or None
            except (KeyError, IndexError, TypeError):
                return None
        # anthropic：仅取 content_block_delta 的 text
        if obj.get("type") == "content_block_delta":
            return (obj.get("delta") or {}).get("text") or None
        return None

    if provider == "gemini":
        payload = line
        if payload.startswith("data:"):          # 防御：万一网关加了前缀
            payload = payload[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        cands = obj.get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return text or None

    return None


# ---- 对外接口 ------------------------------------------------------------

def complete(
    base_url: Optional[str],
    api_key: Optional[str],
    model_id: str,
    messages,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """一次非流式补全，返回纯文本。失败抛 LLMError。"""
    spec = _resolve(model_id)
    url, headers, body = _build_request(
        spec, messages, temperature=temperature, max_tokens=max_tokens,
        stream=False, base_url=base_url, api_key=api_key,
    )
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise LLMError(f"LLM 调用失败：{e}")
    if resp.status_code >= 400:
        raise LLMError(f"网关返回 {resp.status_code}：{resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError:
        raise LLMError(f"网关响应非 JSON：{resp.text[:300]}")
    return _parse_complete(spec["provider"], data)


def stream(
    base_url: Optional[str],
    api_key: Optional[str],
    model_id: str,
    messages,
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> Iterator[str]:
    """流式补全：逐段 yield 文本增量。失败抛 LLMError（由上层转成 [ERROR]）。"""
    spec = _resolve(model_id)
    provider = spec["provider"]
    url, headers, body = _build_request(
        spec, messages, temperature=temperature, max_tokens=max_tokens,
        stream=True, base_url=base_url, api_key=api_key,
    )
    try:
        with httpx.stream("POST", url, headers=headers, json=body, timeout=_TIMEOUT) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise LLMError(f"网关返回 {resp.status_code}：{resp.text[:300]}")
            for line in resp.iter_lines():
                piece = _parse_stream_line(provider, line)
                if piece:
                    yield piece
    except httpx.HTTPError as e:
        raise LLMError(f"LLM 流式调用失败：{e}")
