"""配置解析：.env + 环境变量，零依赖。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_RUNTIME_DIR = REPO_ROOT / "runtime"

VALID_MODES = ("auto", "mock", "llm")


class ConfigError(RuntimeError):
    pass


def load_env_file(path: Path) -> Dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，不处理引号转义。"""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class Config:
    mode: str
    api_key: str
    base_url: str
    model: str
    timeout: float
    data_dir: Path
    runtime_dir: Path

    @property
    def uses_llm(self) -> bool:
        return self.mode == "llm"

    def public_dict(self) -> Dict[str, str]:
        """可安全打印的配置摘要（绝不含 key）。"""
        return {
            "mode": self.mode,
            "base_url": self.base_url,
            "model": self.model if self.uses_llm else "n/a",
        }


def resolve_config(
    mode_override: Optional[str] = None,
    data_dir: Optional[Path] = None,
    runtime_dir: Optional[Path] = None,
    env_file: Optional[Path] = None,
) -> Config:
    file_env = load_env_file(env_file or (REPO_ROOT / ".env"))

    def pick(name: str, default: str = "") -> str:
        # 环境变量优先于 .env，便于 CI / 临时覆盖
        return os.environ.get(name) or file_env.get(name) or default

    requested = (mode_override or pick("AGENT_MODE", "auto")).strip().lower()
    if requested not in VALID_MODES:
        raise ConfigError(f"AGENT_MODE 非法：{requested}（可选 auto/mock/llm）")

    api_key = pick("DEEPSEEK_API_KEY")
    if requested == "auto":
        mode = "llm" if api_key else "mock"
    else:
        mode = requested

    if mode == "llm" and not api_key:
        raise ConfigError("mode=llm 但缺少 DEEPSEEK_API_KEY；请写入 .env 或改用 --mode mock")

    return Config(
        mode=mode,
        api_key=api_key,
        base_url=pick("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        model=pick("MODEL_NAME", "deepseek-flash"),
        timeout=float(pick("MODEL_TIMEOUT", "60")),
        data_dir=Path(data_dir) if data_dir else Path(pick("DATA_DIR") or DEFAULT_DATA_DIR),
        runtime_dir=Path(runtime_dir) if runtime_dir else Path(pick("RUNTIME_DIR") or DEFAULT_RUNTIME_DIR),
    )
