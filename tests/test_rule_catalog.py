from __future__ import annotations

import pytest
from pypdf import PdfReader

import nongshi.rules as rules
from nongshi.rules import CARD_PACKAGE, DEVELOPMENT_RULE_IDS, load_rule_catalog


def test_catalog_keeps_original_cards_disabled_and_selects_only_agreed_candidates() -> None:
    catalog = load_rule_catalog()

    assert len(catalog.cards) == 31
    assert set(catalog.development_rule_ids) == DEVELOPMENT_RULE_IDS
    assert catalog.delivery_version
    assert catalog.legacy_rule_version
    assert "非等价替换" in catalog.legacy_mapping_type
    assert catalog.engineering_rules_version
    assert {row["id"] for row in catalog.engineering_rules} == {
        f"D{number:02}" for number in range(1, 13)
    }
    assert all(not catalog.cards[rule_id].runtime_enabled for rule_id in DEVELOPMENT_RULE_IDS)
    assert catalog.profile_label == "开发验证，适用性待复核"


def test_selected_card_evidence_matches_source_index_and_local_fingerprints() -> None:
    catalog = load_rule_catalog()

    for rule_id in DEVELOPMENT_RULE_IDS:
        card = catalog.cards[rule_id]
        assert card.evidence
        for evidence in card.evidence:
            assert evidence.source_id in catalog.sources
            source = catalog.sources[evidence.source_id]
            assert evidence.source_sha256 == source.sha256
            assert source.local_sha256 == source.sha256
            assert evidence.pdf_pages and all(page >= 1 for page in evidence.pdf_pages)
            assert evidence.quote.strip()


def test_every_c_card_locator_fits_its_local_pdf() -> None:
    catalog = load_rule_catalog()
    page_counts = {
        source_id: len(PdfReader(CARD_PACKAGE / source.relative_file).pages)
        for source_id, source in catalog.sources.items()
    }

    for card in catalog.cards.values():
        for evidence in card.evidence:
            assert max(evidence.pdf_pages) <= page_counts[evidence.source_id], card.id


def test_rule_evidence_is_preserved_as_source_text_not_executable_condition() -> None:
    catalog = load_rule_catalog()
    card = catalog.cards["C20"]

    assert card.condition
    assert card.limits
    assert card.runtime_enabled is False
    assert card.source_text == card.evidence[0].quote


def test_missing_rule_package_fails_closed(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    rules.load_rule_catalog.cache_clear()
    monkeypatch.setattr(rules, "CARD_PACKAGE", tmp_path / "missing-package")

    with pytest.raises(ValueError, match="规则资料无法读取"):
        rules.load_rule_catalog()

    rules.load_rule_catalog.cache_clear()
