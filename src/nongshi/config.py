"""从根目录 .env 或进程环境读取服务配置；DeepSeek 默认可改配。"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NONGSHI_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = 20.0
    weather_timeout_seconds: float = 8.0
    weather_cache_hours: float = 6.0
    database_path: Path = PROJECT_ROOT / "data" / "local" / "plans.sqlite3"
