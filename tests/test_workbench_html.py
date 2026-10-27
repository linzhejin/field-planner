from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.tasks_v4 import TaskSeriesV4, TaskTemplateV4
from nongshi.workbench_ui_protocol import UIAction
from nongshi.workbench_web import build_workbench_view, dispatch_ui_action


def _service(tmp_path):
    return WorkbenchServiceV3(WorkbenchStoreV3(tmp_path / "workbench.sqlite3"))


def test_blank_first_view_does_not_require_a_saved_field(tmp_path) -> None:
    service = _service(tmp_path)

    view = build_workbench_view(
        service,
        "real",
        "field-new",
        None,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={},
    )

    assert view.field is None
    assert view.fields == []
    assert view.start_date == date(2026, 10, 28)
    assert len(view.days) == 7


def test_unsaved_profile_draft_is_restored_for_blank_field(tmp_path) -> None:
    service = _service(tmp_path)
    session = {}
    draft_result = dispatch_ui_action(
        UIAction(
            operation_id="save-blank-profile-draft-001",
            kind="save_profile_draft",
            field_id="field-unsaved-profile",
            payload={"name": "尚未建档的地块", "form_kind": "profile"},
        ),
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id="field-unsaved-profile",
        current_state=None,
        session=session,
    )

    view = build_workbench_view(
        service,
        "real",
        "field-unsaved-profile",
        None,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={},
    )
    assert draft_result.outcome == "draft_saved"
    assert view.editor_drafts["profile"]["payload"]["name"] == "尚未建档的地块"
    restarted_store = WorkbenchStoreV3(tmp_path / "workbench.sqlite3")
    assert restarted_store.latest_unsaved_profile_draft_field_id("real") == "field-unsaved-profile"
    _save_field(WorkbenchServiceV3(restarted_store), "field-unsaved-profile")
    assert restarted_store.latest_unsaved_profile_draft_field_id("real") is None


def test_saved_resource_draft_is_restored_for_existing_field(tmp_path) -> None:
    service = _service(tmp_path)
    state = _save_field(service, "field-resource-draft")
    action = UIAction(
        operation_id="save-resource-draft-0001",
        kind="save_profile_draft",
        field_id=state.id,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        expected_task_version=state.task_version,
        payload={"form_kind": "resource", "day": "2026-10-27", "intervals": "13:00-15:00"},
    )

    saved = dispatch_ui_action(
        action,
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )
    view = build_workbench_view(
        service,
        "real",
        state.id,
        state,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={},
    )

    assert saved.outcome == "draft_saved"
    assert view.editor_drafts["resource"]["payload"]["intervals"] == "13:00-15:00"


def test_location_search_keeps_weather_panel_open_for_candidate_selection(tmp_path) -> None:
    from nongshi.schema_v3 import GeocodingResultV3

    service = _service(tmp_path)
    state = _save_field(service, "field-weather-search")
    database = tmp_path / "workbench.sqlite3"
    session = {}
    candidate = GeocodingResultV3(
        id="online-wuhan",
        name="武汉市",
        latitude=30.6,
        longitude=114.3,
        country="中国",
        match_source="open_meteo",
        resolution="city",
    )

    class Weather:
        @staticmethod
        def search_locations(query: str, *, count: int):
            assert query == "武汉"
            assert count == 10
            return [candidate]

    class Agent:
        weather = Weather()

    result = dispatch_ui_action(
        UIAction(
            operation_id="search-location-keeps-panel-open",
            kind="search_location",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={"query": "武汉"},
        ),
        service=service,
        agent=Agent(),
        mode="real",
        database=database,
        current_field_id=state.id,
        current_state=state,
        session=session,
    )

    view = build_workbench_view(
        service,
        "real",
        state.id,
        state,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session=session,
    )

    assert result.success
    assert view.operation == ""
    assert [location["id"] for location in view.locations] == [candidate.id]


