"""Regression checks for recoverable field-profile save failures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.service_v3 import WorkbenchServiceV3

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def _new_field_app(database: Path, monkeypatch) -> tuple[AppTest, str]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.setenv("NONGSHI_DEFAULT_MODE", "real")
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("保存异常重试地块")
    return app, field_id


def test_database_lock_during_field_save_shows_recoverable_message(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"

    def fail_to_write(self, draft, **kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(WorkbenchServiceV3, "save_field_draft", fail_to_write)
    app, field_id = _new_field_app(database, monkeypatch)
    name = app.text_input(key=f"field_name:{field_id}:0").value
    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    visible_errors = "\n".join(element.value for element in app.error)
    assert "本地数据暂时无法写入" in visible_errors
    assert "database is locked" not in visible_errors
    assert app.text_input(key=f"field_name:{field_id}:0").value == name


def test_english_value_error_during_field_save_is_localized(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"

    def fail_to_write(self, draft, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("invalid date format")

    monkeypatch.setattr(WorkbenchServiceV3, "save_field_draft", fail_to_write)
    app, field_id = _new_field_app(database, monkeypatch)
    name = app.text_input(key=f"field_name:{field_id}:0").value
    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    visible_errors = "\n".join(element.value for element in app.error)
    assert "没有保存" in visible_errors
    assert "已输入的信息仍保留" in visible_errors
    assert "invalid date format" not in visible_errors
    assert app.text_input(key=f"field_name:{field_id}:0").value == name


def test_unexpected_english_save_error_is_hidden_and_retry_succeeds(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    original_save = WorkbenchServiceV3.save_field_draft
    attempts = 0

    def fail_once(self, draft, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("INTERNAL_ERROR: unexpected English implementation detail")
        return original_save(self, draft, **kwargs)

    monkeypatch.setattr(WorkbenchServiceV3, "save_field_draft", fail_once)
    app, field_id = _new_field_app(database, monkeypatch)
    name = app.text_input(key=f"field_name:{field_id}:0").value
    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    visible_errors = "\n".join(element.value for element in app.error)
    assert "没有保存" in visible_errors
    assert "已输入的信息仍保留" in visible_errors
    assert "INTERNAL_ERROR" not in visible_errors
    assert app.text_input(key=f"field_name:{field_id}:0").value == name
    assert WorkbenchStoreV3(database).list_fields(mode="real") == []

    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    assert WorkbenchStoreV3(database).get_field(field_id).draft.profile.name == name
