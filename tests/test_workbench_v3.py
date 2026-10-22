"""Durable field facts, previews, and adopted plans share one service boundary."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FieldProfile, TimeWindow
from nongshi.schema_v3 import (
    FactEventInputV3,
    FieldDraftV3,
    WeatherDayV3,
    WeatherLocationV3,
    WeatherSnapshotV3,
)
from nongshi.service_v3 import WorkbenchServiceV3


def make_service(path: Path) -> WorkbenchServiceV3:
    return WorkbenchServiceV3(
        WorkbenchStoreV3(path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 18),
    )


def draft(field_id: str = "field-1") -> FieldDraftV3:
    profile = FieldProfile(
        id=field_id,
        name="一号田",
        province="湖北",
        province_confirmed=True,
        crop="油菜",
        crop_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type="lowland_rice_stubble",
        inspection_requested=True,
        inspection_duration_minutes=30,
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
    return FieldDraftV3(field_id=field_id, profile=profile, start_date=start, days=days)


def test_blank_field_draft_survives_reopen_and_conflicting_edits_are_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    first = make_service(database).save_field_draft(
        FieldDraftV3(field_id="field-1"), expected_version=None, operation_id="create-field"
    )
    assert first.draft.profile is None

    service = make_service(database)
    saved = service.save_field_draft(
        draft(), expected_version=first.draft_version, operation_id="save-field"
    )
    assert make_service(database).get_field("field-1") == saved

    with pytest.raises(ValueError, match="版本冲突"):
        service.save_field_draft(
            draft(), expected_version=first.draft_version, operation_id="stale-edit"
        )


def test_confirmed_fact_is_saved_idempotently_before_any_plan_exists(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    event = FactEventInputV3(
        event_id="event-observation-1",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "ditch_connectivity",
            "value": "impaired",
        },
    )

    saved = service.commit_events(
        "field-1", [event], operation_id="feedback-1", expected_fact_version=0
    )
    replayed = service.commit_events(
        "field-1", [event], operation_id="feedback-1", expected_fact_version=0
    )

    assert saved.fact_version == 1
    assert replayed.replayed is True
    assert len(service.list_events("field-1")) == 1
    assert service.get_current_plan("field-1") is None


def test_plan_preview_must_be_adopted_and_stale_preview_cannot_overwrite_facts(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1")
    assert service.get_current_plan("field-1") is None

    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="event-water-1",
                feedback={
                    "kind": "observation",
                    "at": "2026-10-18T10:00:00+08:00",
                    "plot_id": "field-1",
                    "field": "standing_water",
                    "value": True,
                },
            )
        ],
        operation_id="feedback-water",
        expected_fact_version=0,
    )

    with pytest.raises(ValueError, match="预览已过期"):
        service.adopt_plan(
            preview.id,
            expected_fact_version=0,
            expected_plan_id=None,
            operation_id="adopt-stale",
        )
    assert len(service.list_events("field-1")) == 1
    assert service.get_current_plan("field-1") is None


def test_fact_survives_cancelled_preview_and_marks_the_old_plan_stale(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    first_preview = service.preview_plan("field-1")
    first = service.adopt_plan(
        first_preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    ).version
    event = FactEventInputV3(
        event_id="event-condition-1",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    )
    service.commit_events(
        "field-1", [event], operation_id="feedback-condition", expected_fact_version=0
    )
    second_preview = service.preview_plan("field-1")
    service.cancel_preview(second_preview.id)

    refreshed = make_service(tmp_path / "workbench.sqlite3")
    state = refreshed.get_field("field-1")
    assert state.fact_version == 1
    assert state.plan_stale is True
    assert refreshed.get_current_plan("field-1").id == first.id
    assert len(refreshed.list_events("field-1")) == 1


def test_unchanged_recalculation_does_not_create_an_empty_plan_version(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    first_preview = service.preview_plan("field-1")
    first = service.adopt_plan(
        first_preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    ).version
    second_preview = service.preview_plan("field-1")
    result = service.adopt_plan(
        second_preview.id,
        expected_fact_version=0,
        expected_plan_id=first.id,
        operation_id="adopt-unchanged",
    )

    assert result.created_new_version is False
    assert result.version.id == first.id
    assert service.plan_history("field-1") == [first]


def test_adoption_is_append_only_and_repeated_operation_returns_same_version(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1")

    adopted = service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    )
    replayed = service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    )

    assert adopted.version.id == replayed.version.id
    assert replayed.replayed is True
    assert service.get_current_plan("field-1").id == adopted.version.id
    assert service.plan_history("field-1") == [adopted.version]


def test_weather_and_rule_adoption_records_are_separate_from_source_cards(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service.record_rule_adoption(
        "field-1", "C11", status="pending_review", reviewer="许继哲", notes="待复核"
    )
    adoption = service.list_rule_adoptions("field-1")[0]
    assert adoption.rule_id == "C11"
    assert adoption.status == "pending_review"
    assert load_rule_catalog().cards["C11"].runtime_enabled is False


def test_rule_adoption_status_shapes_evidence_before_planning(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service.record_rule_adoption("field-1", "C23", status="rejected", reviewer="许继哲")
    service.record_rule_adoption("field-1", "C31", status="adopted", reviewer="许继哲")

    evaluation = service.evaluate_field("field-1")
    items = {item.task_id: item for item in evaluation.items}
    maintenance_rules = {evidence.rule_id for evidence in items["field-1-M"].evidence}
    inspection = {evidence.rule_id: evidence for evidence in items["field-1-I"].evidence}

    assert "C23" not in maintenance_rules
    assert inspection["C31"].status_label == "已采用（限用途）"
    assert inspection["C11"].status_label == "限用途采用（负责人确认，待独立复核）"
    assert "暂缓" in inspection["C19"].status_label

    preview = service.preview_plan("field-1")
    assert preview.version.rule_adoption_ids
    assert any("暂缓规则不作为计划依据" in basis for basis in preview.version.calculation_basis)


def test_standing_water_adds_onsite_verification_basis_without_threshold(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1")
    assert not any("农技人员" in basis for basis in preview.version.calculation_basis)

    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="obs-standing-water",
                feedback={
                    "kind": "observation",
                    "at": "2026-10-18T10:00:00+08:00",
                    "plot_id": "field-1",
                    "field": "standing_water",
                    "value": True,
                },
            )
        ],
        operation_id="obs-water",
        expected_fact_version=0,
    )
    preview = service.preview_plan("field-1")
    assert any("农技人员" in basis for basis in preview.version.calculation_basis)


def test_growth_observation_events_validate_and_survive_reopen(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    with pytest.raises(ValueError, match="pest_signs"):
        FactEventInputV3(
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "pest_signs",
                "value": "少量",
            }
        )
    events = [
        FactEventInputV3(
            event_id=f"obs-{field}",
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": field,
                "value": True,
            },
        )
        for field in ("pest_signs", "missing_seedlings", "crowding")
    ]
    service.commit_events("field-1", events, operation_id="obs-growth", expected_fact_version=0)

    reloaded = make_service(tmp_path / "workbench.sqlite3")
    assert [event.feedback.field for event in reloaded.list_events("field-1")] == [
        "pest_signs",
        "missing_seedlings",
        "crowding",
    ]
    evaluation = reloaded.evaluate_field("field-1")
    assert evaluation.fact_version == 1
    maintenance = next(item for item in evaluation.items if item.task_type == "maintenance")
    assert maintenance.necessity == "unknown"


def test_expired_weather_snapshot_makes_preview_stale_and_blocks_adoption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    now = [datetime(2026, 10, 18, 8, tzinfo=UTC)]
    store = WorkbenchStoreV3(database)
    service = WorkbenchServiceV3(
        store,
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 18),
        now=lambda: now[0],
    )
    location = WeatherLocationV3(
        query="武汉市洪山区",
        display_name="湖北省 · 武汉市 · 洪山区",
        latitude=30.5,
        longitude=114.3,
        resolution="county",
    )
    saved_draft = draft().model_copy(update={"location": location})
    service.save_field_draft(
        saved_draft, expected_version=None, operation_id="create-with-location"
    )
    snapshot = WeatherSnapshotV3(
        field_id="field-1",
        location=location,
        requested_latitude=location.latitude,
        requested_longitude=location.longitude,
        grid_latitude=30.51,
        grid_longitude=114.31,
        fetched_at=now[0],
        coverage_start=date(2026, 10, 19),
        coverage_end=date(2026, 10, 25),
        units={},
        days=[WeatherDayV3(day=date(2026, 10, 19) + timedelta(days=i)) for i in range(7)],
        cache_expires_at=now[0] + timedelta(hours=1),
    )
    service.store_weather(snapshot)
    preview = service.preview_plan("field-1", operation_id="preview-with-weather")
    assert preview.version.weather_snapshot_id == snapshot.id

    now[0] += timedelta(hours=2)
    with pytest.raises(ValueError, match="天气快照已过期"):
        service.adopt_plan(
            preview.id,
            expected_fact_version=0,
            expected_plan_id=None,
            operation_id="adopt-expired-weather",
        )

    assert store.load_preview(preview.id).status == "stale"
    assert service.get_current_plan("field-1") is None


def test_forecast_rain_never_becomes_an_observed_fact(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    location = WeatherLocationV3(
        query="武汉市洪山区",
        display_name="湖北省 · 武汉市 · 洪山区",
        latitude=30.5,
        longitude=114.3,
        resolution="county",
    )
    service.save_field_draft(
        draft().model_copy(update={"location": location}),
        expected_version=None,
        operation_id="create-with-location",
    )
    service.store_weather(
        WeatherSnapshotV3(
            field_id="field-1",
            location=location,
            requested_latitude=location.latitude,
            requested_longitude=location.longitude,
            grid_latitude=30.51,
            grid_longitude=114.31,
            fetched_at=datetime(2026, 10, 18, 8, tzinfo=UTC),
            coverage_start=date(2026, 10, 19),
            coverage_end=date(2026, 10, 25),
            units={"precipitation_sum": "mm"},
            days=[
                WeatherDayV3(
                    day=date(2026, 10, 19) + timedelta(days=offset),
                    precipitation_sum=25.0,
                )
                for offset in range(7)
            ],
            cache_expires_at=datetime(2026, 10, 18, 20, tzinfo=UTC),
        )
    )

    state = service.get_field("field-1")
    assert state.draft.profile is not None
    assert state.draft.profile.actual_rain_observed is None

    evaluation = service.evaluate_field("field-1")
    rule_ids = {row.rule_id for item in evaluation.items for row in item.evidence}
    assert "C24" not in rule_ids


def test_adopting_another_field_plan_leaves_the_first_field_plan_untouched(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft("field-1"), expected_version=None, operation_id="create-1")
    preview_one = service.preview_plan("field-1", operation_id="preview-1")
    adopted_one = service.adopt_plan(
        preview_one.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-1",
    ).version

    service.save_field_draft(draft("field-2"), expected_version=None, operation_id="create-2")
    preview_two = service.preview_plan("field-2", operation_id="preview-2")
    service.adopt_plan(
        preview_two.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-2",
    )

    assert service.get_current_plan("field-1").id == adopted_one.id
    markdown = service.export_markdown("field-1")
    assert "单地块；尚未统筹其他地块时间" in markdown
    assert "不代表合并后可执行" in markdown


def test_duplicate_or_unknown_completion_event_is_rejected_without_new_version(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1", operation_id="preview")
    adopted = service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt",
    ).version
    inspection = next(item for item in adopted.items if item.task_type == "inspection")

    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="complete-1",
                feedback={
                    "kind": "completed",
                    "task_id": inspection.task_id,
                    "at": (inspection.end_at + timedelta(minutes=1)).isoformat(),
                },
            )
        ],
        operation_id="complete-1",
        expected_fact_version=0,
    )
    history_after_first = len(service.plan_history("field-1"))

    with pytest.raises(ValueError, match="已记录完成"):
        service.commit_events(
            "field-1",
            [
                FactEventInputV3(
                    event_id="complete-2",
                    feedback={
                        "kind": "completed",
                        "task_id": inspection.task_id,
                        "at": (inspection.end_at + timedelta(minutes=2)).isoformat(),
                    },
                )
            ],
            operation_id="complete-2",
            expected_fact_version=1,
        )
    assert len(service.plan_history("field-1")) == history_after_first


def test_exports_carry_mode_version_tasks_basis_and_reasons(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1", operation_id="preview")
    service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt",
    )

    payload = json.loads(service.export_json("field-1"))
    assert payload["schema_version"] == 3
    assert payload["adopted_plan"] is not None
    assert payload["adopted_plan"]["id"] == preview.version.id
    assert payload["field"]["draft"]["mode"] == "real"
    assert payload["adopted_plan"]["plan"]["items"], "导出必须包含任务明细"
    assert payload["history"], "导出必须包含已采用版本历史"

    markdown = service.export_markdown("field-1")
    assert "开发验证，适用性待复核" in markdown
    assert "单地块；尚未统筹其他地块时间" in markdown
    assert "不代表合并后可执行" in markdown
    assert "## 七天安排" in markdown
    assert "依据：" in markdown
    assert "任务：" in markdown
    assert "## 缺项和未排入原因" in markdown


def test_feedback_values_are_field_validated_before_any_fact_is_saved(
    tmp_path: Path,
) -> None:
    from nongshi.schema_v3 import FactEventInputV3

    with pytest.raises(ValueError, match="沟渠连通情况"):
        FactEventInputV3(
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "ditch_connectivity",
                "value": "堵塞程度很严重",
            }
        )
    with pytest.raises(ValueError, match="叶片数不能小于零"):
        FactEventInputV3(
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "leaf_count",
                "value": -1,
            }
        )


def test_correction_keeps_original_event_and_recomputes_from_corrected_fact(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    original = FactEventInputV3(
        event_id="water-observation-original",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    )
    service.commit_events(
        "field-1", [original], operation_id="event-original", expected_fact_version=0
    )
    correction = FactEventInputV3(
        event_id="water-observation-correction",
        correction_of=original.event_id,
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:05:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": False,
        },
    )
    service.commit_events(
        "field-1", [correction], operation_id="event-correction", expected_fact_version=1
    )

    events = service.list_events("field-1")
    assert [event.event_id for event in events] == [
        "water-observation-original",
        "water-observation-correction",
    ]
    assert service._active_events("field-1") == [events[1]]
    assert service.evaluate_field("field-1").fact_version == 2


def test_a_failed_preview_does_not_rollback_previously_committed_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    event = FactEventInputV3(
        event_id="saved-before-calculation",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    )
    service.commit_events(
        "field-1", [event], operation_id="commit-before-preview", expected_fact_version=0
    )

    def fail_evaluation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated calculation failure")

    monkeypatch.setattr(service, "_evaluate_request", fail_evaluation)
    with pytest.raises(RuntimeError, match="simulated calculation failure"):
        service.preview_plan("field-1")

    assert service.get_field("field-1").fact_version == 1
    assert service.list_events("field-1")[0].event_id == event.event_id
    assert service.get_current_plan("field-1") is None


def test_fourth_field_keeps_its_draft_events_and_plan_independent(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    for index in range(4):
        field_id = f"field-{index + 1}"
        service.save_field_draft(
            draft(field_id).model_copy(
                update={
                    "profile": draft(field_id).profile.model_copy(
                        update={"name": f"第{index + 1}块地"}
                    )
                }
            ),
            expected_version=None,
            operation_id=f"create-{field_id}",
        )
    fourth = service.get_field("field-4")
    assert fourth.draft.profile.name == "第4块地"
    assert service.get_field("field-1").draft.profile.name == "第1块地"
    assert len(service.list_fields(mode="real")) == 4


def test_completed_maintenance_remains_historical_when_a_new_need_is_confirmed(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    new_draft = draft().model_copy(
        update={
            "profile": draft().profile.model_copy(
                update={
                    "inspection_requested": False,
                    "maintenance_need_confirmed": True,
                    "maintenance_problem_confirmed": True,
                    "maintenance_duration_minutes": 60,
                }
            )
        }
    )
    service.save_field_draft(new_draft, expected_version=None, operation_id="create-field")
    first = service.preview_plan("field-1", operation_id="preview-maintenance-1")
    adopted_v1 = service.adopt_plan(
        first.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-maintenance-1",
    ).version
    prior_task = next(item for item in adopted_v1.items if item.task_type == "maintenance")
    assert prior_task.start_at is not None
    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="maintenance-completed",
                feedback={
                    "kind": "completed",
                    "task_id": prior_task.task_id,
                    "at": (prior_task.end_at + timedelta(minutes=5)).isoformat(),
                },
            )
        ],
        operation_id="complete-maintenance",
        expected_fact_version=0,
    )
    after_completion = service.preview_plan("field-1", operation_id="preview-completed")
    adopted_v2 = service.adopt_plan(
        after_completion.id,
        expected_fact_version=1,
        expected_plan_id=adopted_v1.id,
        operation_id="adopt-completed",
    ).version
    assert next(item for item in adopted_v2.items if item.task_id == prior_task.task_id).completed
    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="maintenance-demand-again",
                feedback={
                    "kind": "observation",
                    "plot_id": "field-1",
                    "field": "maintenance_need_confirmed",
                    "value": True,
                    "at": (prior_task.end_at + timedelta(days=1)).isoformat(),
                },
            )
        ],
        operation_id="new-maintenance-need",
        expected_fact_version=1,
    )

    first_evaluation = service.evaluate_field("field-1")
    second_evaluation = service.evaluate_field("field-1")
    maintenance = [item for item in first_evaluation.items if item.task_type == "maintenance"]
    by_id = {item.task_id: item for item in maintenance}
    assert by_id[prior_task.task_id].completed is True
    assert len(by_id) == 2
    new_ids = set(by_id) - {prior_task.task_id}
    assert len(new_ids) == 1
    assert new_ids == {
        item.task_id
        for item in second_evaluation.items
        if item.task_type == "maintenance" and item.task_id != prior_task.task_id
    }


def test_copying_v1_plan_preserves_unknown_agronomic_inputs_and_source(
    tmp_path: Path,
) -> None:
    from nongshi.demo import make_demo_request
    from nongshi.planning import build_plan
    from nongshi.repository import PlanStore

    database = tmp_path / "legacy.sqlite3"
    legacy_store = PlanStore(database)
    original = build_plan(make_demo_request(date(2026, 10, 19)))
    legacy_store.save(original)

    service = make_service(database)
    copied = service.copy_legacy_plan(original.id, operation_id="copy-v1-once")

    assert copied.draft.input_source == "copied_legacy"
    assert copied.draft.source_plan_id == original.id
    assert copied.draft.profile is not None
    assert copied.draft.profile.crop is None
    assert copied.draft.profile.field_type is None
    assert copied.draft.days == []
    assert legacy_store.load(original.id) == original
