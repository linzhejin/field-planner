"""显示层：候选卡用中文状态胶囊，主界面不直接暴露英文枚举或任务编号。"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from nongshi.schema_v2 import FeedbackV2, TimeWindow
from nongshi.schema_v3 import FactEventInputV3
from nongshi.workbench import _proposal_event_description

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def test_feedback_proposal_shows_exact_resource_change_before_confirmation() -> None:
    event = FactEventInputV3(
        feedback=FeedbackV2(
            kind="resource",
            at=datetime(2026, 10, 24, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai")),
            day=date(2026, 10, 25),
            resource_minutes=60,
            intervals=[TimeWindow(start=time(14), end=time(15))],
        )
    )

    description = _proposal_event_description(event)

    assert "2026-10-25" in description
    assert "14:00–15:00" in description
    assert "60 分钟" in description
    assert "人员" in description


def _render(tmp_path: Path, monkeypatch) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("验收一号田")
    app.text_input(key=f"field_province:{field_id}:0").set_value("湖北")
    app.checkbox(key=f"province_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_crop:{field_id}:0").set_value("油菜")
    app.checkbox(key=f"crop_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_stage:{field_id}:0").set_value("苗期")
    app.checkbox(key=f"stage_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_type:{field_id}:0").set_value("低洼/稻茬田")
    app.selectbox(key=f"inspection_requested:{field_id}:0").set_value("是")
    app.number_input(key=f"inspection_minutes:{field_id}:0").set_value(30)
    app.button(key=f"save_field:{field_id}:0").click().run()
    return app


def test_candidate_card_uses_chinese_status_chip(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app = _render(tmp_path, monkeypatch)
    assert not app.exception

    markdown = "\n".join(element.value for element in app.markdown)
    captions = "\n".join(element.value for element in app.caption)
    joined = markdown + "\n" + captions

    # 状态用带颜色的中文胶囊呈现。
    assert "status-chip" in markdown
    assert "status-green" in markdown or "status-amber" in markdown or "status-red" in markdown

    # 主界面不再直接显示英文枚举或反引号任务编号。
    for token in ("适用性：matched", "适用性：pending", "需求：required", "需求：no_need"):
        assert token not in joined
    assert "`field-" not in markdown

    # 四层状态与任务编号移入「查看详情」对话框，主卡片只保留可扫描的摘要。
    assert any(button.label == "查看详情" for button in app.button)
    assert not re.search(r"任务 (巡田检查|沟渠维护)", joined)
    assert not re.search(r"适用性 (适用|待确认|超出范围)", joined)
    assert not re.search(r"窗口 (可安排|暂不可安排|待确认|未提供)", joined)
    assert not re.search(r"编号 field-", joined)
