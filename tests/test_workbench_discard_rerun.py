from unittest.mock import Mock

from nongshi import workbench


def test_discard_editor_requests_full_app_rerun(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    field_id = "field-1"
    widget_key = "field_name:field-1:2"
    state = {
        f"edit_buffer:field:{field_id}": {
            "values": {widget_key: "尚未保存的名字"},
            "baseline": {widget_key: "原始地块名"},
            "dirty": True,
        },
        widget_key: "尚未保存的名字",
    }
    rerun = Mock()
    monkeypatch.setattr(workbench.st, "session_state", state)
    monkeypatch.setattr(workbench.st, "rerun", rerun)

    workbench._discard_editor("field", field_id)

    assert widget_key not in state
    assert f"edit_buffer:field:{field_id}" not in state
    assert state[f"editor_generation:field:{field_id}"] == 1
    rerun.assert_called_once_with(scope="app")
