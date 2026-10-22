"""Data contracts for the persistent single-field workbench."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from nongshi.schema_v2 import (
    DayWindow,
    FeedbackV2,
    FieldProfile,
    PlanItemV2,
    PlanVersionV2,
    RuleEvidenceV2,
)

FieldMode = Literal["real", "demo"]
EventSource = Literal["user", "assistant", "import"]


class WeatherLocationV3(BaseModel):
    query: str
    display_name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = "Asia/Shanghai"
    resolution: Literal["township", "county", "city", "unknown"] = "unknown"
    administrative_area: list[str] = Field(default_factory=list)
    geocoding_source: str = "Open-Meteo Geocoding API"


class FieldDraftV3(BaseModel):
    schema_version: Literal[3] = 3
    field_id: str = Field(min_length=1)
    mode: FieldMode = "real"
    profile: FieldProfile | None = None
    location: WeatherLocationV3 | None = None
    start_date: date | None = None
    days: list[DayWindow] = Field(default_factory=list)
    notes: str = ""
    input_source: Literal["user", "copied_legacy", "demo"] = "user"
    source_plan_id: str | None = None

    @model_validator(mode="after")
    def validate_field_and_schedule(self) -> FieldDraftV3:
        if self.profile is not None and self.profile.id != self.field_id:
            raise ValueError("地块档案编号必须与草稿编号一致")
        if self.days:
            if self.start_date is None:
                raise ValueError("填写逐日时段时需要指定计划起始日期")
            expected = [date.fromordinal(self.start_date.toordinal() + i) for i in range(7)]
            if [entry.day for entry in self.days] != expected:
                raise ValueError("每日条件和资源必须覆盖连续七天")
        return self


class FieldStateV3(BaseModel):
    id: str
    mode: FieldMode
    draft_version: int = Field(ge=1)
    fact_version: int = Field(ge=0)
    draft: FieldDraftV3
    latest_plan_id: str | None = None
    plan_stale: bool = False
    updated_at: AwareDatetime


class FactEventInputV3(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    feedback: FeedbackV2 | None = None
    source: EventSource = "user"
    correction_of: str | None = None
    retracts: bool = False

    @model_validator(mode="after")
    def validate_correction(self) -> FactEventInputV3:
        if not self.retracts and self.feedback is None:
            raise ValueError("现场事件需要有效反馈内容")
        if self.retracts and self.correction_of is None:
            raise ValueError("更正事件需要引用原事件")
        if self.feedback and self.feedback.kind == "observation":
            field = self.feedback.field
            value = self.feedback.value
            boolean_fields = {
                "maintenance_need_confirmed",
                "maintenance_problem_confirmed",
                "standing_water",
                "actual_rain_observed",
                "winter_before_context_confirmed",
                "pest_signs",
                "missing_seedlings",
                "crowding",
            }
            if field in boolean_fields and value is not None and not isinstance(value, bool):
                raise ValueError(f"{field} 的现场值必须是是/否/未知")
            if field == "ditch_connectivity" and value not in {None, "clear", "impaired"}:
                raise ValueError("沟渠连通情况只能是通畅、受阻或未知")
            if field == "maintenance_purpose" and value not in {
                None,
                "clear_existing_ditch",
                "other",
            }:
                raise ValueError("维护目的只能是清理既有沟渠、其他或未知")
            if field == "moisture" and value not in {None, "dry", "normal", "wet"}:
                raise ValueError("土壤墒情只能是偏干、适中、偏湿或未知")
            if field == "leaf_count" and (
                isinstance(value, bool)
                or (value is not None and not isinstance(value, (int, float)))
            ):
                raise ValueError("叶片数必须是非负数字或未知")
            if field == "leaf_count" and isinstance(value, (int, float)) and value < 0:
                raise ValueError("叶片数不能小于零")
        if self.feedback and self.feedback.kind == "workability":
            if any(
                value is not None and not isinstance(value, bool)
                for value in (self.feedback.site_workable, self.feedback.weather_workable)
            ):
                raise ValueError("作业条件只能是可作业、不可作业或待确认")
        return self


class FactEventV3(FactEventInputV3):
    field_id: str
    fact_version: int = Field(ge=1)
    created_at: AwareDatetime


class EventCommitResultV3(BaseModel):
    field_id: str
    fact_version: int = Field(ge=1)
    events: list[FactEventV3]
    plan_stale: bool
    replayed: bool = False


class RuleAdoptionRecordV3(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    field_id: str
    rule_id: str
    status: Literal["pending_review", "adopted", "rejected"]
    reviewer: str = ""
    reviewed_at: AwareDatetime
    notes: str = ""
    ruleset_version: str
    source_fingerprints: list[str] = Field(default_factory=list)


class PlanChangeV3(BaseModel):
    task_id: str
    kind: Literal["added", "moved", "removed", "cancelled", "unchanged", "completed"]
    old_start_at: AwareDatetime | None = None
    old_end_at: AwareDatetime | None = None
    new_start_at: AwareDatetime | None = None
    new_end_at: AwareDatetime | None = None
    reason: str


class PlanVersionV3(BaseModel):
    schema_version: Literal[3] = 3
    id: str = Field(default_factory=lambda: str(uuid4()))
    field_id: str
    parent_id: str | None = None
    created_at: AwareDatetime
    draft_version: int = Field(ge=1)
    fact_version: int = Field(ge=0)
    plan: PlanVersionV2
    weather_snapshot_id: str | None = None
    ruleset_version: str
    rule_adoption_ids: list[str] = Field(default_factory=list)
    source: Literal["manual", "assistant", "demo", "copied_legacy"] = "manual"
    changes: list[PlanChangeV3] = Field(default_factory=list)
    calculation_basis: list[str] = Field(default_factory=list)

    @property
    def items(self) -> list[PlanItemV2]:
        return self.plan.items


class PlanPreviewV3(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    field_id: str
    expected_draft_version: int = Field(ge=1)
    expected_fact_version: int = Field(ge=0)
    expected_plan_id: str | None = None
    created_at: AwareDatetime
    version: PlanVersionV3
    status: Literal["preview", "adopted", "stale", "cancelled"] = "preview"


class PlanAdoptionResultV3(BaseModel):
    version: PlanVersionV3
    created_new_version: bool
    replayed: bool = False


class FieldEvaluationV3(BaseModel):
    field_id: str
    draft_version: int
    fact_version: int
    evaluated_at: AwareDatetime
    profile_ready: bool
    items: list[PlanItemV2] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    ruleset_version: str
    status_label: str = "开发验证，适用性待复核"


class RuleSearchQueryV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    province: str | None = None
    province_confirmed: bool = False
    crop: str | None = None
    crop_confirmed: bool = False
    stage: str | None = None
    stage_confirmed: bool = False
    field_type: Literal["lowland_rice_stubble", "dry_hilly", "other"] | None = None
    task_type: Literal["inspection", "maintenance", "all"] = "all"
    problem: str | None = None
    problem_confirmed: bool | None = None
    maintenance_requested: bool | None = None
    maintenance_purpose: Literal["clear_existing_ditch", "other"] | None = None
    winter_before_context_confirmed: bool | None = None
    actual_rain_observed: bool | None = None


class RuleSupplementarySourceV3(BaseModel):
    """A04 补充来源展示项；单独标识，不改写原 C 卡的适用性、来源指纹或启用状态。"""

    rule_id: str
    source_id: str
    source_title: str
    publisher: str
    source_date: str
    source_url: str
    locator: str
    use: str
    access_date: str
    source_sha256: str
    archive_type: str
    status_label: str = "A04 补充来源；不改变原卡适用性与启用状态"


class RuleSearchHitV3(BaseModel):
    rule_id: str
    title: str
    applicability: Literal["matched", "out_of_scope", "pending", "no_basis", "reference"]
    reason: str
    evidence: list[RuleEvidenceV2] = Field(default_factory=list)
    supplementary_sources: list[RuleSupplementarySourceV3] = Field(default_factory=list)
    runtime_enabled: bool = False


class RuleSearchResultV3(BaseModel):
    status: Literal["matched", "out_of_scope", "pending", "no_basis"]
    reason: str
    ruleset_version: str
    profile_label: str = "开发验证，适用性待复核"
    hits: list[RuleSearchHitV3] = Field(default_factory=list)


class GeocodingResultV3(BaseModel):
    id: str
    name: str
    latitude: float
    longitude: float
    timezone: str = "Asia/Shanghai"
    country: str = ""
    country_code: str = ""
    admin1: str | None = None
    admin2: str | None = None
    admin3: str | None = None
    admin4: str | None = None
    feature_code: str = ""
    population: int | None = None
    query: str = ""
    match_source: Literal["open_meteo", "local_gazetteer"] = "open_meteo"
    resolution: Literal["township", "county", "city", "unknown"] = "unknown"
    match_reason: str = ""

    @property
    def label(self) -> str:
        areas = [self.admin1, self.admin2, self.admin3, self.name]
        return " · ".join(part for part in areas if part)


class WeatherDayV3(BaseModel):
    day: date
    weather_code: int | None = None
    temperature_max: float | None = None
    temperature_min: float | None = None
    precipitation_sum: float | None = None
    rain_sum: float | None = None
    showers_sum: float | None = None
    wind_speed_max: float | None = None


class WeatherSnapshotV3(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    field_id: str
    source: str = "Open-Meteo Forecast API"
    location: WeatherLocationV3
    requested_latitude: float
    requested_longitude: float
    grid_latitude: float
    grid_longitude: float
    timezone: str = "Asia/Shanghai"
    fetched_at: AwareDatetime
    coverage_start: date
    coverage_end: date
    units: dict[str, str]
    days: list[WeatherDayV3]
    cache_expires_at: AwareDatetime
    is_stale: bool = False

    @model_validator(mode="after")
    def validate_coverage(self) -> WeatherSnapshotV3:
        if self.coverage_end < self.coverage_start:
            raise ValueError("天气覆盖结束日期不能早于开始日期")
        expected = [
            date.fromordinal(self.coverage_start.toordinal() + i)
            for i in range((self.coverage_end - self.coverage_start).days + 1)
        ]
        if [item.day for item in self.days] != expected:
            raise ValueError("天气日期必须完整覆盖请求范围")
        return self


class AgentToolMetricV3(BaseModel):
    name: str
    status: Literal["success", "invalid_arguments", "failed", "limit_reached"]
    elapsed_ms: int = Field(ge=0)


class AgentRunV3(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    field_id: str
    model: str
    started_at: AwareDatetime
    elapsed_ms: int = Field(ge=0)
    request_count: int = Field(ge=0)
    tool_count: int = Field(ge=0)
    status: Literal[
        "success",
        "missing_credentials",
        "auth_error",
        "insufficient_balance",
        "rate_limited",
        "timeout",
        "invalid_response",
        "error",
    ]
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    tools: list[str] = Field(default_factory=list)
    tool_metrics: list[AgentToolMetricV3] = Field(default_factory=list)


class AgentProposalV3(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["field_update", "facts"]
    field_id: str
    expected_draft_version: int | None = None
    expected_fact_version: int | None = None
    draft: FieldDraftV3 | None = None
    events: list[FactEventInputV3] = Field(default_factory=list)
    summary: str
    changes: dict[str, object] = Field(default_factory=dict)


class AgentToolTraceV3(BaseModel):
    name: str
    status: Literal["success", "invalid_arguments", "failed", "limit_reached"]
    elapsed_ms: int = Field(ge=0)
    result_summary: str


class AgentResponseV3(BaseModel):
    run_id: str
    status: Literal[
        "success",
        "missing_credentials",
        "auth_error",
        "insufficient_balance",
        "rate_limited",
        "timeout",
        "invalid_response",
        "error",
    ]
    text: str = ""
    error_message: str = ""
    model: str
    request_count: int = Field(ge=0)
    tools: list[AgentToolTraceV3] = Field(default_factory=list)
    proposals: list[AgentProposalV3] = Field(default_factory=list)
    previews: list[PlanPreviewV3] = Field(default_factory=list)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