def test_agent_result_status_uses_response_schema_fields(tmp_path) -> None:
    from nongshi.schema_v3 import AgentResponseV3

    service = _service(tmp_path)
    state = _save_field(service, "field-agent-status")
    response = AgentResponseV3(
        run_id="run-agent-status",
        status="success",
        model="deepseek-flash",
        request_count=2,
        request_ids=["request-1", "request-2"],
    )

    view = build_workbench_view(
        service,
        "real",
        state.id,
        state,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={f"agent_result:{state.id}": response},
    )

    assert "2 次模型请求" in view.agent_status
    assert "0 个工具结果" in view.agent_status
    assert "success" in view.agent_status


def test_completed_agent_job_error_is_visible_with_a_next_step(tmp_path) -> None:
    from types import SimpleNamespace

    service = _service(tmp_path)
    state = _save_field(service, "field-agent-error")

    class FinishedJob:
        field_id = state.id
        user_message = "解释今天的安排"

        @staticmethod
        def snapshot():
            return SimpleNamespace(
                done=True,
                response=None,
                error="助手这次没能完成。",
                cancelled=False,
            )

    session = {"active_agent_job": FinishedJob()}
    view = build_workbench_view(
        service,
        "real",
        state.id,
        state,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session=session,
    )

    assert "助手这次没能完成" in view.agent_status
    assert "下方表单" in view.agent_status
    assert session.get(f"agent_job_error:{state.id}") == view.agent_status


def test_agent_model_failure_is_visible_with_its_status(tmp_path) -> None:
    from nongshi.schema_v3 import AgentResponseV3

    service = _service(tmp_path)
    state = _save_field(service, "field-agent-invalid-response")
    response = AgentResponseV3(
        run_id="run-agent-invalid-response",
        status="invalid_response",
        error_message="模型没有返回可用答复。",
        model="deepseek-flash",
        request_count=3,
    )

    view = build_workbench_view(
        service,
        "real",
        state.id,
        state,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={f"agent_result:{state.id}": response},
    )

    assert "模型没有返回可用答复" in view.agent_status
    assert "invalid_response" in view.agent_status
    assert "3 次模型请求" in view.agent_status


def test_html_bridge_saves_profile_then_builds_and_adopts_preview(tmp_path) -> None:
    service = _service(tmp_path)
    session = {}
    database = tmp_path / "workbench.sqlite3"
    profile_action = UIAction(
        operation_id="save-profile-0001",
        kind="save_profile",
        field_id="field-a",
        payload={"name": "北侧地块", "province": "湖北"},
    )

    saved = dispatch_ui_action(
        profile_action,
        service=service,
        agent=None,
        mode="real",
        database=database,
        current_field_id="field-a",
        current_state=None,
        session=session,
    )
    state = service.get_field("field-a")
    preview = dispatch_ui_action(
        UIAction(
            operation_id="preview-plan-0001",
            kind="preview_plan",
            field_id="field-a",
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
        ),
        service=service,
        agent=None,
        mode="real",
        database=database,
        current_field_id="field-a",
        current_state=state,
        session=session,
    )
    actionable = service.latest_actionable_preview("field-a")
    adopted = dispatch_ui_action(
        UIAction(
            operation_id="adopt-plan-0001",
            kind="adopt_plan",
            field_id="field-a",
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            expected_plan_id=None,
            payload={"preview_id": actionable.id},
        ),
        service=service,
        agent=None,
        mode="real",
        database=database,
        current_field_id="field-a",
        current_state=state,
        session=session,
    )

    assert saved.success and preview.outcome == "preview_ready"
    assert adopted.outcome == "adopted"
    assert service.get_current_plan("field-a") is not None
    assert dispatch_ui_action(
        profile_action,
        service=service,
        agent=None,
        mode="real",
        database=database,
        current_field_id="field-a",
        current_state=state,
        session=session,
    ).success
    assert len(service.list_events("field-a")) == 0


