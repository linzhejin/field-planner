"""环境检查只报告本地结果，不调用外部服务。"""

import importlib
import importlib.util
import json

import pytest


def test_diagnostic_checks_dependencies_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert importlib.util.find_spec("nongshi.check_env") is not None, "环境自检尚未实现"
    monkeypatch.setenv("NONGSHI_LLM_API_KEY", "test-only-diagnostic-secret")
    module = importlib.import_module("nongshi.check_env")
    report = module.check_environment()
    assert report["sqlite_write_read"] == "ok"
    assert report["virtual_environment"] is True
    assert report["external_services_tested"] is False
    assert report["packages"]["streamlit"]
    assert "test-only-diagnostic-secret" not in json.dumps(report)
