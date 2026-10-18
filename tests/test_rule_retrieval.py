"""Rule retrieval reports source evidence and applicability gaps separately."""

from __future__ import annotations

from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v3 import RuleSearchQueryV3


def query(**overrides: object) -> RuleSearchQueryV3:
    values: dict[str, object] = {
        "province": "湖北",
        "province_confirmed": True,
        "crop": "油菜",
        "crop_confirmed": True,
        "stage": "苗期",
        "stage_confirmed": True,
        "field_type": "lowland_rice_stubble",
        "task_type": "inspection",
    }
    values.update(overrides)
    return RuleSearchQueryV3(**values)


def test_matching_rule_hits_retain_original_quotes_pages_hash_and_review_label() -> None:
    result = RuleRetrieverV3().search(query())

    assert result.status == "matched"
    assert {hit.rule_id for hit in result.hits} == {"C11", "C31", "C19", "C20"}
    assert result.profile_label == "开发验证，适用性待复核"
    assert all(hit.runtime_enabled is False for hit in result.hits)
    assert all(hit.evidence and hit.evidence[0].pdf_pages for hit in result.hits)
    assert all(hit.evidence[0].source_sha256 for hit in result.hits)
    assert load_rule_catalog().cards["C11"].runtime_enabled is False


def test_missing_crop_or_stage_returns_pending_instead_of_applying_by_guess() -> None:
    result = RuleRetrieverV3().search(query(crop=None, crop_confirmed=False, stage=None))

    assert result.status == "pending"
    assert all(hit.applicability == "pending" for hit in result.hits)
    assert "作物" in result.reason and "生育期" in result.reason


def test_out_of_scope_location_and_field_type_are_explained() -> None:
    result = RuleRetrieverV3().search(query(province="湖南"))
    assert result.status == "out_of_scope"
    assert all(hit.applicability == "out_of_scope" for hit in result.hits)

    wrong_type = RuleRetrieverV3().search(query(field_type="dry_hilly"))
    by_id = {hit.rule_id: hit for hit in wrong_type.hits}
    assert by_id["C19"].applicability == "out_of_scope"
    assert by_id["C20"].applicability == "matched"


def test_unknown_maintenance_problem_keeps_maintenance_rule_pending() -> None:
    result = RuleRetrieverV3().search(
        query(
            task_type="maintenance",
            problem="ditch",
            problem_confirmed=None,
            maintenance_requested=None,
        )
    )
    hit = next(hit for hit in result.hits if hit.rule_id == "C23")

    assert result.status == "pending"
    assert hit.applicability == "pending"
    assert "不能推定" in hit.reason


def test_unrepresented_problem_returns_no_basis_instead_of_keyword_match() -> None:
    result = RuleRetrieverV3().search(query(problem="weed_control"))

    assert result.status == "no_basis"
    assert not result.hits
    assert "没有直接依据" in result.reason