def test_html_bridge_rejects_cross_field_mutation(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.save_field_draft(
        __import__("nongshi.schema_v3", fromlist=["FieldDraftV3"]).FieldDraftV3(
            field_id="field-a",
            mode="real",
            profile={"id": "field-a", "name": "北侧地块"},
        ),
        expected_version=None,
        operation_id="seed-field-a-001",
    )

    with pytest.raises(ValueError, match="其他地块"):
        dispatch_ui_action(
            UIAction(
                operation_id="forged-edit-0001",
                kind="save_profile",
                field_id="field-b",
                payload={"name": "不属于当前地块"},
            ),
            service=service,
            agent=None,
            mode="real",
            database=tmp_path / "workbench.sqlite3",
            current_field_id="field-a",
            current_state=first,
            session={},
        )


def test_profile_form_can_set_plan_start_date_without_moving_daily_confirmations(tmp_path) -> None:
    service = _service(tmp_path)
    session = {}
    database = tmp_path / "workbench.sqlite3"
    previous = _save_field(service, "field-start-date")
    previous = service.save_field_draft(
        previous.draft.model_copy(update={"start_date": date(2026, 10, 28)}),
        expected_version=previous.draft_version,
        operation_id="set-initial-start-date-001",
    )
    action = UIAction(
        operation_id="save-profile-start-001",
        kind="save_profile",
        field_id="field-start-date",
        payload={"name": "今日执行地块", "start_date": "2026-10-27"},
    )

    result = dispatch_ui_action(
        action,
        service=service,
        agent=None,
        mode="real",
        database=database,
        current_field_id="field-start-date",
        current_state=previous,
        session=session,
    )

    saved = service.get_field("field-start-date")
    view = build_workbench_view(
        service,
        "real",
        "field-start-date",
        saved,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session=session,
    )
    assert result.success
    assert saved.draft.start_date == date(2026, 10, 27)
    assert view.days[0]["date"] == "2026-10-27"


def test_profile_form_saves_optional_task_durations(tmp_path) -> None:
    service = _service(tmp_path)
    state = _save_field(service, "field-task-durations")

    result = dispatch_ui_action(
        UIAction(
            operation_id="save-task-durations-001",
            kind="save_profile",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "name": "北侧地块",
                "province": "湖北省",
                "province_confirmed": "true",
                "crop": "油菜",
                "crop_confirmed": "true",
                "stage": "苗期",
                "stage_confirmed": "true",
                "inspection_duration_minutes": "35",
                "maintenance_duration_minutes": "50",
            },
        ),
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )

    saved = service.get_field(state.id)
    view = build_workbench_view(
        service,
        "real",
        state.id,
        saved,
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={},
    )
    assert result.success
    assert saved.draft.profile.inspection_duration_minutes == 35
    assert saved.draft.profile.maintenance_duration_minutes == 50
    assert saved.draft.profile.province_confirmed
    assert saved.draft.profile.crop_confirmed
    assert saved.draft.profile.stage_confirmed
    assert view.field["profile"]["inspection_duration_minutes"] == 35
    assert view.field["profile"]["maintenance_duration_minutes"] == 50


@pytest.mark.parametrize(
    ("field_name", "confirmed_name", "message"),
    [
        ("province", "province_confirmed", "确认省份前请先填写省份"),
        ("crop", "crop_confirmed", "确认作物前请先选择作物"),
        ("stage", "stage_confirmed", "确认生育期前请先选择生育期"),
    ],
)
def test_profile_cannot_confirm_an_empty_value(tmp_path, field_name, confirmed_name, message):
    service = _service(tmp_path)
    state = _save_field(service, "field-empty-confirmation")
    result = dispatch_ui_action(
        UIAction(
            operation_id=f"confirm-empty-{field_name}-001",
            kind="save_profile",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "name": "北侧地块",
                confirmed_name: "true",
            },
        ),
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )

    assert not result.success
    assert message in result.message


