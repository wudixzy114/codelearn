# CodeLearn — 大型代码库快速学习工具

用 AI 帮你快速吃透一个陌生的大型代码库：

1. **学习路线图**：AI 分析整个项目，产出一条有序的学习路线，每一步列出需要阅读的具体文件（可点击直达）。
2. **逐块讲解**：打开任意文件，AI 把它切成语义块，逐块给出「做什么 + 为什么 + 注意点」的中文讲解，左代码右讲解双栏对照。
3. **递归学习**：打开任意文件夹，得到该模块的职责概览、每个文件的一行摘要，以及建议阅读顺序。

工具本身与被学习的代码库解耦，可在界面上打开任意文件夹作为工作区。

## 快速开始

```bash
cd codelearn
pip install -r requirements.txt

# 直接启动，进浏览器后再选工作区
./run.sh

# 或指定初始工作区
./run.sh /path/to/your/repo
```

浏览器打开 http://127.0.0.1:43187

首次进入若未指定工作区，会弹出选择器：可**浏览本机文件系统**、**粘贴绝对路径**或从**最近打开**中选择。
选择会记住，下次启动自动恢复；随时点右上角「📂 打开工作区」切换到其它代码库。

## 配置

LLM 网关从**本项目根目录**的 `.env` 读取（参见 `.env.example`），OpenAI 兼容格式：

```
JD_LLM_API_KEY=...
JD_LLM_BASE_URL=http://llm-gw.jd.local/v1
XIAOSHU_MODEL=DeepSeek-V4-Pro-joybuilder
```

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODELEARN_TARGET` | 无 | 可选，启动时的初始工作区（也可启动后在 UI 里打开） |
| `CODELEARN_LANG` | `zh` | 讲解语言（`zh`/`en`，前端也可切换） |
| `CODELEARN_CHUNK_LINES` | `400` | 大文件分块窗口行数 |
| `CODELEARN_PORT` | `43187` | 服务端口 |

## 缓存

讲解结果按「相对路径 + 内容哈希 + 语言」缓存到 `.cache/<仓库名>-<路径哈希>/`，
文件内容变化即自动失效重算。二次打开同一文件秒开。不同工作区各自独立缓存，互不干扰。

## 架构

- `backend/` — FastAPI 服务
  - `repo_scanner.py` 安全路径解析 / 目录树 / 语言识别 / 紧凑 repo map
  - `roadmap.py` 路线图生成（结构 + 文档种子）
  - `annotator.py` 单文件分块讲解（绝对行号锚定，块连续性校验）
  - `folder_learn.py` 文件夹级模块导读
  - `llm_client.py` OpenAI 兼容客户端（重试 + JSON 容错解析）
  - `cache.py` 磁盘缓存
- `frontend/` — 无构建步骤的原生单页应用（highlight.js 高亮）
