from datetime import datetime

import pytest

from nongshi.feedback_time import audit_completed_time_visibility, ground_completed_time


def dt(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "text,proposed,accepted",
    [
        ("今天09:40完成巡查", "2026-10-23T09:40:00+08:00", True),
        ("2026年10月23日上午9点40分完成巡查", "2026-10-23T09:40:00+08:00", True),
        ("巡查于2026-10-23T01:40:00+00:00完成", "2026-10-23T09:40:00+08:00", True),
        ("刚刚完成巡查", "2026-10-23T10:00:00+08:00", True),
        ("今天已完成巡查", "2026-10-23T10:00:00+08:00", False),
        ("上午完成巡查", "2026-10-23T10:00:00+08:00", False),
        ("巡查已完成，资源14:30可用", "2026-10-23T14:30:00+08:00", False),
        ("今天09:40完成巡查", "2026-10-23T09:41:00+08:00", False),
        ("昨天09:40完成巡查", "2026-10-23T09:40:00+08:00", False),
        ("今天11:00完成巡查", "2026-10-23T11:00:00+08:00", False),
    ],
)
def test_grounding(text, proposed, accepted):
    result = ground_completed_time(dt(proposed), dt("2026-10-23T10:00:00+08:00"), text)
    assert result.accepted is accepted
    assert (result.actual_at == dt(proposed)) if accepted else result.actual_at is None
    assert result.reason


def case(text):
    return {
        "case_id": "PUBLIC-TIME",
        "received_at": "2026-10-23T10:00:00+08:00",
        "interaction_script": [
            {"step": 1, "operation": "chat", "user_message": text},
            {
                "step": 2,
                "operation": "confirm_feedback",
                "at": "2026-10-23T10:02:00+08:00",
                "payload": {
                    "proposal_step": 1,
                    "events": [
                        {
                            "source": "user",
                            "feedback": {
                                "kind": "completed",
                                "task_id": "inspection",
                                "at": "2026-10-23T09:40:00+08:00",
                            },
                        }
                    ],
                },
            },
        ],
    }


def test_visibility_audit_uses_user_time_not_hidden_answer_or_save_time():
    assert audit_completed_time_visibility(case("今天09:40完成巡查")) == []
    assert audit_completed_time_visibility(case("今天已完成巡查"))
    assert audit_completed_time_visibility(case("巡查已完成，资源09:40可用"))


def test_missing_completion_does_not_turn_resource_into_completion():
    result = ground_completed_time(
        dt("2026-10-23T09:40:00+08:00"), dt("2026-10-23T10:00:00+08:00"), "资源09:40可用"
    )
    assert not result.accepted
    assert result.reason == "missing_actual_completion_time"


def test_later_chat_cannot_supply_hidden_time():
    value = case("今天已完成巡查")
    value["interaction_script"].append(
        {"step": 3, "operation": "chat", "user_message": "今天09:40完成巡查"}
    )
    assert audit_completed_time_visibility(value)


def test_seal_version_marks_completion_visibility_contract():
    from nongshi.d02_sealed import SEAL_VERSION

    assert SEAL_VERSION == "internal-auto-v7"


def test_formal_seal_rejects_completion_time_only_in_confirmation(tmp_path, monkeypatch):
    from nongshi.d02_sealed import FILES, seal

    cases = [case("今天已完成巡查") for _ in range(36)]
    for index, value in enumerate(cases):
        value["case_id"] = f"PUBLIC-{index}"
    loaded = {
        "main_cases": cases[:30],
        "boundary_cases": cases[30:],
        "main_constraints": [{"case_id": row["case_id"]} for row in cases[:30]],
        "boundary_constraints": [{"case_id": row["case_id"]} for row in cases[30:]],
    }
    monkeypatch.setattr("nongshi.d02_sealed._git", lambda *args: "")
    monkeypatch.setattr("nongshi.d02_sealed._inside_git_checkout", lambda path: False)
    monkeypatch.setattr("nongshi.d02_sealed._validate_pair", lambda *args: None)
    monkeypatch.setattr("nongshi.d02_sealed._validate_formal_constraints", lambda *args: None)
    monkeypatch.setattr("nongshi.d02_sealed._read_jsonl", lambda path: loaded[path.name])
    with pytest.raises(ValueError, match="missing visible actual completion time"):
        seal(
            {key: tmp_path / key for key in FILES},
            tmp_path / "seal",
            known_exposure=["public simulated fixture"],
        )


