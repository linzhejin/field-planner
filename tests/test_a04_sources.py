"""A04 补充来源登记与检索展示：追加标识，不改写原卡、原来源索引或启用状态。"""

from __future__ import annotations

import json
from pathlib import Path

from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v3 import RuleSearchQueryV3

A03_JSON = (
    Path(__file__).resolve().parents[1]
    / "任务落实_20261019"
    / "A03_规则采用记录"
    / "A03_七卡采用建议_20261019.json"
)
A04_JSON = (
    Path(__file__).resolve().parents[1]
    / "任务落实_20261019"
    / "A04_缺口答复"
    / "A04_缺口答复表_20261019.json"
)


def test_supplementary_sources_are_registered_with_prefix_and_recorded_fingerprints() -> None:
    catalog = load_rule_catalog()

    assert set(catalog.supplementary_sources) == {
        "A04-S01",
        "A04-S02",
        "A04-S03",
        "A04-S04",
        "A04-S05",
        "A04-S06",
        "A04-S07",
    }
    assert all(source_id.startswith("A04-") for source_id in catalog.supplementary_sources)

    recorded = {
        row["id"]: row for row in json.loads(A04_JSON.read_text(encoding="utf-8"))["sources"]
    }
    for source_id, source in catalog.supplementary_sources.items():
        original = recorded[source_id.removeprefix("A04-")]
        assert source.title == original["title"]
        assert source.url == original["url"]
        assert source.sha256 == original["sha256"].lower()
        assert source.access_date == original["access_date"]


def test_supplementary_registration_does_not_touch_original_source_index_or_card_state() -> None:
    catalog = load_rule_catalog()

    assert all(source_id.startswith("Z") for source_id in catalog.sources)
    assert not (set(catalog.sources) & set(catalog.supplementary_sources))
    assert all(card.runtime_enabled is False for card in catalog.cards.values())
    assert all(
        evidence["source_id"].startswith("Z")
        for evidence in catalog.evidence_for(["C18", "C19", "C20", "C23", "C24", "C31"])
    )


def test_retrieval_hits_carry_supplementary_sources_without_changing_applicability() -> None:
    query = RuleSearchQueryV3(
        province="湖北",
        province_confirmed=True,
        crop="油菜",
        crop_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type="lowland_rice_stubble",
        task_type="all",
    )
    by_id = {hit.rule_id: hit for hit in RuleRetrieverV3().search(query).hits}

    c19_ids = [row.source_id for row in by_id["C19"].supplementary_sources]
    assert c19_ids and all(source_id.startswith("A04-") for source_id in c19_ids)
    assert by_id["C19"].applicability == "reference"
    assert all(
        row.status_label == "A04 补充来源；不改变原卡适用性与启用状态"
        for row in by_id["C19"].supplementary_sources
    )
    assert by_id["C23"].supplementary_sources
    assert by_id["C11"].supplementary_sources == []


def test_deferred_and_limited_adopt_ids_come_from_a03_registry() -> None:
    catalog = load_rule_catalog()
    recorded = {
        row["id"]: row["proposed_decision"]
        for row in json.loads(A03_JSON.read_text(encoding="utf-8"))["records"]
    }

    assert catalog.deferred_rule_ids == {
        rule_id for rule_id, decision in recorded.items() if decision == "暂缓"
    }
    assert catalog.deferred_rule_ids == {"C18", "C19", "C20", "C24"}
    assert catalog.limited_adopt_rule_ids == {"C11", "C23", "C31"}
