"""Condition-aware rule lookup; source prose remains evidence, never executable code."""

from __future__ import annotations

from nongshi.rules import RuleCatalog, load_rule_catalog
from nongshi.schema_v2 import RuleEvidenceV2
from nongshi.schema_v3 import RuleSearchHitV3, RuleSearchQueryV3, RuleSearchResultV3

SUPPORTED_PROBLEMS = {
    "inspection",
    "maintenance",
    "drainage",
    "ditch",
    "沟渠",
    "沟系",
    "巡田",
    "苗情",
    "墒情",
}


class RuleRetrieverV3:
    def __init__(self, catalog: RuleCatalog | None = None) -> None:
        self.catalog = catalog or load_rule_catalog()

    def search(self, query: RuleSearchQueryV3) -> RuleSearchResultV3:
        if query.problem and query.problem.strip().casefold() not in SUPPORTED_PROBLEMS:
            return RuleSearchResultV3(
                status="no_basis",
                reason="当前规则资料没有直接依据，不能仅凭关键词关联或生成农业建议。",
                ruleset_version=self.catalog.delivery_version,
            )
        rule_ids = self._rule_ids(query)
        if not rule_ids:
            return RuleSearchResultV3(
                status="no_basis",
                reason="当前条件没有对应的规则卡。",
                ruleset_version=self.catalog.delivery_version,
            )
        hits = [self._hit(rule_id, query) for rule_id in rule_ids]
        statuses = {hit.applicability for hit in hits}
        if "pending" in statuses:
            status = "pending"
            reasons = list(
                dict.fromkeys(hit.reason for hit in hits if hit.applicability == "pending")
            )
            reason = "适用条件待确认：" + "；".join(reasons)
        elif statuses == {"out_of_scope"}:
            status = "out_of_scope"
            reason = "当前地区、作物、生育期或田型超出候选规则的适用范围。"
        elif "matched" in statuses:
            status = "matched"
            reason = "找到条件相符的规则候选；这只表示可查阅依据，不代表现场需求或规则已正式采用。"
        else:
            status = "no_basis"
            reason = "当前资料没有可用依据。"
        return RuleSearchResultV3(
            status=status,
            reason=reason,
            ruleset_version=self.catalog.delivery_version,
            hits=hits,
        )

    @staticmethod
    def _rule_ids(query: RuleSearchQueryV3) -> list[str]:
        if query.task_type == "inspection":
            result = ["C31", "C11"]
        elif query.task_type == "maintenance":
            result = ["C23"]
        else:
            result = ["C31", "C11", "C23"]
        result.extend(["C19", "C20"])
        if query.winter_before_context_confirmed is True:
            result.append("C18")
        if query.actual_rain_observed is True:
            result.append("C24")
        return list(dict.fromkeys(result))

    def _hit(self, rule_id: str, query: RuleSearchQueryV3) -> RuleSearchHitV3:
        card = self.catalog.cards[rule_id]
        evidence = [RuleEvidenceV2(**row) for row in self.catalog.evidence_for([rule_id])]
        scope_reason = self._scope_reason(query)
        if scope_reason:
            applicability = "out_of_scope" if scope_reason.startswith("超出") else "pending"
            return RuleSearchHitV3(
                rule_id=rule_id,
                title=card.title,
                applicability=applicability,
                reason=scope_reason,
                evidence=evidence,
                runtime_enabled=card.runtime_enabled,
            )
        if rule_id == "C19":
            if query.field_type is None:
                applicability, reason = "pending", "需要确认田型，才能判断低洼/稻茬田条件。"
            elif query.field_type != "lowland_rice_stubble":
                applicability, reason = "out_of_scope", "超出低洼/稻茬田适用条件。"
            else:
                applicability, reason = (
                    "matched",
                    "田型与规则适用条件相符；仍需单独确认现场问题和任务需求。",
                )
        elif rule_id == "C20":
            if query.field_type is None:
                applicability, reason = "pending", "需要确认田型，才能判断丘陵旱地条件。"
            elif query.field_type != "dry_hilly":
                applicability, reason = "out_of_scope", "超出丘陵旱地适用条件。"
            else:
                applicability, reason = (
                    "matched",
                    "田型与规则适用条件相符；仍需单独确认现场问题和任务需求。",
                )
        elif rule_id == "C23":
            if query.field_type is None:
                applicability, reason = "pending", "需要先确认田型。"
            elif query.field_type == "other":
                applicability, reason = "out_of_scope", "当前田型超出维护候选的首版适用范围。"
            elif query.problem_confirmed is False or query.maintenance_requested is False:
                applicability, reason = "out_of_scope", "用户已确认无相应问题或不需要维护。"
            elif query.problem_confirmed is not True or query.maintenance_requested is not True:
                applicability, reason = (
                    "pending",
                    "现场问题和维护需求都需要用户确认；资料不能推定现场确有堵塞或需要维护。",
                )
            else:
                applicability, reason = (
                    "matched",
                    "田型、现场问题和维护需求均已确认；天气与进田条件仍需另行判断。",
                )
        elif rule_id == "C18":
            if query.winter_before_context_confirmed is True:
                applicability, reason = "matched", "用户已确认冬前情境；仍需按卡片限制复核。"
            else:
                applicability, reason = "pending", "冬前情境尚未确认。"
        elif rule_id == "C24":
            if query.actual_rain_observed is True:
                applicability, reason = "matched", "已记录实际降雨；预报不能替代实况观察。"
            else:
                applicability, reason = (
                    "pending",
                    "需要确认实际降雨情况，不能把天气预报当成现场事实。",
                )
        else:
            applicability, reason = (
                "matched",
                "地区、作物和生育期与首版范围相符；规则本身仍处于开发验证。",
            )
        return RuleSearchHitV3(
            rule_id=rule_id,
            title=card.title,
            applicability=applicability,
            reason=reason,
            evidence=evidence,
            runtime_enabled=card.runtime_enabled,
        )

    @staticmethod
    def _scope_reason(query: RuleSearchQueryV3) -> str:
        if query.province and query.province != "湖北":
            return "超出湖北地区范围。"
        if query.crop and query.crop != "油菜":
            return "超出油菜作物范围。"
        if query.stage and query.stage != "苗期":
            return "超出苗期范围。"
        missing: list[str] = []
        if query.province is None or not query.province_confirmed:
            missing.append("地区")
        if query.crop is None or not query.crop_confirmed:
            missing.append("作物")
        if query.stage is None or not query.stage_confirmed:
            missing.append("生育期")
        return f"待确认{'、'.join(missing)}。" if missing else ""
