"""ReAct 循环驱动器：让模型多轮「思考 → 调用工具 → 观察」后产出最终 JSON。

设计要点：
- 不依赖模型原生 function-calling，纯提示式 ReAct（每轮要求模型输出一个
  JSON：要么 {"thought","action","action_input"} 调用工具，要么
  {"thought","final":{...}} 收尾）。对任何 OpenAI 兼容 chat 模型都可用。
- 以 generator yield 进度事件，供上层做 SSE 流式；最后 yield result 事件。
- 预算保护：迭代次数与文件读取次数双上限，耗尽时强制模型立即产出 final。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from . import agent_tools, llm_client


@dataclass
class Budget:
    """探索预算。均衡默认：最多 12 轮、10 次文件读取。"""
    max_iters: int = 12
    max_reads: int = 10


def _short(action: str, action_input: Any) -> str:
    """把一次工具调用压成一句人读的进度描述。"""
    if isinstance(action_input, dict):
        arg = action_input.get("path") or action_input.get("query") or ""
    else:
        arg = str(action_input or "")
    label = {"read_file": "读取", "list_dir": "查看目录", "search": "搜索"}.get(action, action)
    return f"{label} {arg}".strip()


def _extract_final(step: Any):
    """从模型一步输出里提取最终结果：支持 {"final":{...}} 或直接的路线图对象。

    返回 (found: bool, data)。found=False 表示这一步不是收尾。
    """
    if not isinstance(step, dict):
        return True, step  # 非 dict，直接当结果兜底
    if "final" in step:
        return True, step.get("final")
    if "steps" in step:  # 模型没包 final，直接吐了路线图
        return True, step
    return False, None


def run(
    system: str,
    seed_user: str,
    budget: Optional[Budget] = None,
    *,
    finalize=None,
    temperature: float = 0.2,
    max_tokens: int = 6000,
) -> Iterator[Dict[str, Any]]:
    """驱动 ReAct 循环。

    参数 finalize: 可选回调 finalize(findings: list[str]) -> data。findings 是
    探索期 read_file/search 收集到的观测文本。收尾时优先用它在**全新、无工具**的
    上下文里产出最终结果——避免模型被自己的 action 历史带偏、持续调用工具不收尾。

    yield 事件：
      {"type":"progress","msg": str}         —— 每次工具调用前
      {"type":"result","data": dict|None}    —— 最终结果；失败为 None
      {"type":"error","msg": str}            —— 不可恢复错误
    """
    budget = budget or Budget()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": seed_user},
    ]

    reads_used = 0
    findings: list = []  # 探索期收集的 read_file/search 观测，供 finalize 使用
    seen_reads: set = set()  # 已读过的 (path,start,end)，避免重复读白耗预算

    # ---- 探索阶段 ----
    for _ in range(budget.max_iters):
        try:
            step = llm_client.chat_json_messages(
                messages, temperature=temperature, max_tokens=max_tokens, retries=1,
            )
        except llm_client.LLMError as e:
            yield {"type": "error", "msg": str(e)}
            return

        found, data = _extract_final(step)
        if found:
            yield {"type": "result", "data": data}
            return

        action = step.get("action")
        action_input = step.get("action_input")
        if not action:
            yield {"type": "result", "data": step}  # 无法推进，兜底
            return

        messages.append({"role": "assistant", "content": json.dumps(step, ensure_ascii=False)})

        # 重复读同一文件同一区间 → 不重复执行、不耗预算，提醒模型换目标
        if action == "read_file":
            ai = action_input if isinstance(action_input, dict) else {"path": action_input}
            rkey = (ai.get("path", ""), ai.get("start"), ai.get("end"))
            if rkey in seen_reads:
                messages.append({
                    "role": "user",
                    "content": f"[提示] 你已经读过 {rkey[0]} 的这一区间，不要重复读；"
                               f"请读**尚未读过**的文件，或直接产出最终路线图。（已用读取 {reads_used}/{budget.max_reads}）",
                })
                continue
            seen_reads.add(rkey)

        yield {"type": "progress", "msg": _short(action, action_input)}

        observation = agent_tools.dispatch(action, action_input)
        ok = not observation.startswith("[错误]")
        if action in ("read_file", "search") and ok:
            findings.append(observation)
        if action == "read_file" and ok:
            reads_used += 1

        messages.append({
            "role": "user",
            "content": f"[工具 {action} 的结果]\n{observation}",
        })

        if reads_used >= budget.max_reads:
            break  # 读预算耗尽 → 收尾

    # ---- 收尾阶段：全新无工具上下文，避免被 action 历史带偏 ----
    if finalize is not None:
        try:
            data = finalize(findings)
        except llm_client.LLMError as e:
            yield {"type": "error", "msg": str(e)}
            return
        yield {"type": "result", "data": data}
        return

    # 无 finalize 回调时的通用兜底：在原对话里逼一次最终 JSON
    messages.append({
        "role": "user",
        "content": "探索到此为止，现在只输出最终结果 JSON（{\"final\":{...}}），不要再调用工具。",
    })
    try:
        step = llm_client.chat_json_messages(
            messages, temperature=temperature, max_tokens=max_tokens, retries=1,
        )
    except llm_client.LLMError as e:
        yield {"type": "error", "msg": str(e)}
        return
    _, data = _extract_final(step)
    yield {"type": "result", "data": data}


def run_to_result(system: str, seed_user: str, budget: Optional[Budget] = None, **kw) -> Any:
    """不需要流式进度的调用方：跑完循环直接拿最终 data（可能为 None）。"""
    data = None
    for ev in run(system, seed_user, budget, **kw):
        if ev["type"] == "result":
            data = ev["data"]
        elif ev["type"] == "error":
            raise llm_client.LLMError(ev["msg"])
    return data
