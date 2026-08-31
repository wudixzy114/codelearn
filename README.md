# codelearn

> **大型代码库快速学习工具：AI 自动生成学习路线图，逐块讲解代码，文件夹级模块导读，左代码右讲解双栏对照。**

## 项目定位 / 背景

`codelearn` 解决一个让所有程序员都头疼的问题：**接手一个陌生的大型代码库，怎么快速吃透？** 传统做法是"先读 README、再追 main 入口、然后逐文件看"——遇到 10 万行级别的项目就崩了。

本工具用 AI 把这个流程自动化：

1. **学习路线图**：AI 主动探索真实源码（ReAct 循环调 `list_dir`/`read_file`/`search`），最后输出一条有序路线，每一步列出要读的具体文件（可点击直达）
2. **逐块讲解**：打开任意文件，AI 把它切成语义块（左代码、右讲解），每块标 `start_line` / `end_line` / `kind` / `comment` / `detail`（选读再解释）
3. **文件夹导读**：打开任意文件夹，得模块职责概览 + 每个文件的一行摘要 + 建议阅读顺序

**核心理念：工具与被学习的代码库解耦** —— `.env` 配置 LLM、UI 上打开任意文件夹当工作区、最近工作区跨重启自动恢复。

**LLM 走内部多模型网关**（`JD_LLM_*` + `XIAOSHU_MODEL`），客户端在 `providers.py` 实现了**三种方言统一封装**：OpenAI（DeepSeek）/ Gemini 3（含流式 + 思考额度分档）/ Anthropic（Claude）。三种走同一个 host 但不同路径，OpenAI 走 `/v1/chat/completions`、Gemini 走 `/v1/responses`、Anthropic 走 `/anthropic/v1/messages`。Gemini 3 Flash 在响应前会消耗"思考"token，所以 `max_tokens` 太小会饿死可见输出——这个细节已经在 `llm_client.ping` 健康检查里做了预算保护。

## 仓库结构

```
codelearn/
├── requirements.txt                 # fastapi / uvicorn / openai / httpx / python-dotenv
├── run.sh                           # 一键启动（uvicorn backend.main:app）
├── .env.example                     # JD_LLM_API_KEY / JD_LLM_BASE_URL / XIAOSHU_MODEL
├── README.md
├── backend/
│   ├── main.py                      # FastAPI app + 所有路由（配置/工作区/路线图/讲解/folder）
│   ├── config.py                    # .env 加载 + 工作区校验 + 持久化最近列表
│   ├── llm_client.py                # chat_json / chat_json_messages / chat_stream / ping
│   ├── providers.py                 # 三方言（openai/gemini/anthropic）统一封装
│   ├── prompts.py                   # 所有 prompt 模板（~21KB）
│   ├── repo_scanner.py              # 路径越界校验 + 语言识别 + 紧凑 repo map + 文件读取
│   ├── agent_loop.py                # 纯提示式 ReAct 循环（无 function-calling 依赖）
│   ├── agent_tools.py               # list_dir / read_file / search 三个只读工具
│   ├── roadmap.py                   # ReAct 驱动：模型自主探索后输出路线图
│   ├── annotator.py                 # 单文件分块讲解（绝对行号锚定 + 连续性校验）
│   ├── folder_learn.py              # 文件夹级导读（并发摘要 + 缓存复用）
│   └── cache.py                     # 磁盘缓存（key: path+content_hash+lang）
└── frontend/
    ├── index.html                   # SPA 入口（无构建步骤，原生 JS + highlight.js）
    ├── app.js                       # ~51KB 全部前端逻辑
    ├── style.css                    # 样式
    └── vendor/
        ├── highlight.min.js         # 代码高亮（~123KB）
        └── github-dark.min.css      # 高亮主题
```

## 技术栈

| 维度 | 选型 | 版本/说明 |
|------|------|-----------|
| 运行时 | Python | ≥ 3.10 |
| Web 框架 | FastAPI | ≥ 0.110 |
| ASGI | uvicorn[standard] | ≥ 0.27 |
| LLM SDK | openai（兼容客户端） | ≥ 1.30 |
| HTTP | httpx | ≥ 0.27 |
| 配置 | python-dotenv | ≥ 1.0 |
| 前端 | 原生 HTML + JS + CSS | **无构建步骤**（无 React/Vue） |
| 代码高亮 | highlight.js | 通过 vendor/ 静态引用 |
| 缓存 | 磁盘 JSON | `.cache/<repo>-<path-hash>/` |

## 核心模块 / 特性

### 1. 工作区（Workspace）—— 工具与代码库解耦
`config.py::Settings.open_workspace(raw)` 校验非空 → 展开 `~` 与相对 → 必须存在 → 必须为目录 → 必须可读；通过后写到 `.cache/workspaces.json` 的 `last` + `recents`（最多 12 个）。前端进入应用时可"浏览本机文件系统 / 粘贴绝对路径 / 从最近打开选择"。

### 2. `providers.py` —— 三方言统一
`_THINKING = {"gemini":{off:0,medium:4096,max:24576}, "anthropic":{off:None,medium:4096,max:32000}, "openai":{off:None,medium:None,max:None}}` —— 思考额度分档；DeepSeek 不暴露开关，忽略。所有 provider 内部统一以 OpenAI 风格 `messages` 作为输入表示，再映射到各自 wire format。