def test_follow_up_result_passes_explicit_issue_resolution_to_service(tmp_path) -> None:
    state = _save_field(_service(tmp_path), "field-follow-up")
    item = SimpleNamespace(
        task_id="recheck:000001",
        schedule_status="scheduled",
        task_type="follow_up",
        template_snapshot={"related_issue_id": "issue-1"},
    )
    calls = []

    class ServiceProxy:
        @staticmethod
        def get_current_plan(_field_id):
            return SimpleNamespace(items=[item])

        @staticmethod
        def record_task_result(*args, **kwargs):
            calls.append((args, kwargs))

        @staticmethod
        def preview_plan(*args, **kwargs):
            return None

    result = dispatch_ui_action(
        UIAction(
            operation_id="resolve-follow-up-issue-001",
            kind="record_result",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "task_id": item.task_id,
                "result_values": {"复查情况": "normal"},
                "completed": True,
                "resolve_issue_id": "issue-1",
            },
        ),
        service=ServiceProxy(),
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )

    assert result.success
    assert calls[0][1]["resolve_issue_id"] == "issue-1"


def test_html_bridge_creates_a_follow_up_linked_to_an_open_issue(tmp_path) -> None:
    from nongshi.workbench import _today

    state = _save_field(_service(tmp_path), "field-follow-up-create")
    issue = SimpleNamespace(id="issue-1", status="open", title="积水", description="田角仍有积水")
    saved = []

    class ServiceProxy:
        @staticmethod
        def list_field_issues(_field_id):
            return [issue]

        @staticmethod
        def save_task_series(series, **_kwargs):
            saved.append(series)

        @staticmethod
        def preview_plan(*_args, **_kwargs):
            return None

    day = _today() + timedelta(days=1)
    result = dispatch_ui_action(
        UIAction(
            operation_id="create-follow-up-001",
            kind="save_follow_up",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "issue_id": issue.id,
                "anchor_date": day.isoformat(),
                "duration_minutes": "35",
                "purpose": "复查田角积水是否消退",
            },
        ),
        service=ServiceProxy(),
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )

    assert result.success
    assert result.outcome == "preview_ready"
    assert len(saved) == 1
    assert saved[0].template.task_type == "follow_up"
    assert saved[0].template.related_issue_id == issue.id
    assert saved[0].template.trigger == issue.description
    assert saved[0].anchor_date == day
    assert saved[0].duration_minutes == 35


def test_html_bridge_rejects_follow_up_for_closed_issue(tmp_path) -> None:
    from nongshi.workbench import _today

    state = _save_field(_service(tmp_path), "field-follow-up-closed")
    issue = SimpleNamespace(id="issue-1", status="resolved", title="积水", description="")

    class ServiceProxy:
        @staticmethod
        def list_field_issues(_field_id):
            return [issue]

        @staticmethod
        def save_task_series(*_args, **_kwargs):
            pytest.fail("已解决的问题不能创建复查任务")

    result = dispatch_ui_action(
        UIAction(
            operation_id="create-follow-up-closed-001",
            kind="save_follow_up",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "issue_id": issue.id,
                "anchor_date": (_today() + timedelta(days=1)).isoformat(),
                "duration_minutes": "35",
                "purpose": "重新检查",
            },
        ),
        service=ServiceProxy(),
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )

    assert not result.success
    assert "已更新或已解决" in result.message


