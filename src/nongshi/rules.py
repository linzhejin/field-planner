"""Load rule evidence without turning source prose into executable policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_ROOT = PROJECT_ROOT / "rules"
CARD_PACKAGE = RULES_ROOT / "农时窗口_规则卡片_融合修订版_20261017"
DELIVERY_PACKAGE = RULES_ROOT / "农时窗口_规则与验证交付包_20261017"
DEVELOPMENT_RULE_IDS = frozenset({"C11", "C18", "C19", "C20", "C23", "C24", "C31"})


@dataclass(frozen=True)
class SourceRecord:
    id: str
    title: str
    publisher: str
    date: str
    url: str
    relative_file: str
    sha256: str
    local_sha256: str


@dataclass(frozen=True)
class RuleEvidenceRecord:
    source_id: str
    quote: str
    locator: str
    pdf_pages: tuple[int, ...]
    source_sha256: str
    verification: str


@dataclass(frozen=True)
class RuleCardRecord:
    id: str
    title: str
    category: str
    applicability: str
    condition: str
    source_supported_knowledge: str
    prototype_behavior: str
    missing_behavior: str
    limits: str
    runtime_enabled: bool
    evidence: tuple[RuleEvidenceRecord, ...]

    @property
    def source_text(self) -> str:
        return self.evidence[0].quote if self.evidence else ""


@dataclass(frozen=True)
class RuleCatalog:
    delivery_version: str
    as_of: str
    profile_label: str
    development_rule_ids: frozenset[str]
    cards: dict[str, RuleCardRecord]
    sources: dict[str, SourceRecord]
    legacy_rule_version: str
    legacy_mapping_type: str
    old_rule_mapping: Any
    engineering_rules_version: str
    engineering_rules: tuple[dict[str, Any], ...]
    input_contract: dict[str, Any]

    def plan_metadata(self) -> dict[str, Any]:
        return {
            "ruleset_version": self.delivery_version,
            "legacy_rule_version": self.legacy_rule_version,
            "legacy_mapping_type": self.legacy_mapping_type,
            "engineering_rules_version": self.engineering_rules_version,
            "engineering_rule_ids": sorted(str(row["id"]) for row in self.engineering_rules),
        }

    def evidence_for(self, rule_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for rule_id in rule_ids:
            card = self.cards[rule_id]
            for item in card.evidence:
                source = self.sources[item.source_id]
                evidence.append(
                    {
                        "rule_id": card.id,
                        "title": card.title,
                        "pack_version": self.delivery_version,
                        "evidence_version": self.as_of,
                        "source_id": source.id,
                        "source_title": source.title,
                        "publisher": source.publisher,
                        "source_date": source.date,
                        "source_url": source.url,
                        "pdf_pages": list(item.pdf_pages),
                        "quote": item.quote,
                        "applicability_text": card.applicability,
                        "condition_text": card.condition,
                        "limits": card.limits,
                        "source_sha256": item.source_sha256,
                        "runtime_enabled": card.runtime_enabled,
                    }
                )
        return evidence


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"规则资料无法读取：{path.name}: {error}") from error


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ValueError(f"规则引用文件缺失或不可读：{path}") from error
    return digest.hexdigest()


@lru_cache(maxsize=1)
def load_rule_catalog() -> RuleCatalog:
    """Check IDs, mappings, source links, page locators, and original fingerprints."""
    card_root = CARD_PACKAGE / "结构化资料"
    card_data = _read_json(card_root / "规则卡片.json")
    source_data = _read_json(card_root / "来源索引.json")
    mapping_data = _read_json(card_root / "新旧规则映射.json")
    adoption_data = _read_json(DELIVERY_PACKAGE / "结构化交付" / "输入输出约定.json")
    engineering_data = _read_json(DELIVERY_PACKAGE / "结构化交付" / "工程约定.json")
    legacy_rules_data = _read_json(DELIVERY_PACKAGE / "结构化交付" / "规则卡片.json")

    raw_sources = {str(row["id"]): row for row in source_data}
    raw_cards = {str(row["id"]): row for row in card_data.get("cards", [])}
    if set(raw_cards) != {f"C{number:02}" for number in range(1, 32)}:
        raise ValueError("C规则卡编号不完整或有重复")
    if DEVELOPMENT_RULE_IDS - raw_cards.keys():
        raise ValueError("开发验证名单引用了不存在的C规则卡")

    sources: dict[str, SourceRecord] = {}
    package_root = CARD_PACKAGE
    for source_id, row in raw_sources.items():
        relative_file = str(row.get("file", ""))
        if not relative_file:
            raise ValueError(f"来源索引 {source_id} 缺少文件路径")
        file_path = package_root / Path(relative_file)
        expected = str(row.get("sha256", "")).lower()
        actual = _file_hash(file_path)
        if expected != actual:
            raise ValueError(f"来源 {source_id} 指纹不匹配：{relative_file}")
        sources[source_id] = SourceRecord(
            id=source_id,
            title=str(row.get("title", "")),
            publisher=str(row.get("publisher", "")),
            date=str(row.get("date", "")),
            url=str(row.get("url", "")),
            relative_file=relative_file,
            sha256=expected,
            local_sha256=actual,
        )

    cards: dict[str, RuleCardRecord] = {}
    for rule_id, row in raw_cards.items():
        evidence_rows = row.get("evidence", [])
        evidence: list[RuleEvidenceRecord] = []
        for item in evidence_rows:
            source_id = str(item.get("source_id", ""))
            if source_id not in sources:
                raise ValueError(f"规则 {rule_id} 引用了来源索引中不存在的 {source_id}")
            pages = tuple(int(page) for page in item.get("pdf_pages", []))
            if not pages or any(page < 1 for page in pages):
                raise ValueError(f"规则 {rule_id} 的PDF页码缺失或无效")
            source_hash = str(item.get("source_sha256", "")).lower()
            if source_hash != sources[source_id].sha256:
                raise ValueError(f"规则 {rule_id} 的来源指纹与索引不一致")
            evidence.append(
                RuleEvidenceRecord(
                    source_id=source_id,
                    quote=str(item.get("quote", "")),
                    locator=str(item.get("locator", "")),
                    pdf_pages=pages,
                    source_sha256=source_hash,
                    verification=str(item.get("verification", "")),
                )
            )
        cards[rule_id] = RuleCardRecord(
            id=rule_id,
            title=str(row.get("title", "")),
            category=str(row.get("category", "")),
            applicability=str(row.get("applicability", "")),
            condition=str(row.get("condition", "")),
            source_supported_knowledge=str(row.get("source_supported_knowledge", "")),
            prototype_behavior=str(row.get("prototype_behavior", "")),
            missing_behavior=str(row.get("missing_behavior", "")),
            limits=str(row.get("limits", "")),
            runtime_enabled=bool(row.get("runtime_enabled", False)),
            evidence=tuple(evidence),
        )

    # The engineering mapping stays separately labeled as R/D context; C IDs are never rewritten.
    mapping_rows = (
        mapping_data.get("mapping", mapping_data)
        if isinstance(mapping_data, dict)
        else mapping_data
    )
    if not isinstance(mapping_rows, dict):
        raise ValueError("R到C主题映射格式错误")
    for old_id, mapped_ids in mapping_rows.items():
        if not str(old_id).startswith("R"):
            raise ValueError(f"旧规则映射编号无效：{old_id}")
        for mapped_id in mapped_ids if isinstance(mapped_ids, list) else [mapped_ids]:
            identifier = str(mapped_id).split("；", maxsplit=1)[0]
            if identifier.startswith("C") and identifier not in raw_cards:
                raise ValueError(f"旧规则映射引用了不存在的C规则：{identifier}")
    engineering_ids = {str(row.get("id", "")) for row in engineering_data.get("rules", [])}
    if engineering_ids != {f"D{number:02}" for number in range(1, 13)}:
        raise ValueError("D01—D12工程约定编号不完整")
    legacy_ids = {str(row.get("id", "")) for row in legacy_rules_data.get("cards", [])}
    if legacy_ids != {f"R{number:02}" for number in range(1, 13)}:
        raise ValueError("R01—R12参考规则编号不完整")
    if set(mapping_rows) != legacy_ids:
        raise ValueError("R编号映射与参考规则卡编号不一致")
    delivery_version = str(card_data.get("version", ""))
    if not delivery_version:
        raise ValueError("规则卡交付版本缺失")
    return RuleCatalog(
        delivery_version=delivery_version,
        as_of=str(card_data.get("as_of", "")),
        profile_label="开发验证，适用性待复核",
        development_rule_ids=DEVELOPMENT_RULE_IDS,
        cards=cards,
        sources=sources,
        legacy_rule_version=str(legacy_rules_data.get("version", "")),
        legacy_mapping_type=str(mapping_data.get("mapping_type", "")),
        old_rule_mapping=mapping_rows,
        engineering_rules_version=str(adoption_data.get("version", "")),
        engineering_rules=tuple(engineering_data.get("rules", [])),
        input_contract=adoption_data,
    )