### 3. `agent_loop.run` —— 纯提示式 ReAct
不依赖模型原生 function-calling，每轮让模型输出一个 JSON：
- `{"thought", "action", "action_input"}` 调工具
- `{"thought", "final": {...}}` 收尾

Budget 保护：默认 12 轮、10 次文件读取。`finalize` 回调允许"在全新无工具上下文里收尾"，避免模型被自己的 action 历史带偏。`yield` 进度事件 → 供上层做 SSE 流式。

### 4. `agent_tools.dispatch` —— 三个只读工具
- `list_dir(path)`：列目录（不递归）
- `read_file(path, start?, end?)`：按 1-based 行号区间读文件，超 4000 字符截断
- `search(query)`：朴素文本搜索（仅源码文件，跳过 `.git`/`node_modules`/`.venv`/二进制），最多扫 2000 文件 / 30 命中

所有工具**永不抛异常**——失败也返回可读错误文本，让 agent 自行纠偏。路径越界校验在 `repo_scanner.resolve` 内完成，杜绝 `../../etc/passwd`。

### 5. `annotator.annotate_file` —— 分块讲解
按 `chunk_lines`（默认 400）+ `chunk_overlap` 切窗口，**绝对行号锚定**，**块连续性校验**（必须从第 1 行连续覆盖到最后一行，无空隙、无重叠），任何空隙自动补 `kind="raw"` 的无讲解块。`force` 参数绕过缓存。

### 6. `folder_learn.learn_folder` —— 文件夹导读
为目录下 ≤40 个代码文件生成一行摘要（ThreadPoolExecutor 并发 6 worker），调用 LLM 生成 `overview` + `notes` + `suggested_order`，**子摘要优先复用单文件讲解缓存**。目录结构变化（直接子代码文件集变化）即自动失效。

### 7. `cache.py` —— 智能缓存
按「相对路径 + 内容 hash + 语言」缓存到 `.cache/<仓库名>-<路径哈希>/`，文件变化即失效重算。不同工作区独立缓存。`content_hash(rel, sig, lang, version)` 决定 cache key。

### 8. `main.py` —— API 路由
- `GET/POST /api/config{,/language,/model}` 公开配置切换
- `POST /api/workspace/open` 切换工作区
- `POST /api/roadmap/generate` 启动 ReAct 生成路线图（SSE 流式）
- `POST /api/annotate` 启动单文件分块讲解
- `POST /api/folder/learn` 启动文件夹导读
- `POST /api/chat` 右侧对话（SSE 流式）

`asyncio.to_thread` 把阻塞的 LLM 调用丢到线程池，避免卡住事件循环。

## 已完成 / 进行中

- ✅ FastAPI 后端 + 12+ 路由
- ✅ 三方言 LLM provider 统一封装（OpenAI / Gemini / Anthropic）
- ✅ 思考额度分档（off / medium / max）
- ✅ 纯提示式 ReAct 循环（12 轮 + 10 读 + finalize 兜底）
- ✅ 三个只读 Agent 工具 + 路径越界校验
- ✅ 工作区管理（打开/最近/恢复/校验）
- ✅ 单文件分块讲解（连续性校验）
- ✅ 文件夹导读（摘要复用 + 并发）
- ✅ 磁盘缓存（path + content_hash + lang）
- ✅ 前端无构建（原生 JS + highlight.js）
- ⏳ 真实部署的截图/演示视频
- ⏳ 单元测试（无 test 目录）
- ⏳ 多用户 / 鉴权
- ⏳ 嵌入式向量检索（当前讲解靠 LLM 一次性生成，无 RAG）

## 本地开发

```bash
# 装依赖
pip install -r requirements.txt
# 或用 uv：uv pip install -r requirements.txt

# 配 LLM 网关
cp .env.example .env
# 编辑 .env：填 JD_LLM_API_KEY / JD_LLM_BASE_URL / XIAOSHU_MODEL

# 启动（默认 127.0.0.1:43187）
./run.sh                          # 启动后再选工作区
./run.sh /path/to/your/repo       # 启动时直接打开工作区

# 自定义端口 / 主机
CODELEARN_PORT=5000 ./run.sh
CODELEARN_HOST=0.0.0.0 ./run.sh
```

环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `CODELEARN_TARGET` | 无 | 启动时的初始工作区（也可启动后在 UI 里打开） |
| `CODELEARN_LANG` | `zh` | 讲解语言（`zh`/`en`，前端也可切换） |
| `CODELEARN_CHUNK_LINES` | `400` | 大文件分块窗口行数 |
| `CODELEARN_PORT` | `43187` | 服务端口 |

打开 `http://127.0.0.1:43187`，首次进入若未指定工作区会弹出选择器（浏览/粘贴路径/最近打开）。

## 状态

**v1.0.1** —— 后端 + 前端 + 缓存 + ReAct 完整。生产可用，适合本地学习任意代码库。

## License

MIT（仓库内未显式声明 LICENSE 文件）
