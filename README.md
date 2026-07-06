# CodeLearn — 大型代码库快速学习工具

用 AI 帮你快速吃透一个陌生的大型代码库：

1. **学习路线图**：AI 分析整个项目，产出一条有序的学习路线，每一步列出需要阅读的具体文件（可点击直达）。
2. **逐块讲解**：打开任意文件，AI 把它切成语义块，逐块给出「做什么 + 为什么 + 注意点」的中文讲解，左代码右讲解双栏对照。
3. **递归学习**：打开任意文件夹，得到该模块的职责概览、每个文件的一行摘要，以及建议阅读顺序。

工具本身与被学习的代码库解耦，默认学习同级的 `xllm/`，也可指向任意仓库。

## 快速开始

```bash
cd codelearn
pip install -r requirements.txt

# 默认学习 ../xllm
./run.sh

# 或指定任意目标仓库
./run.sh /path/to/your/repo
```

浏览器打开 http://127.0.0.1:8000

## 配置

LLM 网关从 `tools/.env`（本目录上一级）读取，OpenAI 兼容格式：

```
JD_LLM_API_KEY=...
JD_LLM_BASE_URL=http://llm-gw.jd.local/v1
XIAOSHU_MODEL=DeepSeek-V4-Pro-joybuilder
```

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODELEARN_TARGET` | `../xllm` | 目标代码库路径 |
| `CODELEARN_LANG` | `zh` | 讲解语言（`zh`/`en`，前端也可切换） |
| `CODELEARN_CHUNK_LINES` | `400` | 大文件分块窗口行数 |
| `CODELEARN_PORT` | `8000` | 服务端口 |

## 缓存

讲解结果按「相对路径 + 内容哈希 + 语言」缓存到 `.cache/<仓库名>/`，
文件内容变化即自动失效重算。二次打开同一文件秒开。

## 架构

- `backend/` — FastAPI 服务
  - `repo_scanner.py` 安全路径解析 / 目录树 / 语言识别 / 紧凑 repo map
  - `roadmap.py` 路线图生成（结构 + 文档种子）
  - `annotator.py` 单文件分块讲解（绝对行号锚定，块连续性校验）
  - `folder_learn.py` 文件夹级模块导读
  - `llm_client.py` OpenAI 兼容客户端（重试 + JSON 容错解析）
  - `cache.py` 磁盘缓存
- `frontend/` — 无构建步骤的原生单页应用（highlight.js 高亮）
