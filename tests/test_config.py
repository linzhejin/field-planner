"""配置入口应可离线使用，并避免把密钥写进诊断结果。"""

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
        monkeypatch.delenv(f"NONGSHI_LLM_{key}", raising=False)


def load_config_module() -> ModuleType:
    assert importlib.util.find_spec("nongshi.config") is not None, "Configuration not implemented"
    return importlib.import_module("nongshi.config")


def test_deepseek_defaults_are_ready_but_credentials_remain_empty() -> None:
    config_module = load_config_module()
    settings = config_module.Settings(_env_file=None)
    assert settings.llm_provider == "deepseek"
    assert settings.llm_model == "deepseek-flash"
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_api_key.get_secret_value() == ""


def test_local_env_file_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_module = load_config_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NONGSHI_LLM_PROVIDER=example\nNONGSHI_LLM_MODEL=from-file\n"
        "NONGSHI_LLM_API_KEY=test-only-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NONGSHI_LLM_MODEL", "from-environment")
    settings = config_module.Settings(_env_file=env_file)
    assert settings.llm_provider == "example"
    assert settings.llm_model == "from-environment"
    assert settings.llm_api_key.get_secret_value() == "test-only-value"


def test_secret_is_redacted_from_representation() -> None:
    config_module = load_config_module()
    settings = config_module.Settings(_env_file=None, llm_api_key="test-only-secret")
    assert "test-only-secret" not in repr(settings)
    assert "test-only-secret" not in settings.model_dump_json()
