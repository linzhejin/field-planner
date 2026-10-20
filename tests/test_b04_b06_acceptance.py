"""B04/B06 可复现验收：按工程收尾交接的固定输入核对依据、缺项与导出一致性。"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FeedbackV2, FieldProfile, TimeWindow
from nongshi.schema_v3 import FactEventInputV3, FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3


def _service(path) -> WorkbenchServiceV3:  # type: ignore[no-untyped-def]
    return WorkbenchServiceV3(
        WorkbenchStoreV3(path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 19),
    )


def _fixed_draft() -> FieldDraftV3:
    profile = FieldProfile(
        id="field-1",
        name="验收一号田",
        province="湖北",
        province_confirmed=True,
        crop="油菜",
        crop_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type="lowland_rice_stubble",
        rice_stubble=True,
        cold_waterlogged=False,
        terrain="lowland",
        inspection_requested=True,
        inspection_duration_minutes=30,
        ditch_connectivity="impaired",
        standing_water=True,
        actual_rain_observed=False,
    )
    start = date(2026, 10, 19)
    days = [
        DayWindow(
            day=start + timedelta(days=offset),
            site_workable=True,
            weather_workable=True,
            resource_minutes=120,
            intervals=[TimeWindow(start="09:00", end="11:00")],
        )
        for offset in range(7)
    ]
    return FieldDraftV3(field_id="field-1", profile=profile, start_date=start, days=days)


def test_b04_b06_fixed_input_is_reproducible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "b04.sqlite3"
    service = _service(database)
    service.save_field_draft(_fixed_draft(), expected_version=None, operation_id="create")
    evaluation = service.evaluate_field("field-1")
    items = {item.task_id: item for item in evaluation.items}
    inspection = items["field-1-I"]
    maintenance = items["field-1-M"]

    # 1. 巡田可排；维护在问题与需求确认前为待确认并列出缺项。
    assert inspection.schedule_status == "scheduled"
    assert maintenance.schedule_status == "pending"
    assert maintenance.missing_fields

    # 2. 依据覆盖 C11/C31/C23；A04 补充来源登记可见且不改变原卡。
    evidence_ids = {row.rule_id for item in evaluation.items for row in item.evidence}
    assert {"C11", "C31", "C23"} <= evidence_ids
    supplemental = load_rule_catalog().supplementary_for(["C23"])
    assert supplemental and all(row["source_id"].startswith("A04-") for row in supplemental)
    assert all("不改变原卡" in row["status_label"] for row in supplemental)

    # 3. 积水提示出现，且不出现阈值或固定处置时限。
    preview = service.preview_plan("field-1", operation_id="preview", source="demo")
    basis = "\n".join(preview.version.calculation_basis)
    assert "现场核实" in basis
    assert "阈值" not in basis and "24" not in basis

    # 4. 采用后导出含模式、依据与任务；结构化导出含版本与历史。
    service.adopt_plan(
        preview.id,
        expected_fact_version=preview.expected_fact_version,
        expected_plan_id=preview.expected_plan_id,
        operation_id="adopt",
    )
    markdown = service.export_markdown("field-1")
    assert "单地块；尚未统筹其他地块时间" in markdown
    assert "依据：" in markdown and "任务：" in markdown
    payload = json.loads(service.export_json("field-1"))
    assert payload["schema_version"] == 3
    assert payload["adopted_plan"] is not None
    assert payload["history"]

    # 5. 完成事实保留；重复提交被拒绝且不产生新版本。
    current = service.get_current_plan("field-1")
    action = next(item for item in current.items if item.task_id == "field-1-I")
    state = service.get_field("field-1")
    event = FactEventInputV3(
        feedback=FeedbackV2(kind="completed", task_id="field-1-I", at=action.end_at),
        source="user",
    )
    service.commit_events(
        "field-1", [event], operation_id="complete", expected_fact_version=state.fact_version
    )
    assert len(service.list_events("field-1")) == 1
    with pytest.raises(ValueError):
        service.commit_events(
            "field-1",
            [event.model_copy(update={"event_id": "dup"})],
            operation_id="complete-again",
            expected_fact_version=service.get_field("field-1").fact_version,
        )

    # 6. 重新打开仍保留完成事实与已采用计划。
    reopened = _service(database)
    assert reopened.get_field("field-1").fact_version == 1
    assert len(reopened.list_events("field-1")) == 1
    assert reopened.get_current_plan("field-1") is not None
