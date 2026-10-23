"""Public synthetic agent trials must use actual returned artifacts, never oracle rescue."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_d02_runner_scripted import _case

from nongshi.d02_runner import _ScriptSession
from nongshi.d02_sealed import _common_result
from nongshi.schema_v2 import FeedbackV2
from nongshi.schema_v3 import AgentProposalV3, AgentResponseV3, AgentRunV3, FactEventInputV3


def _session(tmp_path: Path) -> _ScriptSession:
    case = _case()
    session = _ScriptSession(case, database=tmp_path / "trial.sqlite3", enable_model=True)
    session._op_save_draft(case["interaction_script"][0])
    return session


def _response(**kwargs) -> AgentResponseV3:
    return AgentResponseV3(
        run_id="public-run", status="success", model="test", request_count=1, **kwargs
    )


def _persist_fake_proposal(session: _ScriptSession, proposal: AgentProposalV3) -> None:
    session.store.save_agent_run(
        AgentRunV3(
            id="public-run",
            field_id=session.field_id,
            model="test",
            started_at=datetime.fromisoformat("2026-10-20T18:12:00+08:00"),
            elapsed_ms=1,
            request_count=1,
            tool_count=1,
            status="success",
        ),
        [proposal],
    )


def test_actual_agent_preview_is_bound_and_adopted(tmp_path, monkeypatch):
    session = _session(tmp_path)
    preview = session.service.preview_plan(session.field_id)
    monkeypatch.setattr(
        "nongshi.agent.AgentServiceV3.run", lambda *a, **kw: _response(previews=[preview])
    )
    step = {"step": 2, "operation": "chat", "user_message": "请预览", "payload": {}}
    status, result, _ = session._op_chat(step)
    assert status == "ok"
    assert session.bindings["initial_preview"] == preview.id
    assert result["preview_id"] == preview.id
    assert result["items"] == _common_result({"result": session.summary()})["tasks"]
    assert session._op_adopt({"step": 3, "payload": {}})[0] == "ok"
    session._agent.close()


def test_model_without_artifact_cannot_be_rescued_by_adoption(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr("nongshi.agent.AgentServiceV3.run", lambda *a, **kw: _response(text="好了"))
    session._op_chat({"step": 2, "user_message": "请预览"})
    assert session._op_adopt({"step": 3, "payload": {}})[0] == "failed"
    assert session.summary()["current_plan_items"] == []
    session._agent.close()


def _event(session, value=True):
    return FactEventInputV3(
        feedback=FeedbackV2(
            kind="observation",
            plot_id=session.field_id,
            field="standing_water",
            value=value,
            at=datetime.fromisoformat("2026-10-20T18:12:00+08:00"),
        ),
        source="user",
    )


@pytest.mark.parametrize("proposed", [None, False])
def test_confirmation_never_inserts_missing_or_wrong_model_event(tmp_path, monkeypatch, proposed):
    session = _session(tmp_path)
    proposals = (
        []
        if proposed is None
        else [
            AgentProposalV3(
                kind="facts",
                field_id=session.field_id,
                expected_fact_version=0,
                events=[_event(session, proposed)],
                summary="观察",
            )
        ]
    )
    monkeypatch.setattr(
        "nongshi.agent.AgentServiceV3.run", lambda *a, **kw: _response(proposals=proposals)
    )
    session._op_chat({"step": 2, "user_message": "看见积水"})
    confirmation = {
        "step": 3,
        "operation": "confirm_feedback",
        "payload": {
            "proposal_step": 2,
            "events": [_event(session).model_dump(mode="json")],
        },
    }
    assert session.execute_step(confirmation)[0] == "rejected"
    assert session.service.list_events(session.field_id) == []
    session._agent.close()


def test_matching_confirmation_saves_model_event_and_replay_is_idempotent(tmp_path, monkeypatch):
    session = _session(tmp_path)
    actual = _event(session)
    proposal = AgentProposalV3(
        kind="facts",
        field_id=session.field_id,
        expected_fact_version=0,
        events=[actual],
        summary="观察",
    )
    monkeypatch.setattr(
        "nongshi.agent.AgentServiceV3.run", lambda *a, **kw: _response(proposals=[proposal])
    )
    session._op_chat({"step": 2, "user_message": "看见积水"})
    _persist_fake_proposal(session, proposal)
    oracle = deepcopy(actual.model_dump(mode="json"))
    oracle["event_id"] = "different-script-id"
    confirmation = {
        "step": 3,
        "operation": "confirm_feedback",
        "payload": {
            "proposal_step": 2,
            "events": [oracle],
        },
    }
    first_status, first_result, _ = session.execute_step(confirmation)
    second_status, second_result, _ = session.execute_step(confirmation)
    assert first_status == second_status == "ok"
    assert first_result["replayed"] is False
    assert second_result["replayed"] is True
    if first_result["preview_id"]:
        assert session.bindings["preview_3"] == first_result["preview_id"]
    saved = session.service.list_events(session.field_id)
    assert len(saved) == 1 and saved[0].event_id == actual.event_id
    session._agent.close()


def test_confirmation_accepts_consistent_redundant_completion_day(tmp_path, monkeypatch):
    session = _session(tmp_path)
    preview = session.service.preview_plan(session.field_id)
    session.service.adopt_plan(
        preview.id,
        expected_fact_version=preview.expected_fact_version,
        expected_plan_id=preview.expected_plan_id,
        operation_id="adopt-before-completion",
    )
    task = next(
        item for item in session.service.get_current_plan(session.field_id).items if item.start_at
    )
    actual = FactEventInputV3(
        feedback=FeedbackV2(
            kind="completed",
            task_id=task.task_id,
            plot_id=session.field_id,
            at=task.end_at,
        )
    )
    proposal = AgentProposalV3(
        kind="facts",
        field_id=session.field_id,
        expected_fact_version=0,
        events=[actual],
        summary="已完成",
    )
    monkeypatch.setattr(
        "nongshi.agent.AgentServiceV3.run", lambda *a, **kw: _response(proposals=[proposal])
    )
    session._op_chat({"step": 2, "user_message": "已完成"})
    _persist_fake_proposal(session, proposal)
    expected = actual.model_dump(mode="json")
    expected["feedback"]["day"] = task.end_at.date().isoformat()
    expected["feedback"]["at"] = task.end_at.astimezone().isoformat()
    assert (
        session._op_confirm_feedback(
            {"step": 3, "payload": {"proposal_step": 2, "events": [expected]}}
        )[0]
        == "ok"
    )
    assert len(session.service.list_events(session.field_id)) == 1
    session._agent.close()


def test_confirmation_rejects_conflicting_completion_day(tmp_path, monkeypatch):
    session = _session(tmp_path)
    preview = session.service.preview_plan(session.field_id)
    session.service.adopt_plan(
        preview.id,
        expected_fact_version=preview.expected_fact_version,
        expected_plan_id=preview.expected_plan_id,
        operation_id="adopt-for-conflict",
    )
    task = next(
        item for item in session.service.get_current_plan(session.field_id).items if item.start_at
    )
    actual = FactEventInputV3(
        feedback=FeedbackV2(
            kind="completed",
            task_id=task.task_id,
            plot_id=session.field_id,
            at=task.end_at,
        )
    )
    session._chat_proposals[2] = [
        AgentProposalV3(
            kind="facts",
            field_id=session.field_id,
            expected_fact_version=0,
            events=[actual],
            summary="完成",
        )
    ]
    expected = actual.model_dump(mode="json")
    expected["feedback"]["day"] = (task.end_at.date() + timedelta(days=1)).isoformat()
    assert (
        session._op_confirm_feedback(
            {"step": 3, "payload": {"proposal_step": 2, "events": [expected]}}
        )[0]
        == "rejected"
    )
    assert session.service.list_events(session.field_id) == []


def test_adopt_explicit_preview_binding_uses_new_preview(tmp_path, monkeypatch):
    session = _session(tmp_path)
    first = session.service.preview_plan(session.field_id)
    session.bindings["initial_preview"] = first.id
    assert session._op_adopt({"step": 2, "payload": {}})[0] == "ok"
    second = session.service.preview_plan(session.field_id)
    session.bindings["preview_new"] = second.id
    status, output, _ = session._op_adopt(
        {"step": 3, "payload": {"preview_binding": "preview_new"}}
    )
    assert status == "ok"
    assert output["adopted_version_id"] == session.service.get_current_plan(session.field_id).id
    assert session.store.load_preview(second.id).status == "adopted"
    assert (
        session._op_adopt({"step": 4, "payload": {"preview_binding": "does-not-exist"}})[0]
        == "failed"
    )
    assert (
        session._op_adopt(
            {
                "step": 5,
                "payload": {"preview_binding": "preview_new", "plan_binding": "initial_preview"},
            }
        )[0]
        == "failed"
    )


def test_confirmed_script_payload_is_hidden_from_model_until_confirmation():
    from nongshi.d02_model_groups import _case_input

    case = _case()
    case["interaction_script"] = [
        {"step": 1, "operation": "save_draft", "at": case["received_at"], "payload": {}},
        {
            "step": 2,
            "operation": "chat",
            "at": case["received_at"],
            "user_message": "看见积水",
            "payload": {"evaluation_binding": "oracle-secret"},
        },
        {
            "step": 3,
            "operation": "confirm_feedback",
            "at": case["received_at"],
            "payload": {
                "events": [
                    {"feedback": {"kind": "observation", "field": "standing_water", "value": True}}
                ]
            },
        },
    ]
    visible = _case_input(case, through_step=2)
    assert "oracle-secret" not in str(visible)
    assert visible["draft"]["profile"].get("standing_water") is not True
    confirmed = _case_input(case, through_step=3)
    assert confirmed["draft"]["profile"]["standing_water"] is True


def test_preview_cancellation_returns_adopted_result(tmp_path, monkeypatch):
    session = _session(tmp_path)
    first = session.service.preview_plan(session.field_id)
    changed = session.service.preview_plan(session.field_id)
    responses = iter([_response(previews=[first]), _response(previews=[changed])])
    monkeypatch.setattr("nongshi.agent.AgentServiceV3.run", lambda *a, **kw: next(responses))
    session._op_chat({"step": 2, "user_message": "预览"})
    session._op_adopt({"step": 3, "payload": {}})
    session._op_chat({"step": 4, "user_message": "再预览"})
    session._op_cancel_preview({"step": 5, "payload": {"preview_binding": "preview_4"}})
    assert session.summary()["displayed_result"]["source"] == "adopted_after_cancel"
    session._agent.close()


def test_formal_runner_keeps_earlier_model_responses_and_all_tool_metrics(monkeypatch):
    from nongshi.config import Settings
    from nongshi.d02_sealed import _model_turns

    captured = []

    def fake(case, group, **kwargs):
        captured.append(kwargs.get("previous_turns"))
        return {
            "status": "executed",
            "result": {"tasks": []},
            "model_calls": 1,
            "elapsed_ms": 1,
            "token_usage": {"prompt": 2, "completion": 3},
            "tool_records": [{"name": "search_rules", "status": "success"}],
            "first_result": '{"tasks":[]}',
        }

    monkeypatch.setattr("nongshi.d02_sealed.run_model_group", fake)
    case = _case()
    case["interaction_script"] = [
        {"step": 2, "operation": "chat"},
        {"step": 4, "operation": "chat"},
    ]
    out = _model_turns(case, "B", Settings())
    assert captured[0] == [] and len(captured[1]) == 1
    assert len(out["tool_records"]) == 2


def test_earlier_failed_model_turn_remains_failed_after_later_success(monkeypatch):
    from nongshi.config import Settings
    from nongshi.d02_sealed import _model_turns

    attempts = iter(
        [
            {
                "status": "failed",
                "scorable": False,
                "result": None,
                "model_calls": 1,
                "elapsed_ms": 1,
                "token_usage": {"prompt": None, "completion": None},
                "tool_records": [],
                "errors": ["invalid JSON"],
            },
            {
                "status": "executed",
                "scorable": True,
                "result": {"tasks": []},
                "model_calls": 1,
                "elapsed_ms": 2,
                "token_usage": {"prompt": 2, "completion": 3},
                "tool_records": [],
                "errors": [],
            },
        ]
    )
    monkeypatch.setattr("nongshi.d02_sealed.run_model_group", lambda *a, **kw: next(attempts))
    case = _case()
    case["interaction_script"] = [
        {"step": 2, "operation": "chat"},
        {"step": 4, "operation": "chat"},
    ]
    result = _model_turns(case, "A", Settings())
    assert result["status"] == "failed" and result["scorable"] is False
    assert len(result["steps"]) == 2 and result["model_calls"] == 2


def test_ordinary_adapter_request_is_not_mislabeled_as_new_maintenance_demand():
    from nongshi.d02_runner import validate_case

    case = _case()
    case["interaction_script"].append(
        {
            "step": 99,
            "at": case["received_at"],
            "operation": "request",
            "payload": {"adapter_action": "retry_weather", "attempt": 2},
        }
    )
    assert not any(
        warning["code"] == "request_missing_confirmation"
        for warning in validate_case(case)["warnings"]
    )


def test_explicit_two_attempt_failure_records_both_without_model_call(monkeypatch):
    from nongshi.config import Settings
    from nongshi.d02_sealed import _model_turns

    monkeypatch.setattr(
        "nongshi.d02_sealed.run_model_group",
        lambda *a, **kw: pytest.fail("injected failure must not call live model"),
    )
    case = _case()
    case["interaction_script"] = [
        {"step": 2, "operation": "chat"},
        {"step": 3, "operation": "chat"},
    ]
    case["adapter_failure_fixture"] = {
        "operation": "chat",
        "attempts": [
            {"inject_at_step": 2, "status": "no_output"},
            {"inject_at_step": 3, "status": "timeout"},
        ],
    }
    result = _model_turns(case, "A", Settings())
    assert result["status"] == "failed"
    assert result["model_calls"] == 0
    assert [step["output"]["failure_status"] for step in result["steps"]] == [
        "no_output",
        "timeout",
    ]


def test_saved_user_description_is_available_to_first_agent_turn(tmp_path, monkeypatch):
    case = _case()
    case["interaction_script"][0]["user_message"] = "地块叫清河镇，省县还未确认"
    session = _ScriptSession(case, database=tmp_path / "context.sqlite3", enable_model=True)
    session.execute_step(case["interaction_script"][0])
    seen = []

    def fake(*_args, **kwargs):
        seen.extend(kwargs["conversation"])
        return _response(text="请确认地点")

    monkeypatch.setattr("nongshi.agent.AgentServiceV3.run", fake)
    session._op_chat({"step": 2, "operation": "chat", "user_message": "请查地点"})
    assert any("清河镇" in item["content"] for item in seen)
    session._agent.close()