@pytest.mark.parametrize(
    ("result_values", "completed", "message"),
    [
        ({"复查情况": "normal"}, False, "同时确认复查任务已完成"),
        ({"复查情况": "issue"}, True, "复查仍有异常"),
    ],
)
def test_follow_up_cannot_close_issue_without_completed_normal_recheck(
    tmp_path, result_values, completed, message
) -> None:
    state = _save_field(_service(tmp_path), "field-invalid-follow-up")
    item = SimpleNamespace(
        task_id="recheck:000001",
        schedule_status="scheduled",
        task_type="follow_up",
        template_snapshot={"related_issue_id": "issue-1"},
    )

    class ServiceProxy:
        @staticmethod
        def get_current_plan(_field_id):
            return SimpleNamespace(items=[item])

        @staticmethod
        def record_task_result(*_args, **_kwargs):
            pytest.fail("无效复查不应写入数据库")

    result = dispatch_ui_action(
        UIAction(
            operation_id="invalid-resolve-follow-up-001",
            kind="record_result",
            field_id=state.id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "task_id": item.task_id,
                "result_values": result_values,
                "completed": completed,
                "resolve_issue_id": "issue-1",
            },
        ),
        service=ServiceProxy(),
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id=state.id,
        current_state=state,
        session={},
    )
    assert not result.success
    assert message in result.message


def _save_field(service: WorkbenchServiceV3, field_id: str = "field-a"):
    return service.save_field_draft(
        __import__("nongshi.schema_v3", fromlist=["FieldDraftV3"]).FieldDraftV3(
            field_id=field_id,
            mode="real",
            profile={"id": field_id, "name": "北侧地块"},
        ),
        expected_version=None,
        operation_id=f"seed-{field_id}-001",
    )


def _save_inspection_series(service, field_id: str, series_id: str, task_date: date):
    state = service.get_field(field_id)
    return service.save_task_series(
        TaskSeriesV4(
            id=series_id,
            field_id=field_id,
            template=TaskTemplateV4(
                id=f"template-{series_id}",
                title=f"巡田 {series_id}",
                task_type="inspection",
                category="onsite",
                purpose="按确认内容检查",
                result_fields=["苗情"],
                source_kind="user_defined",
            ),
            recurrence="once",
            anchor_date=task_date,
            duration_minutes=30,
            earliest_date=task_date,
            due_date=task_date,
            conditions_confirmed=True,
        ),
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        expected_task_version=state.task_version,
        operation_id=f"save-{series_id}-001",
    )


def test_html_bridge_updates_future_series_and_preserves_series_identity(tmp_path) -> None:
    service = _service(tmp_path)
    _save_field(service)
    initial = _save_inspection_series(service, "field-a", "series-a", date(2026, 10, 29))
    state = service.get_field("field-a")

    result = dispatch_ui_action(
        UIAction(
            operation_id="update-series-001",
            kind="update_task_series",
            field_id="field-a",
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={"series_id": "series-a", "title": "巡田记录", "purpose": "检查并记录苗情"},
        ),
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id="field-a",
        current_state=state,
        session={},
    )

    updated = service.list_task_series("field-a")[0]
    assert result.success
    assert updated.id == "series-a"
    assert updated.template.title == "巡田记录"
    assert updated.template.version == 2
    assert initial.created_instance_ids


def test_html_bridge_saves_execution_group_and_preview_from_selected_members(tmp_path) -> None:
    service = _service(tmp_path)
    _save_field(service)
    task_date = date(2026, 10, 29)
    first = _save_inspection_series(service, "field-a", "series-a", task_date)
    state = service.get_field("field-a")
    second = _save_inspection_series(service, "field-a", "series-b", task_date)
    state = service.get_field("field-a")
    member_ids = [first.created_instance_ids[0], second.created_instance_ids[0]]

    result = dispatch_ui_action(
        UIAction(
            operation_id="save-group-001",
            kind="save_execution_group",
            field_id="field-a",
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            payload={
                "member_task_ids": member_ids,
                "execution_date": task_date.isoformat(),
                "duration_minutes": 45,
            },
        ),
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id="field-a",
        current_state=state,
        session={},
    )

    group = service.store.list_task_execution_groups("field-a")[0]
    assert result.success
    assert group.member_task_ids == member_ids
    assert group.duration_minutes == 45
    assert group.status == "confirmed"
    view = build_workbench_view(
        service,
        "real",
        "field-a",
        service.get_field("field-a"),
        today=date(2026, 10, 27),
        now=datetime(2026, 10, 27, 8, tzinfo=UTC),
        session={},
    )
    assert {row["id"] for row in view.task_series} == {"series-a", "series-b"}
    assert view.execution_groups[0]["member_task_ids"] == member_ids
    assert {row["id"] for row in view.group_candidates} == set(member_ids)
    projected_members = {row["id"]: row for row in view.items}
    assert all(
        projected_members[task_id]["execution_group_id"] == group.id for task_id in member_ids
    )
    assert all(projected_members[task_id]["group_duration_minutes"] == 45 for task_id in member_ids)


