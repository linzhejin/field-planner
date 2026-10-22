"""The same source has the same meaning in planning and retrieval."""

from __future__ import annotations

from test_planning_v2 import request_v2

from nongshi.planning_v2 import build_plan_v2
from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import RuleEvidenceV2
from nongshi.schema_v3 import RuleSearchQueryV3


def test_evidence_roles_and_source_context_agree_across_tools() -> None:
    planned = build_plan_v2(request_v2(maintenance_purpose="clear_existing_ditch"))
    inspection = next(item for item in planned.items if item.task_type == "inspection")
    by_source = {(e.rule_id, e.source_id): e for e in inspection.evidence}
    assert by_source[("C11", "Z15")].usage_role == "limited_support"
    assert by_source[("C11", "Z09")].usage_role == "limited_support"
    assert by_source[("C19", "Z09")].usage_role == "historical_reference"
    assert "苗期" in by_source[("C11", "Z15")].source_context_summary
    assert "冬前" in by_source[("C11", "Z09")].source_context_summary
    assert "专家" in by_source[("C31", "Z16")].source_context_summary

    result = RuleRetrieverV3().search(
        RuleSearchQueryV3(
            province="湖北",
            province_confirmed=True,
            crop="油菜",
            crop_confirmed=True,
            stage="苗期",
            stage_confirmed=True,
            field_type="lowland_rice_stubble",
            task_type="inspection",
        )
    )
    searched = {(e.rule_id, e.source_id): e for hit in result.hits for e in hit.evidence}
    for key, item in by_source.items():
        assert searched[key].usage_role == item.usage_role
        assert searched[key].source_context_summary == item.source_context_summary
        assert searched[key].status_label == item.status_label


def test_deferred_card_cannot_be_promoted_by_field_review_and_old_rows_still_read() -> None:
    request = request_v2(maintenance_purpose="clear_existing_ditch")
    request.rule_review_status = {"C19": "adopted"}
    plan = build_plan_v2(request)
    inspection = next(item for item in plan.items if item.task_type == "inspection")
    c19 = next(item for item in inspection.evidence if item.rule_id == "C19")
    assert c19.usage_role == "historical_reference"

    old_row = load_rule_catalog().evidence_for(["C19"])[0]
    old_row["status_label"] = "暂缓自动适用，仅作历史参考"
    assert RuleEvidenceV2.model_validate(old_row).usage_role == "historical_reference"


def test_maintenance_needs_confirmed_clearance_purpose() -> None:
    without_purpose = build_plan_v2(request_v2(maintenance_purpose=None))
    maintenance = next(item for item in without_purpose.items if item.task_type == "maintenance")
    assert maintenance.schedule_status == "pending"
    assert "maintenance_purpose" in maintenance.missing_fields

    with_purpose = build_plan_v2(request_v2(maintenance_purpose="clear_existing_ditch"))
    maintenance = next(item for item in with_purpose.items if item.task_type == "maintenance")
    assert maintenance.schedule_status == "scheduled"
