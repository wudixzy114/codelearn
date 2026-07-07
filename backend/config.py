"""集中配置：加载 .env、解析目标代码库路径与运行期设置。

.env 位于本项目根目录（codelearn/.env），格式为 OpenAI 兼容网关：
    JD_LLM_API_KEY / JD_LLM_BASE_URL / XIAOSHU_MODEL

目标代码库（工作区）不再于启动时写死：可在 UI 里随时打开任意文件夹，
选择会持久化到 .cache/workspaces.json，跨重启保留。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# 目录锚点
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent               # codelearn/
FRONTEND_DIR = PROJECT_DIR / "frontend"
CACHE_ROOT = PROJECT_DIR / ".cache"
WORKSPACES_FILE = CACHE_ROOT / "workspaces.json"   # 持久化：last + recents

# 加载 .env：仅项目根目录（不再向上一级目录探测）
_env_path = PROJECT_DIR / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# 最近打开的工作区列表上限
_RECENTS_CAP = 12


class WorkspaceError(ValueError):
    """打开工作区失败：路径为空、不存在、非目录或不可读。"""


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def validate_workspace(raw: str) -> Path:
    """把用户给的路径校验成一个可用的工作区绝对路径。

    规则：非空 → 展开 ~ 与相对 → 必须存在 → 必须是目录 → 必须可读。
    失败抛 WorkspaceError（携带中文可读信息）。
    """
    if not raw or not str(raw).strip():
        raise WorkspaceError("路径为空")
    p = Path(str(raw).strip()).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise WorkspaceError(f"无法解析路径：{e}")
    if not p.exists():
        raise WorkspaceError(f"路径不存在：{p}")
    if not p.is_dir():
        raise WorkspaceError(f"不是文件夹：{p}")
    if not os.access(p, os.R_OK):
        raise WorkspaceError(f"文件夹不可读：{p}")
    return p


class Settings:
    """全局设置对象，进程内单例（见文件底部 settings）。"""

    def __init__(self) -> None:
        # 持久化的 last / recents
        self._recents: List[str] = []
        last = self._load_persisted()

        # 目标代码库（工作区）：CODELEARN_TARGET > 持久化的 last > 无（None）
        self.target_repo: Optional[Path] = self._initial_target(last)

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

    # ---- 初始工作区解析 --------------------------------------------------

    def _initial_target(self, last: Optional[str]) -> Optional[Path]:
        """启动时的初始工作区：环境变量优先，其次上次打开的，都没有则 None。"""
        raw = os.getenv("CODELEARN_TARGET")
        if raw:
            try:
                return validate_workspace(raw)
            except WorkspaceError:
                pass  # 环境变量无效则退回持久化 / 空
        if last:
            try:
                return validate_workspace(last)
            except WorkspaceError:
                pass  # 上次的目录已失效
        return None

    # ---- 持久化：last + recents -----------------------------------------

    def _load_persisted(self) -> Optional[str]:
        """读取 .cache/workspaces.json，剔除已失效的 recents，返回 last。"""
        try:
            data = json.loads(WORKSPACES_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        recents = data.get("recents") or []
        self._recents = [r for r in recents if isinstance(r, str) and Path(r).is_dir()]
        last = data.get("last")
        return last if isinstance(last, str) else None

    def _persist(self) -> None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        payload = {
            "last": str(self.target_repo) if self.target_repo else None,
            "recents": self._recents,
        }
        try:
            WORKSPACES_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
            )
        except OSError:
            pass  # 持久化失败不应中断请求

    # ---- 运行期切换工作区 ------------------------------------------------

    def open_workspace(self, raw: str) -> Path:
        """校验并切换到新工作区，更新 recents/last 并落盘。返回绝对路径。"""
        p = validate_workspace(raw)
        self.target_repo = p
        s = str(p)
        # 置顶去重
        self._recents = [s] + [r for r in self._recents if r != s]
        self._recents = self._recents[:_RECENTS_CAP]
        self._persist()
        return p

    @property
    def recents(self) -> List[str]:
        return list(self._recents)

    @property
    def cache_dir(self) -> Path:
        """该目标库专属缓存目录：.cache/<repo 名>-<绝对路径短哈希>/

        用短哈希消歧，避免不同路径下的同名目录（/a/proj 与 /b/proj）撞车。
        """
        if self.target_repo is None:
            raise WorkspaceError("尚未打开工作区")
        name = f"{self.target_repo.name}-{_short_hash(str(self.target_repo))}"
        d = CACHE_ROOT / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set_language(self, lang: str) -> None:
        if lang in ("zh", "en"):
            self.language = lang

    def as_public_dict(self) -> dict:
        """暴露给前端的安全配置（不含密钥）。"""
        return {
            "target_repo": str(self.target_repo) if self.target_repo else None,
            "target_name": self.target_repo.name if self.target_repo else "",
            "has_workspace": self.target_repo is not None,
            "recents": self.recents,
            "language": self.language,
            "model": self.llm_model,
            "llm_configured": bool(self.llm_api_key and self.llm_base_url),
        }


settings = Settings()