def test_preview_labels_new_task_with_its_title_before_adoption(tmp_path) -> None:
    service = _service(tmp_path)
    state = _save_field(service, "field-preview-title")
    task_date = date.today() + timedelta(days=1)
    series = TaskSeriesV4(
        id="series-preview-title",
        field_id=state.id,
        template=TaskTemplateV4(
            id="template-preview-title",
            title="巡田核对田角积水",
            task_type="inspection",
            category="onsite",
            purpose="记录田角积水是否消退",
            result_fields=["积水情况"],
            source_kind="user_defined",
        ),
        recurrence="once",
        anchor_date=task_date,
        duration_minutes=35,
        earliest_date=task_date,
        due_date=task_date,
        conditions_confirmed=True,
    )
    service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        expected_task_version=state.task_version,
        operation_id="save-preview-title-series",
    )
    service.preview_plan(state.id, operation_id="preview-title-001")

    view = build_workbench_view(
        service,
        "real",
        state.id,
        service.get_field(state.id),
        today=date.today(),
        now=datetime.combine(date.today(), datetime.min.time(), tzinfo=UTC),
        session={},
    )

    assert view.preview is not None
    assert any(change["title"] == "巡田核对田角积水" for change in view.preview["changes"])


def test_html_bridge_cancels_one_task_instance_without_changing_its_sibling(tmp_path) -> None:
    service = _service(tmp_path)
    _save_field(service)
    state = service.get_field("field-a")
    series = TaskSeriesV4(
        id="series-dates",
        field_id="field-a",
        template=TaskTemplateV4(
            id="template-series-dates",
            title="整理农资记录",
            task_type="custom",
            category="offsite",
            purpose="逐次整理采购单据",
            result_fields=["完成情况"],
        ),
        recurrence="dates",
        anchor_date=date(2026, 10, 29),
        end_date=date(2026, 10, 30),
        selected_dates=[date(2026, 10, 29), date(2026, 10, 30)],
        duration_minutes=30,
        earliest_date=date(2026, 10, 29),
        due_date=date(2026, 10, 30),
    )
    created = service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        expected_task_version=state.task_version,
        operation_id="save-dated-series-001",
    )
    state = service.get_field("field-a")
    preview = service.preview_plan("field-a", operation_id="preview-dated-series-001")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=None,
        operation_id="adopt-dated-series-001",
    )
    state = service.get_field("field-a")

    result = dispatch_ui_action(
        UIAction(
            operation_id="cancel-first-instance-001",
            kind="cancel_task",
            field_id="field-a",
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            expected_plan_id=service.get_current_plan("field-a").id,
            payload={"task_id": created.created_instance_ids[0], "reason": "本次无法完成"},
        ),
        service=service,
        agent=None,
        mode="real",
        database=tmp_path / "workbench.sqlite3",
        current_field_id="field-a",
        current_state=state,
        session={},
    )

    events = service.list_events("field-a")
    cancellations = [
        event for event in events if event.feedback and event.feedback.kind == "cancelled"
    ]
    assert result.success
    assert [event.feedback.task_id for event in cancellations] == [created.created_instance_ids[0]]
    assert created.created_instance_ids[1] in {
        instance.id for instance in service.store.list_task_instances("field-a")
    }