@pytest.mark.parametrize("text", ["09:40开始10:00完成巡查", "09:40未结束巡查", "打算09:40完成巡查"])
def test_ambiguous_or_nonactual_completion_is_not_grounded(text):
    result = ground_completed_time(
        dt("2026-10-23T09:40:00+08:00"), dt("2026-10-23T12:00:00+08:00"), text
    )
    assert not result.accepted


@pytest.mark.parametrize("text", ["刚才完成巡查", "上午9点半完成巡查", "上午9点一刻完成巡查"])
def test_unsupported_or_imprecise_phrasing_never_truncates_to_exact_time(text):
    proposed = "2026-10-23T10:00:00+08:00" if "刚才" in text else "2026-10-23T09:00:00+08:00"
    assert not ground_completed_time(dt(proposed), dt("2026-10-23T10:00:00+08:00"), text).accepted


@pytest.mark.parametrize(
    "text",
    [
        "还没在09:40完成巡查",
        "09:40完成巡查了吗",
        "09:40完成巡查？",
        "不是09:40完成巡查",
        "能在09:40完成巡查吗",
    ],
)
def test_nonassertions_cannot_ground_completion(text):
    assert not ground_completed_time(
        dt("2026-10-23T09:40:00+08:00"), dt("2026-10-23T10:00:00+08:00"), text
    ).accepted


@pytest.mark.parametrize("text", ["09:40:30完成巡查", "109:40完成巡查", "09:400完成巡查"])
def test_exact_clock_cannot_be_truncated(text):
    assert not ground_completed_time(
        dt("2026-10-23T09:40:00+08:00"), dt("2026-10-23T10:00:00+08:00"), text
    ).accepted


def test_audit_does_not_borrow_unrelated_earlier_task_clock():
    value = case("巡查今天已完成")
    value["interaction_script"].insert(
        0, {"step": 0, "operation": "chat", "user_message": "09:40完成浇水"}
    )
    assert audit_completed_time_visibility(value)


def test_audit_rejects_unidentified_task_but_accepts_exact_task_id():
    value = case("09:40完成巡查")
    value["interaction_script"][1]["payload"]["events"][0]["feedback"]["task_id"] = "opaque-task"
    assert audit_completed_time_visibility(value)
    value["interaction_script"][0]["user_message"] = "09:40完成任务 opaque-task"
    assert audit_completed_time_visibility(value) == []


def test_audit_rejects_other_task_in_same_message_and_allows_same_task_history():
    value = case("09:40完成浇水，巡查今天已完成")
    assert audit_completed_time_visibility(value)
    value["interaction_script"].insert(
        0, {"step": 0, "operation": "chat", "user_message": "09:40完成巡查"}
    )
    assert audit_completed_time_visibility(value) == []


@pytest.mark.parametrize(
    "text", ["可能09:40完成巡查", "不确定09:40完成巡查", "如果09:40完成巡查", "是否09:40完成巡查"]
)
def test_uncertain_completion_is_not_actual(text):
    assert not ground_completed_time(
        dt("2026-10-23T09:40:00+08:00"), dt("2026-10-23T10:00:00+08:00"), text
    ).accepted


def test_audit_uses_declared_task_kind_with_unambiguous_visible_clause():
    value = case("09:40完成巡田")
    value["stage_execution_policy"] = {"task_ids": {"inspection": "sim-field-I"}}
    value["interaction_script"][1]["payload"]["events"][0]["feedback"]["task_id"] = "sim-field-I"
    assert audit_completed_time_visibility(value) == []
    value["interaction_script"][0]["user_message"] = "09:40完成清沟"
    assert audit_completed_time_visibility(value)
    value["interaction_script"][0]["user_message"] = "09:40完成巡田和清沟"
    assert audit_completed_time_visibility(value)


@pytest.mark.parametrize(
    "text",
    [
        "9点40分30秒完成巡查",
        "大概09:40完成巡查",
        "09:40完成巡查了吧",
        "大约09:40完成巡查",
        "估计09:40完成巡查",
    ],
)
def test_approximate_or_chinese_seconds_do_not_ground_exact_completion(text):
    assert not ground_completed_time(
        dt("2026-10-23T09:40:00+08:00"), dt("2026-10-23T10:00:00+08:00"), text
    ).accepted


def test_audit_maps_existing_ditch_cleanup_phrase_to_maintenance():
    value = case("既有沟渠清理的实际完成时间是2026-10-23T09:40:00+08:00")
    value["stage_execution_policy"] = {"task_ids": {"maintenance": "sim-field-M"}}
    value["interaction_script"][1]["payload"]["events"][0]["feedback"]["task_id"] = "sim-field-M"
    assert audit_completed_time_visibility(value) == []
