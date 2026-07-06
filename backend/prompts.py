"""提示词模板集中管理（中/英两套）。

设计原则：
- 严格要求纯 JSON 输出，便于稳健解析。
- 逐块讲解使用「绝对行号」锚定，保证前端代码↔讲解精确对齐。
- 语言由 settings.language 决定，只影响讲解文字，代码永不改动。
"""
from __future__ import annotations

from typing import Dict

# ======================================================================
# 单文件逐块讲解
# ======================================================================

_ANNOTATE_SYSTEM = {
    "zh": (
        "你是一位资深软件工程师，正在为初到此代码库的工程师做“代码导读”。"
        "你会拿到一个源文件的一段带行号的内容，需要把它切成若干连续的语义块，"
        "为每块写出两层讲解：一是紧贴代码的【行内注释 comment】（简短，像写在代码旁边的注释），"
        "二是【深入解释 detail】（当读者看了注释仍不懂时展开阅读的补充说明）。"
        "只输出 JSON，不要任何额外文字。"
    ),
    "en": (
        "You are a senior engineer giving a code walkthrough to someone new to this "
        "codebase. You receive a numbered slice of a source file, split it into contiguous "
        "semantic blocks, and for each block write two layers: an inline [comment] (short, "
        "like a comment written next to the code) and a [detail] (deeper explanation the "
        "reader expands only if the comment isn't enough). Output JSON only, no extra prose."
    ),
}

_ANNOTATE_USER = {
    "zh": """文件路径：{path}
语言：{language}
本段覆盖的行号范围：第 {start} 行 到 第 {end} 行（行号为文件内的绝对行号）。

要求：
1. 把这段代码切成若干“连续、无重叠”的语义块（一个函数、一个类、一组相关声明、一段逻辑）。
2. 每块给出：start_line、end_line（绝对行号，必须落在 [{start}, {end}] 内且首尾相接覆盖全段）、kind、comment、detail。
3. kind 取值：license(版权头)、includes(头文件/import)、macro、class、struct、function、method、logic、data、comment、other。
4. comment（行内注释，主讲解）：一到两句中文，简短精炼，点明这块在做什么/关键意图，像资深工程师顺手写在代码旁的注释。这是读者最先、也最常看到的讲解。
5. detail（深入解释，选读）：仅当有值得展开的内容时才写——为什么这么设计、参数/返回/副作用、易错点、与其他模块的关系；若 comment 已足够则留空字符串。
6. 不要逐行翻译代码；comment 要短，detail 才展开。
7. 只输出如下 JSON：
{{"blocks":[{{"start_line":<int>,"end_line":<int>,"kind":"<str>","comment":"<简短行内注释>","detail":"<深入解释，可为空>"}}]}}

代码（带绝对行号）：
{code}
""",
    "en": """File path: {path}
Language: {language}
Absolute line range of this slice: line {start} to line {end}.

Requirements:
1. Split into contiguous, non-overlapping semantic blocks.
2. Each block: start_line, end_line (absolute, within [{start}, {end}], covering the whole slice head-to-tail), kind, comment, detail.
3. kind: license, includes, macro, class, struct, function, method, logic, data, comment, other.
4. comment (inline, primary): one or two short sentences stating what the block does / key intent, like a senior engineer's inline comment. This is what the reader sees first and most.
5. detail (deeper, optional): only when there is more worth expanding — why this design, params/returns/side effects, pitfalls, relations to other modules; empty string if the comment suffices.
6. Do not translate line by line; keep comment short, expand in detail.
7. Output only:
{{"blocks":[{{"start_line":<int>,"end_line":<int>,"kind":"<str>","comment":"<short inline>","detail":"<deeper, may be empty>"}}]}}

Code (with absolute line numbers):
{code}
""",
}

_OVERVIEW_SYSTEM = {
    "zh": "你是资深工程师。用中文写一段简明的文件职责概览。只输出 JSON。",
    "en": "You are a senior engineer. Write a concise file overview in English. Output JSON only.",
}

_OVERVIEW_USER = {
    "zh": """文件路径：{path}
语言：{language}
总行数：{total_lines}

请用 2-4 句中文概括：这个文件在整个项目里承担什么职责、定义了哪些核心类型/函数、
在数据流或控制流中处于什么位置。避免空泛，抓住要点。
只输出：{{"overview":"<中文概览>","role":"<一句话定位，如：调度器抽象基类>"}}

文件开头节选：
{head}
""",
    "en": """File path: {path}
Language: {language}
Total lines: {total_lines}

In 2-4 English sentences: what role this file plays in the project, the core
types/functions it defines, and where it sits in the data/control flow.
Output only: {{"overview":"<overview>","role":"<one-line role>"}}

Head of file:
{head}
""",
}

# ======================================================================
# 学习路线图
# ======================================================================

