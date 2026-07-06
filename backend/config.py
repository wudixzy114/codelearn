"""集中配置：加载 .env、解析目标代码库路径与运行期设置。

.env 位于 tools/ 根目录（本工具的上一级），格式为京东 OpenAI 兼容网关：
    JD_LLM_API_KEY / JD_LLM_BASE_URL / XIAOSHU_MODEL
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 目录锚点
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent               # codelearn/
TOOLS_DIR = PROJECT_DIR.parent                 # tools/  —— .env 所在处
FRONTEND_DIR = PROJECT_DIR / "frontend"
CACHE_ROOT = PROJECT_DIR / ".cache"

# 加载 .env：优先 tools/.env，回退 codelearn/.env
for env_path in (TOOLS_DIR / ".env", PROJECT_DIR / ".env"):
    if env_path.exists():
        load_dotenv(env_path)
        break


def _resolve_target() -> Path:
    """目标代码库路径：环境变量 CODELEARN_TARGET 优先，默认同级 xllm。"""
    raw = os.getenv("CODELEARN_TARGET")
    if raw:
        return Path(raw).expanduser().resolve()
    return (TOOLS_DIR / "xllm").resolve()


class Settings:
    """全局设置对象，进程内单例（见文件底部 settings）。"""

    def __init__(self) -> None:
        self.target_repo: Path = _resolve_target()
        # 讲解语言，可配置，默认中文
        self.language: str = os.getenv("CODELEARN_LANG", "zh").lower()

        # LLM 网关
        self.llm_api_key: Optional[str] = os.getenv("JD_LLM_API_KEY")
        self.llm_base_url: Optional[str] = os.getenv("JD_LLM_BASE_URL")
        self.llm_model: str = os.getenv("XIAOSHU_MODEL", "DeepSeek-V4-Pro")

        # 分块参数（大文件逐块讲解）
        self.chunk_lines: int = int(os.getenv("CODELEARN_CHUNK_LINES", "400"))
        self.chunk_overlap: int = int(os.getenv("CODELEARN_CHUNK_OVERLAP", "20"))
        # 单次讲解可处理的最大文件行数（超大文件截断保护）
        self.max_file_lines: int = int(os.getenv("CODELEARN_MAX_FILE_LINES", "6000"))

    @property
    def cache_dir(self) -> Path:
        """该目标库专属缓存目录：.cache/<repo 名>/"""
        d = CACHE_ROOT / self.target_repo.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set_language(self, lang: str) -> None:
        if lang in ("zh", "en"):
            self.language = lang

    def as_public_dict(self) -> dict:
        """暴露给前端的安全配置（不含密钥）。"""
        return {
            "target_repo": str(self.target_repo),
            "target_name": self.target_repo.name,
            "language": self.language,
            "model": self.llm_model,
            "llm_configured": bool(self.llm_api_key and self.llm_base_url),
        }


settings = Settings()