_ROADMAP_SYSTEM = {
    "zh": (
        "你是一位技术导师，要为一个大型代码库设计一条“从零到理解”的学习路线。"
        "你会拿到该项目的紧凑结构图（目录、文件数、入口点、依赖线索）和现有文档摘录。"
        "请规划有序的学习步骤，让学习者按顺序阅读即可循序渐进地掌握整个系统。"
        "只输出 JSON，不要任何额外文字。"
    ),
    "en": (
        "You are a technical mentor designing a zero-to-understanding learning path for a "
        "large codebase. You receive a compact structure map (directories, file counts, "
        "entry points, dependency hints) and excerpts of existing docs. Produce ordered "
        "learning steps so a reader can progressively master the whole system. "
        "Output JSON only."
    ),
}

_ROADMAP_USER = {
    "zh": """项目名：{repo_name}

请规划一条学习路线，遵循以下原则：
- 先从“入口点 + 全局概览”开始，建立整体印象；
- 中段按“依赖方向自底向上”：先基础词汇/工具/配置，再核心领域模型，再上层子系统；
- 末段回到“编排层 / 服务层”，闭合整个调用链；
- 每一步聚焦一个主题，列出该步需要阅读的具体文件（相对路径），文件要真实存在于结构图中；
- 步骤数量控制在 8-14 步，循序渐进，避免一步塞太多文件（每步 2-8 个关键文件为宜）。

只输出如下 JSON：
{{"title":"<路线标题>",
  "summary":"<用 2-3 句概括这个项目是做什么的，以及这条路线的整体思路>",
  "steps":[
    {{"title":"<步骤标题>",
      "goal":"<读完这步能理解什么>",
      "description":"<这一步的讲解，说明为什么按此顺序、各文件之间的关系>",
      "files":["<相对路径>", ...]}}
  ]}}

==== 项目结构图 ====
{repo_map}

==== 现有文档摘录 ====
{docs}
""",
    "en": """Project: {repo_name}

Design a learning path following these principles:
- Start with entry points + a global overview to form a mental model;
- Middle: bottom-up by dependency direction (foundational vocab/utils/config, then core
  domain model, then higher-level subsystems);
- End: return to the orchestration/service layer to close the call chain;
- Each step focuses on one theme and lists the concrete files (relative paths) to read;
  files must actually exist in the structure map;
- 8-14 steps, progressive, 2-8 key files per step.

Output only:
{{"title":"...","summary":"...",
  "steps":[{{"title":"...","goal":"...","description":"...","files":["..."]}}]}}

==== Structure map ====
{repo_map}

==== Existing docs ====
{docs}
""",
}

# ======================================================================
# 文件夹级学习
# ======================================================================

_FOLDER_SYSTEM = {
    "zh": (
        "你是资深工程师，要为代码库中的一个目录做“模块导读”。"
        "根据目录内文件清单（含每个文件的一行摘要）总结该目录的职责，并给出建议的阅读顺序。"
        "只输出 JSON。"
    ),
    "en": (
        "You are a senior engineer giving a module walkthrough for one directory. "
        "Summarize the directory's responsibility from its file list (each with a one-line "
        "summary) and suggest a reading order. Output JSON only."
    ),
}

_FOLDER_USER = {
    "zh": """目录：{path}
包含 {n_files} 个代码文件、{n_subdirs} 个子目录。

请输出：
{{"overview":"<用 2-4 句中文说明该目录的整体职责与内部组织>",
  "suggested_order":["<相对路径>", ...],
  "notes":"<可选：阅读建议，如先看哪个基类/接口>"}}
suggested_order 要用下方清单里真实存在的文件路径，按“从基础到具体”排序。

==== 文件清单（路径 : 摘要） ====
{file_list}

==== 子目录 ====
{subdir_list}
""",
    "en": """Directory: {path}
Contains {n_files} code files and {n_subdirs} subdirectories.

Output:
{{"overview":"...","suggested_order":["..."],"notes":"..."}}
suggested_order must use real paths from the list below, ordered foundational-to-specific.

==== Files (path : summary) ====
{file_list}

==== Subdirectories ====
{subdir_list}
""",
}


# ---- 取值助手 ------------------------------------------------------------

def _pick(mapping: Dict[str, str], lang: str) -> str:
    return mapping.get(lang, mapping["zh"])


def annotate_system(lang: str) -> str:
    return _pick(_ANNOTATE_SYSTEM, lang)


def annotate_user(lang: str, **kw) -> str:
    return _pick(_ANNOTATE_USER, lang).format(**kw)


def overview_system(lang: str) -> str:
    return _pick(_OVERVIEW_SYSTEM, lang)


def overview_user(lang: str, **kw) -> str:
    return _pick(_OVERVIEW_USER, lang).format(**kw)


def roadmap_system(lang: str) -> str:
    return _pick(_ROADMAP_SYSTEM, lang)


def roadmap_user(lang: str, **kw) -> str:
    return _pick(_ROADMAP_USER, lang).format(**kw)


def folder_system(lang: str) -> str:
    return _pick(_FOLDER_SYSTEM, lang)


def folder_user(lang: str, **kw) -> str:
    return _pick(_FOLDER_USER, lang).format(**kw)
