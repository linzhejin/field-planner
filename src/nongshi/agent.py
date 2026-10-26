"""DeepSeek Chat Completions orchestration with bounded, validated local tools."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from copy import copy
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from nongshi.agent_jobs import AgentCancellation, AgentProgress
from nongshi.config import Settings
from nongshi.feedback_time import ground_completed_time
from nongshi.model_http import ModelBudgetExceeded, post_model_json
from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.schema_v2 import LOCAL_ZONE, TimeWindow
from nongshi.schema_v3 import (
    AgentProposalV3,
    AgentResponseV3,
    AgentRunV3,
    AgentToolMetricV3,
    AgentToolTraceV3,
    AgentWorkflowStepV3,
    FactEventInputV3,
    GeocodingResultV3,
    PlanPreviewV3,
    RuleSearchQueryV3,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.tasks_v4 import RecurrenceV4, TaskCategoryV4, TaskKindV4, TaskSeriesV4
from nongshi.weather import OpenMeteoService, WeatherServiceError
from nongshi.workflows import (
    CAPABILITIES,
    DeclareRequestArgs,
    allowed_capabilities,
)

MAX_MODEL_REQUESTS = 3
MAX_TOOL_EXECUTIONS = 6


class _GuardedAgentStore:
    """Reject late assistant writes after its UI turn has been cancelled."""

    def __init__(self, store: Any, cancellation: AgentCancellation) -> None:
        self._store = store
        self._cancellation = cancellation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def save_preview(self, *args: Any, **kwargs: Any) -> Any:
        return self._cancellation.persist(lambda: self._store.save_preview(*args, **kwargs))

    def save_agent_run(self, *args: Any, **kwargs: Any) -> Any:
        return self._cancellation.persist(lambda: self._store.save_agent_run(*args, **kwargs))

    def save_weather_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        return self._cancellation.persist(
            lambda: self._store.save_weather_snapshot(*args, **kwargs)
        )


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFieldArgs(ToolArgs):
    field_id: str


class EvaluateFieldArgs(ToolArgs):
    field_id: str


class SearchRulesArgs(ToolArgs):
    query: RuleSearchQueryV3


class QueryWeatherArgs(ToolArgs):
    field_id: str
    start_date: str
    end_date: str


class QueryCurrentWeatherArgs(ToolArgs):
    field_id: str


class SearchLocationsArgs(ToolArgs):
    field_id: str
    query: str = Field(min_length=2, max_length=160)


class ProposeLocationArgs(ToolArgs):
    field_id: str
    candidate_id: str


class PreviewPlanArgs(ToolArgs):
    field_id: str


class ProposeFieldUpdateArgs(ToolArgs):
    field_id: str
    changes: dict[str, Any] = Field(min_length=1)


class ProposedCompleted(ToolArgs):
    kind: Literal["completed"]
    at: AwareDatetime
    task_id: str = Field(min_length=1)
    plot_id: str | None = None
    day: date | None = None


class ProposedObservation(ToolArgs):
    kind: Literal["observation"]
    at: AwareDatetime
    plot_id: str
    field: str
    value: str | bool | float | None = None
    day: date | None = None


class ProposedWorkability(ToolArgs):
    kind: Literal["workability"]
    at: AwareDatetime
    plot_id: str
    day: date
    site_workable: bool | None = None
    weather_workable: bool | None = None

    @model_validator(mode="after")
    def confirm_explicit_change(self) -> ProposedWorkability:
        if not {"site_workable", "weather_workable"} & self.model_fields_set:
            raise ValueError("作业条件反馈须明确填写可作业、不可作业或未知")
        return self


class ProposedResource(ToolArgs):
    kind: Literal["resource"]
    at: AwareDatetime
    day: date
    intervals: list[TimeWindow]
    resource_minutes: int | None = Field(default=None, ge=0)
    plot_id: str | None = None


ProposedFeedback = Annotated[
    ProposedCompleted | ProposedObservation | ProposedWorkability | ProposedResource,
    Field(discriminator="kind"),
]


class ProposedEvent(ToolArgs):
    event_id: str | None = None
    source: Literal["user", "assistant", "import"] = "user"
    source_quote: str | None = None
    feedback: ProposedFeedback | None = None
    correction_of: str | None = None
    retracts: bool = False


class ProposeFeedbackArgs(ToolArgs):
    field_id: str
    events: list[ProposedEvent] = Field(min_length=1, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def reject_mixed_feedback(cls, value: Any) -> Any:
        if isinstance(value, dict):
            normalized: list[Any] = []
            for event in value.get("events") or []:
                if isinstance(event, dict) and "feedback" not in event and "kind" in event:
                    envelope = {"event_id", "source", "source_quote", "correction_of", "retracts"}
                    event = {
                        **{key: item for key, item in event.items() if key in envelope},
                        "feedback": {
                            key: item for key, item in event.items() if key not in envelope
                        },
                    }
                normalized.append(event)
                feedback = event.get("feedback") if isinstance(event, dict) else None
                if not isinstance(feedback, dict):
                    continue
                if feedback.get("kind") == "workability" and (
                    "intervals" in feedback or "resource_minutes" in feedback
                ):
                    raise ValueError("作业条件反馈不能包含可用时段或人员工时")
                if feedback.get("kind") == "resource" and (
                    "site_workable" in feedback or "weather_workable" in feedback
                ):
                    raise ValueError("人员时间反馈不能包含田间作业条件")
            return {**value, "events": normalized}
        return value

    def validated_events(self) -> list[FactEventInputV3]:
        return [
            FactEventInputV3.model_validate(
                event.model_dump(mode="python", exclude_unset=True, exclude={"source_quote"})
            )
            for event in self.events
        ]


class ProposeTaskSeriesArgs(ToolArgs):
    field_id: str
    source_quote: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    purpose: str = ""
    task_type: TaskKindV4 = "custom"
    category: TaskCategoryV4 = "onsite"
    recurrence: RecurrenceV4 = "once"
    anchor_date: date
    end_date: date | None = None
    selected_dates: list[date] = Field(default_factory=list, max_length=3660)
    interval_days: int | None = Field(default=None, gt=0, le=365)
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
    priority: int = Field(default=2, ge=1, le=3)
    earliest_date: date | None = None
    due_date: date | None = None
    prerequisites: list[str] = Field(default_factory=list)
    necessary_conditions: list[str] = Field(default_factory=list)
    conditions_confirmed: bool | None = None
    steps: list[str] = Field(default_factory=list)
    result_fields: list[str] = Field(default_factory=list)
    completion_standard: str = ""
    follow_up: str = ""


class DeclaredEvent(ProposedEvent):
    source_quote: str = Field(
        min_length=1,
        description="与这条事实对应的用户原话，必须逐字摘自本轮用户消息",
    )


class AgentDeclareRequestArgs(DeclareRequestArgs):
    events: list[DeclaredEvent] = Field(default_factory=list, max_length=10)


class ModelFunction(BaseModel):
    name: str
    arguments: str


class ModelToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ModelFunction


class ModelMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ModelToolCall] = Field(default_factory=list)


class ModelChoice(BaseModel):
    message: ModelMessage
    finish_reason: str | None = None


class ModelUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ModelCompletion(BaseModel):
    choices: list[ModelChoice] = Field(min_length=1)
    usage: ModelUsage | None = None


def _tool(name: str, description: str, arguments: type[BaseModel]) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": arguments.model_json_schema(),
        },
    }


ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "declare_request": AgentDeclareRequestArgs,
    "read_field": ReadFieldArgs,
    "search_rules": SearchRulesArgs,
    "evaluate_field": EvaluateFieldArgs,
    "query_weather": QueryWeatherArgs,
    "query_current_weather": QueryCurrentWeatherArgs,
    "search_locations": SearchLocationsArgs,
    "propose_location": ProposeLocationArgs,
    "preview_plan": PreviewPlanArgs,
    "propose_field_update": ProposeFieldUpdateArgs,
    "propose_feedback": ProposeFeedbackArgs,
    "propose_task_series": ProposeTaskSeriesArgs,
}
TOOL_DEFINITIONS: list[dict[str, object]] = [
    _tool(name, capability.description, ARGUMENT_MODELS[name])
    for name, capability in CAPABILITIES.items()
]


class AgentServiceV3:
    def __init__(
        self,
        settings: Settings,
        workbench: WorkbenchServiceV3,
        *,
        weather: OpenMeteoService | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic=time.perf_counter,
        evaluation_rule_query: Callable[[], RuleSearchQueryV3] | None = None,
    ) -> None:
        self.settings = settings
        self.workbench = workbench
        self.weather = weather or OpenMeteoService(
            store=workbench.store,
            timeout_seconds=settings.weather_timeout_seconds,
            cache_hours=settings.weather_cache_hours,
        )
        self._transport = transport
        self._monotonic = monotonic
        self._retriever = RuleRetrieverV3(workbench.catalog)
        self._evaluation_rule_query = evaluation_rule_query
        self._geocoding_candidates: dict[str, tuple[str, GeocodingResultV3]] = {}

    def close(self) -> None:
        self.weather.close()

    def run(
        self,
        field_id: str,
        user_message: str,
        *,
        conversation: list[dict[str, str]] | None = None,
        received_at: datetime | None = None,
        progress: Callable[[AgentProgress], None] | None = None,
        cancellation: AgentCancellation | None = None,
    ) -> AgentResponseV3:
        """Execute a turn with per-run state so cancelled network results stay private."""
        execution = self
        if cancellation is not None:
            cancellation.check()
            execution = copy(self)
            execution.workbench = copy(self.workbench)
            execution.workbench.store = _GuardedAgentStore(self.workbench.store, cancellation)
            execution.weather = copy(self.weather)
            if getattr(self.weather, "store", None) is not None:
                execution.weather.store = _GuardedAgentStore(self.weather.store, cancellation)
            execution._geocoding_candidates = dict(self._geocoding_candidates)
        response = execution._run(
            field_id,
            user_message,
            conversation=conversation,
            received_at=received_at,
            progress=progress,
            cancellation=cancellation,
        )
        if cancellation is not None and response.status == "success":
            cancellation.persist(
                lambda: self._geocoding_candidates.update(execution._geocoding_candidates)
            )
        return response

    def _run(
        self,
        field_id: str,
        user_message: str,
        *,
        conversation: list[dict[str, str]] | None = None,
        received_at: datetime | None = None,
        progress: Callable[[AgentProgress], None] | None = None,
        cancellation: AgentCancellation | None = None,
    ) -> AgentResponseV3:
        started = self._monotonic()
        with self.workbench.store.connection_budget(min(1.0, self.settings.llm_timeout_seconds)):
            self.workbench.get_field(field_id)
        run_id = str(uuid4())
        # SYS-02：正式运行接口需要可注入的接收时间，便于离线适配器与三组对照固定时钟；
        # 默认仍取系统 UTC 时钟。
        started_at = received_at or datetime.now(UTC)
        model = self.settings.llm_model.strip() or "deepseek-flash"
        request_count = 0
        tool_count = 0
        tool_names: list[str] = []
        traces: list[AgentToolTraceV3] = []
        proposals: list[AgentProposalV3] = []
        previews: list[PlanPreviewV3] = []
        prompt_tokens = 0
        completion_tokens = 0
        token_usage_seen = False
        workflow_actions: set[str] | None = None
        declared_missing: list[str] = []
        runtime_missing: dict[str, str] = {}
        steps: list[AgentWorkflowStepV3] = []
        request_ids: list[str] = []

        def report(stage: str, status: str, tool_name: str = "") -> None:
            if cancellation is not None:
                cancellation.check()
            if progress is not None:
                progress(AgentProgress(stage, status, request_count, tool_name))
            if cancellation is not None:
                cancellation.check()

        def finish(
            status: str,
            *,
            text: str = "",
            error_message: str = "",
            business_outcome: str | None = None,
            error_category: str | None = None,
        ) -> AgentResponseV3:
            if cancellation is not None:
                cancellation.check()
            elapsed = max(0, int((self._monotonic() - started) * 1000))
            run = AgentRunV3(
                id=run_id,
                field_id=field_id,
                model=model,
                started_at=started_at,
                elapsed_ms=elapsed,
                request_count=request_count,
                tool_count=tool_count,
                status=status,  # type: ignore[arg-type]
                prompt_tokens=prompt_tokens if token_usage_seen else None,
                completion_tokens=completion_tokens if token_usage_seen else None,
                tools=tool_names,
                tool_metrics=[
                    AgentToolMetricV3(
                        name=trace.name,
                        status=trace.status,
                        elapsed_ms=trace.elapsed_ms,
                    )
                    for trace in traces
                ],
                tool_traces=traces,
                workflow_version="open-ga-reliability-v1",
                workflow_actions=sorted(workflow_actions or []),
                steps=[
                    *steps,
                    AgentWorkflowStepV3(stage="finished", status=status, elapsed_ms=elapsed),
                ],
                error_category=error_category or (status if status != "success" else None),
                request_ids=request_ids,
            )
            try:
                with self.workbench.store.connection_budget(1.0):
                    pending_ids = {
                        proposal.id for proposal in self.workbench.pending_agent_proposals(field_id)
                    }
                    self.workbench.store.save_agent_run(
                        run, [proposal for proposal in proposals if proposal.id not in pending_ids]
                    )
            except sqlite3.OperationalError:
                status = "error"
                text = ""
                error_message = "本地数据库正在写入；本轮提案未保存，请刷新后重试。"
                error_category = "database_busy"
                business_outcome = "failed"
                proposals.clear()
                previews.clear()
            report("finished", status)
            return AgentResponseV3(
                run_id=run_id,
                status=status,  # type: ignore[arg-type]
                text=text,
                error_message=error_message,
                model=model,
                request_count=request_count,
                tools=traces,
                proposals=proposals,
                previews=previews,
                prompt_tokens=prompt_tokens if token_usage_seen else None,
                completion_tokens=completion_tokens if token_usage_seen else None,
                workflow_actions=sorted(workflow_actions or []),
                business_outcome=business_outcome
                or ("failed" if status != "success" else "answered"),
                error_category=error_category or (status if status != "success" else None),
                request_ids=request_ids,
            )

        if self.settings.llm_provider.strip().casefold() != "deepseek":
            return finish("error", error_message="当前只配置了 DeepSeek Chat Completions。")
        api_key = self.settings.llm_api_key.get_secret_value().strip()
        if not api_key:
            return finish(
                "missing_credentials",
                error_message="助手暂时不可用：还没配置模型密钥。你可以手动填写。",
            )
        if not user_message.strip():
            return finish("invalid_response", error_message="请输入要处理的内容。")
        change_requested = bool(
            re.search(
                r"(?<!已)(?<!已经)(?<!刚)保存|记录|改为|改成|变为|做完了|完成了|已完成|发现|确认.*清理|"
                r"调整|修改|重排|(?:只有|仅有|只剩|有空).*?(?:上午|下午|晚上|\d{1,2}点)",
                user_message,
            )
        )
        completion_only_requested = bool(
            re.search(r"先(?:只)?记录(?:这条)?完成(?:事实|情况|记录)?", user_message)
        )
        preview_requested = bool(re.search(r"预览|生成.*计划|安排.*七天", user_message))

        messages: list[dict[str, object]] = [
            {
                "role": "system",
                "content": self._system_message(
                    field_id, received_at=started_at.astimezone(LOCAL_ZONE)
                )
                + "\n当前已保存状态（只读）："
                + json.dumps(self._field_context(field_id), ensure_ascii=False, default=str),
            }
        ]
        for item in (conversation or [])[-8:]:
            if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
                messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": user_message})
        requested_resource_days = self._resource_days_in_message(
            user_message,
            received_at=started_at,
            plan_start=self.workbench.get_field(field_id).draft.start_date,
        )
        saved_profile = self.workbench.get_field(field_id).draft.profile
        required_observations = {
            field: label
            for field, label in self._requested_observations(user_message).items()
            if not (
                completion_only_requested
                or (
                    field == "maintenance_purpose"
                    and saved_profile.maintenance_purpose == "clear_existing_ditch"
                    and not re.search(r"目的|为了|用于|改为|改成", user_message)
                )
            )
        }
        deferred_feedback_events: list[dict[str, object]] = []
        observation_retry_needed = False
        read_only_preview = bool(
            preview_requested
            and not change_requested
            and not requested_resource_days
            and not self._requested_observations(user_message)
            and not re.search(r"田里|地里|进田|下地|作业条件", user_message)
        )
        deadline = started + self.settings.llm_timeout_seconds
        last_tool_summary = ""
        read_cache: dict[str, dict[str, object]] = {}
        weather_attempt_failed = False
        weather_error_code: str | None = None
        weather_error_message = ""
        weather_failure_reviewed = False
        recovery_attempted = False
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"

        def missing_items() -> list[str]:
            # The model may ask redundant questions after it has supplied every
            # requested, locally grounded change. Keep rejected facts visible,
            # but do not let such questions downgrade a complete proposal.
            all_actions_proposed = bool(
                proposals
                and workflow_actions
                and self._actions_satisfied(workflow_actions, proposals, previews)
            )
            missing = [
                *([] if all_actions_proposed else declared_missing),
                *runtime_missing.values(),
            ]
            observed = {
                event.feedback.field
                for proposal in proposals
                for event in proposal.events
                if event.feedback and event.feedback.kind == "observation"
            }
            if change_requested or (workflow_actions and "observation" in workflow_actions):
                for field, label in required_observations.items():
                    if field not in observed:
                        missing.append(f"请确认{label}的现场情况")
            if workflow_actions:
                covered = {
                    event.feedback.kind
                    for proposal in proposals
                    for event in proposal.events
                    if event.feedback is not None
                }
                if "completed" in workflow_actions and "completed" not in covered:
                    missing.append("请提供该任务实际完成的准确日期和时间")
                if "resource" in workflow_actions and "resource" not in covered:
                    missing.append("请给出人员可用时段的具体日期、开始和结束时间")
                if "workability" in workflow_actions and "workability" not in covered:
                    missing.append("请确认当天的现场和天气作业条件")
                if "field_update" in workflow_actions and not any(
                    proposal.kind == "field_update" for proposal in proposals
                ):
                    missing.append("请明确需要修改的地块档案字段")
                if "location" in workflow_actions and not any(
                    proposal.kind == "field_update" for proposal in proposals
                ):
                    missing.append("请确认天气查询位置")
                if "task_series" in workflow_actions and not any(
                    proposal.kind == "task_series" for proposal in proposals
                ):
                    missing.append("请确认待办名称、日期范围和预计时长")
            proposed_resource_days = {
                event.feedback.day
                for proposal in proposals
                for event in proposal.events
                if event.feedback and event.feedback.kind == "resource"
            }
            for day in sorted(requested_resource_days - proposed_resource_days):
                missing.append(f"请确认{day.isoformat()}的人员可用时段")
            return list(dict.fromkeys(missing))[:3]

        def event_key(raw: dict[str, object]) -> str:
            """Identify the requested fact across a rejected value and its correction."""
            feedback = raw.get("feedback")
            if not isinstance(feedback, dict):
                return "feedback:unrecognized"
            kind = str(feedback.get("kind", "unrecognized"))
            identity = {
                "observation": feedback.get("field"),
                "resource": feedback.get("day"),
                "workability": feedback.get("day"),
                "completed": feedback.get("task_id"),
            }.get(kind)
            return f"feedback:{kind}:{identity}"

        def result_from_progress() -> AgentResponseV3 | None:
            missing = missing_items()
            if proposals:
                return finish(
                    "success",
                    text="；".join(missing) if missing else "已生成待确认内容，请逐项核对后保存。",
                    business_outcome="partial" if missing else "awaiting_confirmation",
                )
            if previews:
                return finish(
                    "success", text="已生成可采用的计划预览。", business_outcome="preview_ready"
                )
            if weather_error_code:
                next_action = {
                    "weather_missing_dates": (
                        "天气数据未覆盖完整七天；请核对计划日期后刷新天气重试。"
                    ),
                    "weather_invalid": "天气快照已过期或不可用；请刷新天气后重试。",
                }.get(
                    weather_error_code,
                    f"{weather_error_message}；请核对查询位置和计划日期后重试。",
                )
                return finish(
                    "error",
                    error_message=next_action,
                    error_category=weather_error_code,
                )
            if missing:
                return finish(
                    "success", text="；".join(missing), business_outcome="needs_information"
                )
            return None

        for _ in range(MAX_MODEL_REQUESTS):
            if cancellation is not None:
                cancellation.check()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return finish(
                    "timeout",
                    text=last_tool_summary,
                    error_message="处理超时，已保存的内容仍在。请重试或手动填写。",
                )
            request_count += 1
            steps.append(AgentWorkflowStepV3(stage="understanding", status="started"))
            report("model", "started")
            try:
                model_result = post_model_json(
                    url,
                    api_key=api_key,
                    payload={
                        "model": model,
                        "temperature": 0,
                        "max_tokens": 4096,
                        "messages": messages,
                        "tools": [
                            definition
                            for definition in TOOL_DEFINITIONS
                            if definition["function"]["name"]
                            in (
                                {"declare_request"}
                                if request_count == 1
                                else allowed_capabilities(workflow_actions)
                            )
                        ],
                        "tool_choice": (
                            {"type": "function", "function": {"name": "declare_request"}}
                            if request_count == 1
                            else "auto"
                        ),
                        "thinking": {"type": "disabled"},
                    },
                    deadline=deadline,
                    transport=self._transport,
                    monotonic=self._monotonic,
                )
                if cancellation is not None:
                    cancellation.check()
                response = model_result.response
                if model_result.request_id:
                    request_ids.append(model_result.request_id)
                if response.status_code >= 400:
                    status, message = self._provider_error(response.status_code)
                    return finish(status, text=last_tool_summary, error_message=message)
                payload = response.json()
                completion = ModelCompletion.model_validate(payload)
                if self._monotonic() >= deadline:
                    return finish(
                        "timeout",
                        text=last_tool_summary,
                        error_message="处理超时，已保存的内容仍在。请检查后重试。",
                    )
            except (httpx.TimeoutException, ModelBudgetExceeded):
                return finish(
                    "timeout",
                    text=last_tool_summary,
                    error_message="助手响应超时，已保存的内容仍在。请重试。",
                )
            except httpx.RequestError as error:
                category = (
                    "connection_tls" if isinstance(error, httpx.ConnectError) else "connection"
                )
                return finish(
                    "error",
                    text=last_tool_summary,
                    error_message="助手暂时连不上。请稍后重试，或手动填写。",
                    error_category=category,
                )
            except (ValueError, ValidationError):
                return finish(
                    "invalid_response",
                    text=last_tool_summary,
                    error_message="助手没有给出可用结果。请重试，或手动填写。",
                )

            if completion.usage:
                token_usage_seen = True
                prompt_tokens += completion.usage.prompt_tokens or 0
                completion_tokens += completion.usage.completion_tokens or 0
            message = completion.choices[0].message
            steps[-1] = AgentWorkflowStepV3(
                stage="understanding", status="success", elapsed_ms=model_result.elapsed_ms
            )
            report("model", "success")
            if not message.tool_calls:
                if not message.content:
                    return finish(
                        "invalid_response",
                        text=last_tool_summary,
                        error_message="模型没有返回说明或工具结果。",
                    )
                if preview_requested and not previews and not proposals:
                    if request_count < MAX_MODEL_REQUESTS:
                        messages.append({"role": "assistant", "content": message.content})
                        messages.append(
                            {"role": "user", "content": "请调用 preview_plan 生成实际预览。"}
                        )
                        continue
                    return finish(
                        "invalid_response",
                        error_message="未生成计划预览；已保存输入不受影响，可用结构化入口重试。",
                    )
                if (
                    workflow_actions
                    and not self._actions_satisfied(workflow_actions, proposals, previews)
                    and proposals
                ):
                    return finish(
                        "success",
                        text="部分事项已形成待确认修改，其余事项仍需补充或重新提出。",
                        business_outcome="partial",
                    )
                if (
                    change_requested
                    and not proposals
                    and not re.search(
                        r"请提供|需要.*确认|请先确认|无法确定|信息不足|缺少|不清楚",
                        message.content,
                    )
                ):
                    if request_count < MAX_MODEL_REQUESTS:
                        messages.append({"role": "assistant", "content": message.content})
                        messages.append(
                            {
                                "role": "user",
                                "content": "我要求修改已保存状态。请调用 propose_feedback 或 "
                                "propose_field_update 生成待确认提案；事实不足时明确列出缺项。",
                            }
                        )
                        continue
                    return finish(
                        "invalid_response",
                        text=last_tool_summary,
                        error_message="还没有生成可核对的修改。请补充具体情况，或手动填写。",
                    )
                if change_requested and not proposals:
                    return finish(
                        "success",
                        text=message.content.strip(),
                        business_outcome="needs_information",
                    )
                if declared_missing and not proposals:
                    return finish(
                        "success",
                        text="；".join(declared_missing[:3]),
                        business_outcome="needs_information",
                    )
                if weather_attempt_failed and not proposals and not previews:
                    return result_from_progress() or finish(
                        "error", error_message="天气查询未完成，请检查后重试。"
                    )
                return finish("success", text=self._safe_explanation(message.content.strip()))

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            calls_to_process = list(message.tool_calls)
            generated_call_ids: set[str] = set()
            for call_index, call in enumerate(calls_to_process):
                if cancellation is not None:
                    cancellation.check()
                if self._monotonic() >= deadline:
                    return finish(
                        "timeout",
                        text=last_tool_summary,
                        error_message="总时间预算已用尽，已保留有效提案。",
                    )
                if tool_count >= MAX_TOOL_EXECUTIONS:
                    traces.append(
                        AgentToolTraceV3(
                            name=call.function.name,
                            status="limit_reached",
                            elapsed_ms=0,
                            result_summary="本轮工具执行次数已达上限。",
                        )
                    )
                    return finish(
                        "error",
                        text=last_tool_summary,
                        error_message="本轮工具执行次数已达上限；已有结果已保留，请检查后重试。",
                    )
                if call.function.name not in allowed_capabilities(workflow_actions):
                    tool_result, proposal, preview, trace_status = (
                        {"error": "capability_not_allowed", "message": "当前流程不允许此工具"},
                        None,
                        None,
                        "invalid_arguments",
                    )
                    tool_count += 1
                    tool_names.append(call.function.name)
                    traces.append(
                        AgentToolTraceV3(
                            name=call.function.name,
                            status=trace_status,
                            elapsed_ms=0,
                            result_summary="当前流程不允许此工具",
                            result=tool_result,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(tool_result, ensure_ascii=False),
                        }
                    )
                    continue
                if weather_attempt_failed and call.function.name in {
                    "query_weather",
                    "query_current_weather",
                }:
                    weather_failure_reviewed = True
                    tool_count += 1
                    tool_names.append(call.function.name)
                    tool_result = {
                        "error": "duplicate_weather_attempt",
                        "message": "本轮天气查询已失败；请先处理原失败原因，再重新查询。",
                    }
                    traces.append(
                        AgentToolTraceV3(
                            name=call.function.name,
                            status="invalid_arguments",
                            elapsed_ms=0,
                            result_summary=tool_result["message"],
                            result=tool_result,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(tool_result, ensure_ascii=False),
                        }
                    )
                    continue
                else:
                    tool_count += 1
                    tool_names.append(call.function.name)
                    tool_started = self._monotonic()
                    report("tool", "started", call.function.name)
                    effective_arguments = call.function.arguments
                    grounding_error: str | None = None
                    if (
                        call.function.name == "propose_feedback"
                        and call.id not in generated_call_ids
                    ):
                        try:
                            raw_feedback = json.loads(effective_arguments)
                            accepted_events: list[dict[str, object]] = []
                            for raw_event in raw_feedback.get("events", []):
                                if not isinstance(raw_event, dict):
                                    continue
                                if (
                                    completion_only_requested
                                    and (raw_event.get("feedback") or {}).get("kind")
                                    == "observation"
                                ):
                                    continue
                                with_quote = {
                                    "source_quote": user_message,
                                    **raw_event,
                                }
                                accepted, problem = self._ground_declared_event(
                                    with_quote,
                                    field_id=field_id,
                                    user_message=user_message,
                                    received_at=started_at,
                                )
                                if accepted:
                                    accepted_events.append(accepted)
                                    runtime_missing.pop(event_key(raw_event), None)
                                elif problem:
                                    runtime_missing[event_key(raw_event)] = problem
                            accepted_events = self._merge_feedback_events(
                                deferred_feedback_events, accepted_events
                            )
                            if workflow_actions and "observation" in workflow_actions:
                                accepted_events = self._merge_feedback_events(
                                    accepted_events,
                                    self._explicit_maintenance_events(
                                        field_id=field_id,
                                        user_message=user_message,
                                        received_at=started_at,
                                        required_fields=set(required_observations),
                                        accepted_events=accepted_events,
                                    ),
                                )
                            missing_observations = set(required_observations) - {
                                str((event.get("feedback") or {}).get("field"))
                                for event in accepted_events
                                if (event.get("feedback") or {}).get("kind") == "observation"
                            }
                            if missing_observations and request_count == 1:
                                deferred_feedback_events = accepted_events
                                observation_retry_needed = True
                                grounding_error = "仍有明确的现场事项未形成待确认事实"
                                accepted_events = []
                            if accepted_events:
                                deferred_feedback_events = []
                                observation_retry_needed = False
                                effective_arguments = json.dumps(
                                    {**raw_feedback, "events": accepted_events},
                                    ensure_ascii=False,
                                )
                            elif grounding_error is None:
                                grounding_error = (
                                    list(runtime_missing.values())[-1]
                                    if runtime_missing
                                    else ("请核对反馈是否有用户原话支持")
                                )
                        except (TypeError, ValueError, AttributeError):
                            pass
                    if (
                        call.function.name == "propose_field_update"
                        and call.id not in generated_call_ids
                    ):
                        try:
                            raw_update = json.loads(effective_arguments)
                            changes = raw_update.get("changes", {})
                            accepted_changes = {}
                            for key, value in changes.items():
                                normalized = self._normalize_field_change(key, value)
                                if self._ground_field_change(key, normalized, user_message):
                                    accepted_changes[key] = normalized
                            for key in changes.keys() - accepted_changes.keys():
                                runtime_missing[f"field:{key}"] = (
                                    f"请确认地块档案字段 {key} 的具体改动"
                                )
                            for key in accepted_changes:
                                runtime_missing.pop(f"field:{key}", None)
                            if accepted_changes:
                                effective_arguments = json.dumps(
                                    {**raw_update, "changes": accepted_changes},
                                    ensure_ascii=False,
                                )
                            else:
                                grounding_error = "地块档案修改与用户原话不符"
                        except (TypeError, ValueError, AttributeError):
                            pass
                    try:
                        normalized_arguments = json.dumps(
                            json.loads(effective_arguments),
                            sort_keys=True,
                            ensure_ascii=False,
                        )
                    except (TypeError, ValueError):
                        normalized_arguments = effective_arguments
                    with self.workbench.store.connection_budget(
                        max(0.001, deadline - self._monotonic())
                    ):
                        state_for_cache = self.workbench.get_field(field_id)
                    cache_key = (
                        f"{call.function.name}:{normalized_arguments}:"
                        f"{state_for_cache.draft_version}:{state_for_cache.fact_version}"
                    )
                    if (
                        CAPABILITIES.get(call.function.name)
                        and CAPABILITIES[call.function.name].cacheable
                        and (cache_key in read_cache)
                    ):
                        tool_result, proposal, preview, trace_status = (
                            read_cache[cache_key],
                            None,
                            None,
                            "success",
                        )
                    else:
                        if grounding_error:
                            tool_result, proposal, preview, trace_status = (
                                {"error": "ungrounded_feedback", "message": grounding_error},
                                None,
                                None,
                                "invalid_arguments",
                            )
                        elif call.function.name == "propose_feedback" and self._vague_resource_time(
                            user_message, effective_arguments
                        ):
                            tool_result, proposal, preview, trace_status = (
                                {
                                    "error": "missing_time",
                                    "message": "请给出人员可用时段的具体开始和结束时间",
                                },
                                None,
                                None,
                                "invalid_arguments",
                            )
                        else:
                            tool_budget = max(0.001, deadline - self._monotonic())
                            with self.workbench.store.connection_budget(tool_budget):
                                tool_result, proposal, preview, trace_status = self._execute_tool(
                                    call.function.name,
                                    effective_arguments,
                                    field_id=field_id,
                                    run_id=run_id,
                                    user_message=user_message,
                                    timeout_seconds=tool_budget,
                                )
                            if cancellation is not None:
                                cancellation.check()
                        if (
                            CAPABILITIES.get(call.function.name)
                            and CAPABILITIES[call.function.name].cacheable
                            and trace_status == "success"
                        ):
                            read_cache[cache_key] = tool_result
                    if call.function.name == "declare_request" and trace_status == "success":
                        workflow_actions = set(tool_result["actions"])
                        if completion_only_requested:
                            workflow_actions.discard("observation")
                        declared_missing = list(tool_result["missing"])
                        declaration = AgentDeclareRequestArgs.model_validate(tool_result)
                        if read_only_preview:
                            workflow_actions = {"preview"} | (workflow_actions & {"query"})
                            declared_missing = []
                        accepted_events: list[dict[str, object]] = []
                        accepted_signatures: set[str] = set()
                        for raw_event in [] if read_only_preview else declaration.events:
                            if raw_event.feedback is None:
                                continue
                            if completion_only_requested and (
                                raw_event.feedback.kind == "observation"
                            ):
                                continue
                            accepted, problem = self._ground_declared_event(
                                raw_event.model_dump(mode="json", exclude_unset=True),
                                field_id=field_id,
                                user_message=user_message,
                                received_at=started_at,
                            )
                            if accepted:
                                signature = json.dumps(
                                    {
                                        "feedback": accepted.get("feedback"),
                                        "correction_of": accepted.get("correction_of"),
                                        "retracts": accepted.get("retracts", False),
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    default=str,
                                )
                                if signature not in accepted_signatures:
                                    accepted_events.append(accepted)
                                    accepted_signatures.add(signature)
                                runtime_missing.pop(event_key(raw_event.model_dump()), None)
                            elif problem:
                                runtime_missing[event_key(raw_event.model_dump())] = problem
                        accepted_events = self._merge_feedback_events(
                            deferred_feedback_events, accepted_events
                        )
                        if "observation" in workflow_actions:
                            accepted_events = self._merge_feedback_events(
                                accepted_events,
                                self._explicit_maintenance_events(
                                    field_id=field_id,
                                    user_message=user_message,
                                    received_at=started_at,
                                    required_fields=set(required_observations),
                                    accepted_events=accepted_events,
                                ),
                            )
                        missing_observations = (
                            set(required_observations)
                            - {
                                str((event.get("feedback") or {}).get("field"))
                                for event in accepted_events
                                if (event.get("feedback") or {}).get("kind") == "observation"
                            }
                            if change_requested or "observation" in workflow_actions
                            else set()
                        )
                        if missing_observations and request_count == 1:
                            deferred_feedback_events = accepted_events
                            observation_retry_needed = True
                            accepted_events = []
                        elif accepted_events:
                            deferred_feedback_events = []
                            observation_retry_needed = False
                        generated: list[tuple[str, dict[str, object]]] = []
                        if accepted_events:
                            generated.append(
                                (
                                    "propose_feedback",
                                    {"field_id": field_id, "events": accepted_events},
                                )
                            )
                        grounded_changes: dict[str, object] = {}
                        for key, value in (
                            {} if read_only_preview else declaration.field_changes
                        ).items():
                            normalized = self._normalize_field_change(key, value)
                            if "observation" in workflow_actions and (
                                key in required_observations
                                or (
                                    key == "maintenance_purpose"
                                    and saved_profile.maintenance_purpose == value
                                )
                            ):
                                continue
                            quote = declaration.field_change_quotes.get(key)
                            if (
                                quote
                                and quote in user_message
                                and self._ground_field_change(key, normalized, quote)
                            ):
                                grounded_changes[key] = normalized
                                runtime_missing.pop(f"field:{key}", None)
                            else:
                                runtime_missing[f"field:{key}"] = (
                                    f"请确认地块档案字段 {key} 的具体改动"
                                )
                        if grounded_changes:
                            generated.append(
                                (
                                    "propose_field_update",
                                    {"field_id": field_id, "changes": grounded_changes},
                                )
                            )
                        if not read_only_preview and "task_series" in workflow_actions:
                            quote = declaration.task_series_quote
                            if not declaration.task_series:
                                runtime_missing["task_series"] = (
                                    "请补充待办名称和日期；耗时未知时可以保留待确认"
                                )
                            elif not quote or quote not in user_message:
                                runtime_missing["task_series"] = (
                                    "请核对任务提案是否对应你刚才提出的内容"
                                )
                            else:
                                generated.append(
                                    (
                                        "propose_task_series",
                                        {
                                            "field_id": field_id,
                                            "source_quote": quote,
                                            **declaration.task_series,
                                        },
                                    )
                                )
                                runtime_missing.pop("task_series", None)
                        pending_changes = workflow_actions & {
                            "field_update",
                            "task_series",
                            "location",
                            "completed",
                            "observation",
                            "workability",
                            "resource",
                        }
                        if "preview" in workflow_actions and not generated and not pending_changes:
                            generated.append(("preview_plan", {"field_id": field_id}))
                        generated_calls: list[ModelToolCall] = []
                        for index, (tool_name, tool_args) in enumerate(generated):
                            generated_id = f"controller-{call.id}-{index}"
                            generated_call_ids.add(generated_id)
                            generated_calls.append(
                                ModelToolCall(
                                    id=generated_id,
                                    function=ModelFunction(
                                        name=tool_name,
                                        arguments=json.dumps(tool_args, ensure_ascii=False),
                                    ),
                                )
                            )
                        calls_to_process[call_index + 1 : call_index + 1] = generated_calls
                    if call.function.name in {"query_weather", "query_current_weather"}:
                        read_cache.clear()
                        if trace_status != "success":
                            weather_attempt_failed = True
                            weather_error_code = str(tool_result.get("error", "weather_failed"))
                            weather_error_message = str(tool_result.get("message", "天气查询失败"))
                            runtime_missing["weather"] = str(
                                tool_result.get("message", "请核对天气查询条件")
                            )
                        else:
                            runtime_missing.pop("weather", None)
                    elapsed_ms = max(0, int((self._monotonic() - tool_started) * 1000))
                    if proposal:
                        proposals.append(proposal)
                    if preview:
                        previews.append(preview)
                    summary = self._tool_summary(tool_result)
                    last_tool_summary = summary
                    traces.append(
                        AgentToolTraceV3(
                            name=call.function.name,
                            status=trace_status,
                            elapsed_ms=elapsed_ms,
                            result_summary=summary,
                            arguments=self._trace_arguments(call.function.arguments),
                            result=tool_result,
                        )
                    )
                    steps.append(
                        AgentWorkflowStepV3(
                            stage="proposal" if proposal else "preview" if preview else "tool",
                            status=trace_status,
                            name=call.function.name,
                            elapsed_ms=elapsed_ms,
                        )
                    )
                    report("tool", trace_status, call.function.name)
                if call.id not in generated_call_ids:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                        }
                    )
                if self._monotonic() >= deadline:
                    return finish(
                        "timeout",
                        text=last_tool_summary,
                        error_message="总时间预算已用尽，已保留有效提案。",
                    )
                if call.id in generated_call_ids and (proposal or preview):
                    if not any(
                        following.id in generated_call_ids
                        for following in calls_to_process[call_index + 1 :]
                    ):
                        break

            if observation_retry_needed and request_count < MAX_MODEL_REQUESTS:
                observation_retry_needed = False
                missing_labels = [
                    label
                    for field, label in required_observations.items()
                    if field
                    not in {
                        str((event.get("feedback") or {}).get("field"))
                        for event in deferred_feedback_events
                        if (event.get("feedback") or {}).get("kind") == "observation"
                    }
                ]
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "本轮用户还明确提出："
                            + "、".join(missing_labels)
                            + "。请逐项核对原话，并把有依据的事实合并为一份完整提案；"
                            "无法确认的项请明确说明缺项，不得猜测。"
                        ),
                    }
                )
                continue
            if workflow_actions and not proposals and not previews and not recovery_attempted:
                extractable = (
                    bool({"field_update", "location"} & workflow_actions)
                    or (
                        "workability" in workflow_actions
                        and bool(re.search(r"现场|田里|进田|下地|可作业", user_message))
                    )
                    or (
                        "resource" in workflow_actions
                        and len(self._clock_minutes(user_message)) >= 2
                    )
                    or (
                        "observation" in workflow_actions
                        and bool(self._requested_observations(user_message))
                    )
                    or (
                        "completed" in workflow_actions
                        and bool(re.search(r"\d{1,2}:\d{2}.*(?:完成|做完)", user_message))
                    )
                )
                if extractable and request_count < MAX_MODEL_REQUESTS and not declared_missing:
                    recovery_attempted = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "请根据已保存状态完成本轮声明的动作。"
                                "事实修改调用 propose_feedback，"
                                "档案修改调用 propose_field_update，位置先搜索候选。"
                                "每条事实附原话 source_quote；不要要求用户重复已给信息。"
                            ),
                        }
                    )
                    continue
            if (
                weather_attempt_failed
                and not weather_failure_reviewed
                and not proposals
                and not previews
                and request_count < MAX_MODEL_REQUESTS
            ):
                weather_failure_reviewed = True
                continue
            intermediate_result = result_from_progress()
            if intermediate_result and (proposals or previews or missing_items()):
                return intermediate_result
            if proposals or (previews and not change_requested):
                if (
                    workflow_actions
                    and not self._actions_satisfied(workflow_actions, proposals, previews)
                    and request_count < MAX_MODEL_REQUESTS
                ):
                    messages.append(
                        {
                            "role": "user",
                            "content": "仍有用户事项未形成待确认提案。请补齐，或明确写出具体缺项。",
                        }
                    )
                    continue
                return finish(
                    "success",
                    text="已生成待确认内容，请逐项核对后再保存或采用。",
                    business_outcome="partial"
                    if workflow_actions
                    and not self._actions_satisfied(workflow_actions, proposals, previews)
                    else "awaiting_confirmation"
                    if proposals
                    else "preview_ready",
                )

        if (proposals or previews) and traces and traces[-1].status == "success":
            return finish(
                "success",
                text="已生成待确认内容，请逐项核对后再保存或采用。",
                business_outcome="awaiting_confirmation" if proposals else "preview_ready",
            )
        return finish(
            "error",
            text=last_tool_summary,
            error_message="本轮最多请求模型三次；工具结果已保留，请检查后重试。",
        )

    @staticmethod
    def _merge_feedback_events(
        previous: list[dict[str, object]], current: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Keep one grounded fact per identical event across a correction request."""
        merged: list[dict[str, object]] = []
        seen: set[str] = set()
        for event in [*previous, *current]:
            fact = FactEventInputV3.model_validate(
                {key: value for key, value in event.items() if key != "source_quote"}
            )
            feedback = fact.feedback
            details = feedback.model_dump(mode="json", exclude_none=True) if feedback else None
            if feedback and details is not None:
                details["at"] = feedback.at.astimezone(UTC).isoformat()
                if feedback.kind in {"completed", "observation"}:
                    details.pop("day", None)
            signature = json.dumps(
                {
                    "feedback": details,
                    "correction_of": fact.correction_of,
                    "retracts": fact.retracts,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if signature not in seen:
                merged.append(event)
                seen.add(signature)
        return merged

    @staticmethod
    def _actions_satisfied(
        actions: set[str],
        proposals: list[AgentProposalV3],
        previews: list[PlanPreviewV3],
    ) -> bool:
        covered = {"query"}
        if previews:
            covered.add("preview")
        for proposal in proposals:
            if proposal.kind == "field_update":
                covered.update({"field_update", "location"})
            elif proposal.kind == "task_series":
                covered.add("task_series")
            for event in proposal.events:
                if event.feedback is not None:
                    covered.add(event.feedback.kind)
        return actions <= covered

    def _field_context(self, field_id: str) -> dict[str, object]:
        """Give the first model call the IDs and saved values it may actually use."""
        state = self.workbench.get_field(field_id)
        current = self.workbench.get_current_plan(field_id)
        return {
            "field_id": field_id,
            "draft_version": state.draft_version,
            "fact_version": state.fact_version,
            "plan_id": current.id if current else None,
            "draft": state.draft.model_dump(mode="json"),
            "current_tasks": [
                {
                    "task_id": item.task_id,
                    "task_type": item.task_type,
                    "completed": item.completed,
                    "start_at": item.start_at.isoformat() if item.start_at else None,
                    "end_at": item.end_at.isoformat() if item.end_at else None,
                }
                for item in (current.items if current else [])
            ],
            "recent_facts": [
                {
                    "event_id": event.event_id,
                    "feedback": event.feedback.model_dump(mode="json") if event.feedback else None,
                }
                for event in self.workbench.list_events(field_id)[-8:]
            ],
        }

    @staticmethod
    def _requested_observations(message: str) -> dict[str, str]:
        names = {
            "standing_water": (r"积水", "积水"),
            "pest_signs": (r"虫情|害虫|虫害", "虫情"),
            "ditch_connectivity": (
                r"沟渠.*?(?:堵|不通|连通|通畅|没复查|未复查)|堵塞",
                "沟渠连通情况",
            ),
            "maintenance_purpose": (
                r"(?:仍需|需要|目的|为了).*?清理既有沟渠|清理既有沟渠.*?(?:目的|用于)",
                "沟渠维护目的",
            ),
            "maintenance_need_confirmed": (r"(?:仍需|需要).*?(?:清理|维护)", "维护需求"),
            "maintenance_problem_confirmed": (r"堵塞|沟渠受阻|沟渠不通", "沟渠现场问题"),
        }
        return {
            field: label for field, (pattern, label) in names.items() if re.search(pattern, message)
        }

    def _explicit_maintenance_events(
        self,
        *,
        field_id: str,
        user_message: str,
        received_at: datetime,
        required_fields: set[str],
        accepted_events: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Offer quoted confirmations after the model identified an observation."""
        if not any(
            (event.get("feedback") or {}).get("kind") == "observation" for event in accepted_events
        ):
            return []
        covered = {
            str((event.get("feedback") or {}).get("field"))
            for event in accepted_events
            if (event.get("feedback") or {}).get("kind") == "observation"
        }
        generated: list[dict[str, object]] = []
        for field in ("maintenance_problem_confirmed", "maintenance_need_confirmed"):
            if field not in required_fields or field in covered:
                continue
            for clause in re.split(r"[，,。；;\n]", user_message):
                clause = clause.strip()
                if (
                    field not in self._requested_observations(clause)
                    or re.search(r"疑似|可能|大概|或许|如果|假如|未确认", clause)
                    or not self._ground_observation_value(field, True, clause)
                ):
                    continue
                observed_at = self._ground_observation_at(clause, received_at)
                if observed_at is None:
                    continue
                event, _problem = self._ground_declared_event(
                    {
                        "source_quote": clause,
                        "feedback": {
                            "kind": "observation",
                            "at": observed_at.isoformat(),
                            "plot_id": field_id,
                            "field": field,
                            "value": True,
                        },
                    },
                    field_id=field_id,
                    user_message=user_message,
                    received_at=received_at,
                )
                if event is not None:
                    generated.append(event)
                    break
        return generated

    @staticmethod
    def _resource_days_in_message(
        message: str, *, received_at: datetime, plan_start: date | None
    ) -> set[date]:
        days: set[date] = set()
        for clause in re.split(r"[,，。；;\n]", message):
            if re.search(r"保留|不变|照旧", clause) and not re.search(
                r"改为|改成|变为|只有|只剩|仅|调整", clause
            ):
                continue
            explicit_resource = re.search(r"(?<!原)有空|可用|人员.*时间|工作时段|空闲", clause)
            changed_resource = re.search(r"改为|改成|变为|只有|只剩|仅|调整", clause) and re.search(
                r"资源|空档|分钟|\d{1,2}:\d{2}", clause
            )
            if not (explicit_resource or changed_resource):
                continue
            days.update(AgentServiceV3._mentioned_days(clause, received_at, plan_start))
        return days

    @staticmethod
    def _explicit_dates(source: str) -> set[date]:
        """Recognize dates as written, without inventing a day for malformed input."""
        found: set[date] = set()
        for year, month, day in re.findall(
            r"(?<!\d)(20\d{2})[-年](\d{1,2})[-月](\d{1,2})(?:日)?(?!\d)", source
        ):
            try:
                found.add(date(int(year), int(month), int(day)))
            except ValueError:
                continue
        return found

    @staticmethod
    def _clock_minutes(source: str) -> list[int]:
        """Only exact user clock expressions can ground resource interval endpoints."""
        values: list[int] = []
        period: str | None = None
        numerals = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        pattern = (
            r"(?:(上午|下午|晚上|中午|凌晨)\s*)?"
            r"(\d{1,2}|[一二两三四五六七八九十])"
            r"(?::(\d{2})|点(?:(\d{1,2})分?)?)"
        )
        for match in re.finditer(pattern, source):
            period = match[1] or period
            hour = int(match[2]) if match[2].isdigit() else numerals[match[2]]
            minute = int(match[3] or match[4] or 0)
            if period in {"下午", "晚上", "中午"} and hour < 12:
                hour += 12
            if period in {"上午", "凌晨"} and hour == 12:
                hour = 0
            if hour < 24 and minute < 60:
                values.append(hour * 60 + minute)
        return values

    @staticmethod
    def _normalize_field_change(field: str, value: object) -> object:
        if field == "field_type":
            return (
                {
                    "低洼稻茬田": "lowland_rice_stubble",
                    "低洼/稻茬田": "lowland_rice_stubble",
                    "丘陵旱地": "dry_hilly",
                }.get(value, value)
                if isinstance(value, str)
                else value
            )
        return value

    @staticmethod
    def _ground_field_change(field: str, value: object, quote: str) -> bool:
        """Accept only field values supported by a matching fragment of the user's message."""
        if value is None:
            return bool(re.search(r"未知|不确定|未确认|还没确认", quote))
        if isinstance(value, bool):
            patterns = {
                "inspection_requested": (
                    r"(?<!不)(?:想|要|需要|请|安排).*?(?:巡田|巡查|检查)",
                    r"(?:不想|不要|不用|无需|不需要|取消).*?(?:巡田|巡查|检查)",
                ),
                "maintenance_need_confirmed": (
                    r"(?<!不)(?:仍需|需要|要).*?(?:清理|维护|疏通)",
                    r"(?:不要|不用|无需|不需要|取消).*?(?:清理|维护|疏通)",
                ),
                "maintenance_problem_confirmed": (
                    r"(?:堵塞|沟渠受阻|沟渠不通)",
                    r"(?:未堵|没有堵|通畅)",
                ),
                "province_confirmed": (r"(?:已确认|确定).*?(?:省份|省|地区)", r"省份未确认"),
                "crop_confirmed": (r"(?:已确认|确定).*?(?:作物|油菜)", r"作物未确认"),
                "stage_confirmed": (r"(?:已确认|确定).*?(?:生育期|苗期)", r"生育期未确认"),
                "winter_before_context_confirmed": (r"(?:已确认|确定).*?冬前", r"冬前未确认"),
            }
            pair = patterns.get(field)
            if pair is None:
                return False
            positive, negative = pair
            negative_found = bool(re.search(negative, quote))
            positive_quote = (
                re.sub(negative, "", quote)
                if field in {"inspection_requested", "maintenance_need_confirmed"}
                else quote
            )
            positive_found = bool(re.search(positive, positive_quote))
            confirmation_labels = {
                "province_confirmed": r"省份|地区|湖北(?:省)?",
                "crop_confirmed": r"作物|油菜",
                "stage_confirmed": r"生育期|苗期",
            }
            if field in confirmation_labels:
                positive_found |= bool(
                    re.search(
                        rf"(?:{confirmation_labels[field]}).*?已(?:现场)?确认",
                        quote,
                    )
                )
            if positive_found and negative_found:
                return False
            return positive_found if value else negative_found
        if field == "maintenance_purpose" and value == "clear_existing_ditch":
            return bool(re.search(r"清理既有沟渠|既有沟渠清理", quote)) and not bool(
                re.search(r"(?:不想|不要|不用|无需|不需要|取消).*?清理", quote)
            )
        if field == "crop" and value == "rapeseed":
            return "油菜" in quote
        if field == "field_type":
            labels = {
                "lowland_rice_stubble": ("低洼稻茬田", "低洼/稻茬田"),
                "dry_hilly": ("丘陵旱地",),
            }.get(value, ())
            return str(value) in quote or any(label in quote for label in labels)
        if isinstance(value, (str, int, float)):
            return str(value).casefold() in quote.casefold()
        return False

    @staticmethod
    def _mentioned_days(quote: str, received_at: datetime, plan_start: date | None) -> set[date]:
        local_day = received_at.astimezone(LOCAL_ZONE).date()
        days: set[date] = set()
        if "今天" in quote:
            days.add(local_day)
        if "明天" in quote:
            days.add(local_day + timedelta(days=1))
        if "后天" in quote:
            days.add(local_day + timedelta(days=2))
        if plan_start:
            ordinals = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}

            def ordinal(raw: str) -> int:
                return int(raw) if raw.isdigit() else ordinals[raw]

            if "首日" in quote:
                days.add(plan_start)
            for first, last in re.findall(
                r"第([一二三四五六七1-7])天\s*(?:到|至|—|–|-)\s*第?([一二三四五六七1-7])天",
                quote,
            ):
                start_index, end_index = ordinal(first), ordinal(last)
                if start_index <= end_index:
                    days.update(
                        plan_start + timedelta(days=index - 1)
                        for index in range(start_index, end_index + 1)
                    )
            for raw in re.findall(r"第([一二三四五六七1-7])天", quote):
                days.add(plan_start + timedelta(days=ordinal(raw) - 1))
            for raw in re.findall(r"(?:前面|前)([一二三四五六七1-7])天", quote):
                days.update(plan_start + timedelta(days=index) for index in range(ordinal(raw)))
        days.update(AgentServiceV3._explicit_dates(quote))
        return days

    @staticmethod
    def _ground_observation_value(field: str, value: object, quote: str) -> bool:
        if value is not None and re.search(r"疑似|可能|大概|或许|如果|假如|未确认", quote):
            return False
        if value is None:
            names = {
                "standing_water": r"积水",
                "pest_signs": r"虫情|害虫|虫害",
                "ditch_connectivity": r"沟渠|堵塞|连通",
                "maintenance_purpose": r"清理|维护目的",
                "maintenance_need_confirmed": r"清理|维护需求",
                "maintenance_problem_confirmed": r"沟渠|堵塞|维护问题",
            }
            target = names.get(field)
            if field == "ditch_connectivity" and re.search(
                r"沟渠.*?清理.*?完成[，,；;。]?\s*效果.*?(?:没|未)复查", quote
            ):
                return True
            return bool(
                target
                and any(
                    re.search(target, clause)
                    and re.search(
                        r"未知|不清楚|未确认|待确认|未观察|没看|未看|没复查|未复查",
                        clause,
                    )
                    for clause in re.split(r"[,，。；;\n]", quote)
                )
            )
        if field in {"maintenance_need_confirmed", "maintenance_problem_confirmed"}:
            return AgentServiceV3._ground_field_change(field, value, quote)
        if field == "maintenance_purpose":
            return AgentServiceV3._ground_field_change(field, value, quote)
        if field == "ditch_connectivity":
            impaired = bool(re.search(r"堵塞|不通|受阻", quote))
            clear = bool(re.search(r"通畅|已疏通|连通良好", quote))
            return not (impaired and clear) and (
                (value == "impaired" and impaired) or (value == "clear" and clear)
            )
        if field in {"standing_water", "pest_signs"}:
            if not isinstance(value, bool):
                return False
            if field == "standing_water":
                negative = bool(
                    re.search(r"无积水|没有积水|未见积水|未发现积水|没发现积水|不积水", quote)
                )
                positive = bool(
                    re.search(
                        r"(?:(?<![未没])发现|(?<!没)有|出现|存在|仍有|看到).*?积水|积水有",
                        quote,
                    )
                )
            else:
                negative = bool(
                    re.search(
                        r"无虫情|没有虫|未见(?:虫情|害虫)|未发现(?:虫情|害虫|虫害)|无害虫", quote
                    )
                )
                positive = bool(
                    re.search(
                        r"(?:(?<![未没])发现|(?<!没)有|出现|看到).*?(?:虫情|害虫|虫害)", quote
                    )
                )
            if positive and negative:
                return False
            return positive if value else negative
        return False

    @staticmethod
    def _ground_observation_at(quote: str, received_at: datetime) -> datetime | None:
        """Use message receipt unless the observation clause explicitly gives one exact clock."""
        if re.search(r"(?:\d{1,2}|[一二两三四五六七八九十])\s*点|\d{1,2}月\d{1,2}日", quote):
            return None
        clocks = list(re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", quote))
        if not clocks:
            if re.search(r"昨天|前天|明天|后天|上午|下午|晚上|凌晨|20\d{2}-\d{2}-\d{2}", quote):
                return None
            return received_at
        if len(clocks) != 1:
            return None
        hour, minute = int(clocks[0][1]), int(clocks[0][2])
        if hour > 23 or minute > 59:
            return None
        local_day = received_at.astimezone(LOCAL_ZONE).date()
        explicit_days = re.findall(r"20\d{2}-\d{2}-\d{2}", quote)
        if len(explicit_days) > 1:
            return None
        if explicit_days:
            local_day = date.fromisoformat(explicit_days[0])
        elif "昨天" in quote:
            local_day -= timedelta(days=1)
        elif "明天" in quote or "后天" in quote:
            return None
        observed_at = datetime(
            local_day.year, local_day.month, local_day.day, hour, minute, tzinfo=LOCAL_ZONE
        )
        return observed_at if observed_at <= received_at else None

    def _ground_declared_event(
        self,
        raw: dict[str, object],
        *,
        field_id: str,
        user_message: str,
        received_at: datetime,
    ) -> tuple[dict[str, object] | None, str | None]:
        quote = raw.get("source_quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in user_message:
            quote = self._current_confirmation_quote(raw, user_message)
            if quote is None:
                return None, "请指出这条修改对应的用户原话"
        event = {key: value for key, value in raw.items() if key != "source_quote"}
        try:
            args = ProposeFeedbackArgs.model_validate({"field_id": field_id, "events": [event]})
            feedback = args.validated_events()[0].feedback
            if feedback is None:
                raise ValueError("缺少事实内容")
            if feedback.kind == "completed":
                current = self.workbench.get_current_plan(field_id)
                task = (
                    next((item for item in current.items if item.task_id == feedback.task_id), None)
                    if current
                    else None
                )
                if task is None:
                    return None, "请核对要记录完成的任务"
                aliases = (
                    r"巡田|巡查|检查|巡视"
                    if task.task_type == "inspection"
                    else r"清沟|沟渠|维护|疏通"
                )
                if feedback.task_id not in quote and not re.search(aliases, quote):
                    return None, "请说明实际完成的是哪项任务"
                grounded = ground_completed_time(feedback.at, received_at, quote)
                if grounded.reason == "future_actual_completion_time":
                    return None, "所报的完成时间尚未到来；请在任务实际完成后再记录"
                if not grounded.accepted:
                    other_aliases = (
                        r"清沟|沟渠|维护|疏通"
                        if task.task_type == "inspection"
                        else r"巡田|巡查|检查|巡视"
                    )
                    related_clauses = [
                        clause
                        for clause in re.split(r"[，,。；;！？!?\n]", user_message)
                        if (feedback.task_id in clause or re.search(aliases, clause))
                        and (feedback.task_id in clause or not re.search(other_aliases, clause))
                    ]
                    grounded = ground_completed_time(
                        feedback.at, received_at, "；".join(related_clauses)
                    )
                if not grounded.accepted:
                    if grounded.reason == "future_actual_completion_time":
                        return None, "所报的完成时间尚未到来；请在任务实际完成后再记录"
                    return None, "请提供该任务实际完成的准确日期和时间"
            elif feedback.kind == "resource":
                if not re.search(r"有空|可用|时段|人员|工时|时间|资源|空档|分钟", quote):
                    return None, "请说明人员可用时间"
                clocks = self._clock_minutes(quote)
                expected = [
                    item
                    for window in feedback.intervals or []
                    for item in (
                        window.start.hour * 60 + window.start.minute,
                        window.end.hour * 60 + window.end.minute,
                    )
                ]
                explicit_zero = bool(re.search(r"(?<!\d)(?:0|零)\s*分钟", quote))
                zero_day = not expected and feedback.resource_minutes == 0 and explicit_zero
                # Users often repeat the same interval when describing the change.
                # Every mentioned clock must still belong to the proposed sequence.
                repeated_sequence = (
                    bool(clocks)
                    and bool(expected)
                    and len(clocks) % len(expected) == 0
                    and all(
                        clocks[index : index + len(expected)] == expected
                        for index in range(0, len(clocks), len(expected))
                    )
                )
                if not zero_day and not repeated_sequence:
                    return None, "请给出人员可用时段的具体开始和结束时间"
                start_day = self.workbench.get_field(field_id).draft.start_date
                mentioned_days = self._mentioned_days(quote, received_at, start_day)
                if feedback.day not in mentioned_days:
                    return None, "请确认人员时间对应的具体日期"
                interval_minutes = sum(window.minutes for window in feedback.intervals or [])
                capacity = feedback.resource_minutes
                if (
                    capacity is not None
                    and not zero_day
                    and (
                        capacity <= 0
                        or capacity > interval_minutes
                        or (
                            capacity != interval_minutes
                            and not re.search(rf"(?<!\d){capacity}\s*分钟", quote)
                        )
                    )
                ):
                    return None, "请核对人员可用工时与时段是否一致"
            elif feedback.kind == "observation":
                names = self._requested_observations(quote)
                unknown_ditch_effect = bool(
                    feedback.field == "ditch_connectivity"
                    and feedback.value is None
                    and re.search(r"效果.*?(?:没|未)复查", quote)
                    and re.search(r"沟渠|清沟|沟口", user_message)
                )
                if feedback.field not in names and not unknown_ditch_effect:
                    return None, "请核对现场观察项目与用户原话"
                if not unknown_ditch_effect and not self._ground_observation_value(
                    feedback.field, feedback.value, quote
                ):
                    return None, "请核对现场观察结果与用户原话"
                observed_at = self._ground_observation_at(quote, received_at)
                if observed_at is None or (
                    observed_at != received_at and feedback.at != observed_at
                ):
                    return None, "请核对现场观察的实际日期和时间"
                if feedback.day is not None and feedback.day != observed_at.date():
                    return None, "请确认现场观察对应的日期"
            elif feedback.kind == "workability":
                if re.search(r"可能|预计|预报|也许|大概", quote) or not re.search(
                    r"现场|田里|地里|进田|下地|可作业|不能作业|不可作业", quote
                ):
                    return None, "请确认当天的现场和天气作业条件"
                if feedback.day not in self._mentioned_days(
                    quote, received_at, self.workbench.get_field(field_id).draft.start_date
                ):
                    return None, "请确认现场作业条件对应的日期"
                negative = bool(
                    re.search(r"不能|不可|无法|下不了|进不了|不适合|可作业改为否", quote)
                )
                positive_quote = re.sub(r"(?:现场|田里|地里)?可作业改为否", "", quote)
                positive = bool(
                    re.search(
                        r"(?<!不)(?:可以|可|能够|能|适合).*?(?:作业|进田|下地)",
                        positive_quote,
                    )
                )
                raw_feedback = event.get("feedback", event)
                if (
                    isinstance(raw_feedback, dict)
                    and "site_workable" in raw_feedback
                    and feedback.site_workable is None
                    and not re.search(r"未知|不清楚|未确认|待确认", quote)
                ):
                    return None, "请确认当天现场是否可以作业"
                if feedback.site_workable is not None and (
                    positive == negative or feedback.site_workable != positive
                ):
                    return None, "请核对当天能否进田作业"
                if feedback.weather_workable is not None:
                    weather_quote = re.search(r"天气条件[^，。；;]*", quote)
                    if not weather_quote:
                        return None, "请单独确认当天的天气作业条件"
                    weather_negative = bool(re.search(r"不能|不可|无法|不适合", weather_quote[0]))
                    weather_positive = bool(re.search(r"仍可|可以|可作业|适合", weather_quote[0]))
                    if (
                        weather_positive == weather_negative
                        or feedback.weather_workable != weather_positive
                    ):
                        return None, "请核对当天的天气作业条件"
            if feedback.kind != "completed":
                feedback_data = feedback.model_dump(mode="json", exclude_unset=True)
                feedback_data["at"] = (
                    observed_at.isoformat()
                    if feedback.kind == "observation"
                    else received_at.isoformat()
                )
                event["feedback"] = feedback_data
            return event, None
        except (ValidationError, ValueError, TypeError):
            return None, "请核对修改内容、日期和字段值"

    @staticmethod
    def _current_confirmation_quote(raw: dict[str, object], message: str) -> str | None:
        """Ground a repeated fact in this turn, never in an earlier model quote."""
        if not re.search(r"确认|核对无误|同意", message):
            return None
        feedback = raw.get("feedback")
        if not isinstance(feedback, dict):
            return None
        kind = feedback.get("kind")
        field = feedback.get("field")
        if kind == "completed":
            pattern = r"巡田|巡查|检查|沟渠|清理|维护|完成|做完"
        elif kind == "observation":
            pattern = {
                "standing_water": r"积水",
                "pest_signs": r"虫情|害虫|虫害",
                "ditch_connectivity": r"沟渠|沟口|堵塞",
                "maintenance_problem_confirmed": r"沟渠|沟口|堵塞",
                "maintenance_need_confirmed": r"仍需|需要.*清理|维护需求",
                "maintenance_purpose": r"既有沟渠|维护目的",
            }.get(field)
        elif kind == "resource":
            pattern = r"可用|有空|空档|分钟|时段"
        elif kind == "workability":
            pattern = r"进田|下地|作业条件|可作业"
        else:
            return None
        if not pattern:
            return None
        clauses = [
            clause.strip()
            for clause in re.split(r"[，,。；;、\n]", message)
            if re.search(pattern, clause)
        ]
        if kind == "completed":
            direct = [
                clause
                for clause in clauses
                if re.search(r"完成|做完", clause) and not re.search(r"完成时间", clause)
            ]
            clauses = direct or [clause for clause in clauses if re.search(r"完成时间", clause)]
        return clauses[0] if len(clauses) == 1 else None

    @staticmethod
    def _vague_resource_time(user_message: str, arguments_json: str) -> bool:
        if not re.search(r"上午|下午|晚上|(?<!原)有空|可用时间", user_message):
            return False
        if re.search(r"\d{1,2}:\d{2}|[\d一二三四五六七八九十两]{1,3}\s*点", user_message):
            return False
        try:
            arguments = json.loads(arguments_json)
            return any(
                (event.get("feedback") or event).get("kind") == "resource"
                for event in arguments.get("events", [])
                if isinstance(event, dict)
            )
        except (TypeError, ValueError, AttributeError):
            return False

    @staticmethod
    def _trace_arguments(raw: str) -> dict[str, object] | None:
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _system_message(field_id: str, *, received_at: datetime) -> str:
        return (
            "你是农时窗口的中文农事助手。当前地块 ID 是 "
            f"{field_id}。本轮接收时间为北京时间 {received_at:%Y-%m-%d %H:%M}。"
            "只能根据本地工具结果解释任务、依据、天气和排程。"
            "首轮必须调用 declare_request，列出本轮每个用户动作和具体缺项；"
            "声明与参数提取在同一轮，不额外请求分类。"
            "先读取或核对状态，再按需调用地点搜索、天气、规则、评估或计划预览工具。"
            "查询天气优先用 query_current_weather，不要猜测起止日期。"
            "用户要求记录完成、现场观察或修改可用时间时，必须调用 propose_feedback；"
            "可用时段属于 resource，不属于 workability。多事项应逐项提出同一批事实。"
            "事实提案未获用户确认前，不应根据提案预览调整结果。"
            "最多提出三条影响当前判断的问题。"
            "相对日期只按本轮接收时的北京时间换算，并在待确认修改里写出完整日期；"
            "不能猜测缺失的日期。"
            "涉及档案或现场事实的修改只能调用 propose_* 工具，必须向用户逐项说明并等待确认；"
            "新增自定义农事须先由 declare_request 提取任务描述，再提出 propose_task_series；"
            "只使用用户明确给出的名称、日期、工时和步骤；未知内容留空，不推导农艺处方；"
            "不能声称草稿已保存、事实已记录或计划已采用。不能编造任务、工时、天气、来源和农艺结论。"
            "未知保留为未知。天气预报不代表现场可作业。候选规则标记为开发验证、适用性待复核。"
            "面向用户先说结果，再给一个必要操作或具体问题。用简短自然的中文，"
            "少说‘结构化表单’‘本轮交互’‘事实提案’等内部术语；空值只说暂不确定，"
            "不要把未填写说成已检查。不要套话，也不要重复任务清单。"
            "任务、日期、时段、状态和来源都由本地工具结果展示。"
            "不要输出私有推理过程。"
        )

    @staticmethod
    def _provider_error(status_code: int) -> tuple[str, str]:
        if status_code == 401:
            return "auth_error", "模型认证失败；请检查本地密钥配置。"
        if status_code == 402:
            return "insufficient_balance", "模型账户余额不足或当前模型不可用。"
        if status_code == 429:
            return "rate_limited", "模型服务限流；请稍后重试。"
        if status_code in {408, 504}:
            return "timeout", "模型服务处理超时；可以稍后重试。"
        return "error", f"模型服务返回 HTTP {status_code}。"

    @staticmethod
    def _safe_explanation(text: str) -> str:
        if len(text) > 400:
            return "已完成本轮查询；请以计划结果和依据面板中的结构化内容为准。"
        unsupported_detail = re.search(
            r"(?:\b20\d{2}[-/年]|\b\d{1,2}:\d{2}\b|\bC\d{2}\b|\b[A-Z]-[IM]\b|"
            r"\d+(?:\.\d+)?\s*(?:mm|毫米|°?C|℃|km/h)|建议.*(?:施肥|喷药|排水|清沟|浇水|巡田|维护))",
            text,
            flags=re.IGNORECASE,
        )
        if unsupported_detail:
            return "已完成本轮查询；请以计划结果和依据面板中的结构化内容为准。"
        return text

    def _execute_tool(
        self,
        name: str,
        arguments_json: str,
        *,
        field_id: str,
        run_id: str,
        user_message: str = "",
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, object], AgentProposalV3 | None, PlanPreviewV3 | None, str]:
        started = self._monotonic()
        try:
            weather_kwargs = (
                {"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}
            )
            raw_arguments = json.loads(arguments_json)
            if not isinstance(raw_arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            proposal: AgentProposalV3 | None = None
            if name == "declare_request":
                args = AgentDeclareRequestArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                result = args.model_dump(mode="json", exclude_unset=True)
                result.setdefault("missing", [])
            elif name == "read_field":
                args = ReadFieldArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                result = self.workbench.get_field(field_id).model_dump(mode="json")
                current = self.workbench.get_current_plan(field_id)
                result["current_tasks"] = [
                    {
                        "task_id": item.task_id,
                        "task_type": item.task_type,
                        "schedule_status": item.schedule_status,
                        "completed": item.completed,
                        "start_at": item.start_at.isoformat() if item.start_at else None,
                        "end_at": item.end_at.isoformat() if item.end_at else None,
                    }
                    for item in (current.items if current else [])
                ]
                result["recent_facts"] = [
                    {
                        "event_id": event.event_id,
                        "feedback": event.feedback.model_dump(mode="json")
                        if event.feedback
                        else None,
                    }
                    for event in self.workbench.list_events(field_id)[-8:]
                ]
            elif name == "search_rules":
                args = SearchRulesArgs.model_validate(raw_arguments)
                query = self._evaluation_rule_query() if self._evaluation_rule_query else args.query
                result = {
                    "query": query.model_dump(mode="json"),
                    **self._retriever.search(query).model_dump(mode="json"),
                }
            elif name == "evaluate_field":
                args = EvaluateFieldArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                result = self.workbench.evaluate_field(field_id).model_dump(mode="json")
            elif name == "query_weather":
                args = QueryWeatherArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                state = self.workbench.get_field(field_id)
                if state.draft.location is None:
                    raise ValueError("请先搜索并确认天气查询地点")
                start = date.fromisoformat(args.start_date)
                end = date.fromisoformat(args.end_date)
                if state.draft.start_date is None or start != state.draft.start_date:
                    raise ValueError("天气开始日期必须与当前计划起始日一致")
                if end != start + timedelta(days=6):
                    raise ValueError("天气日期必须完整覆盖当前计划七天")
                snapshot = self.weather.fetch_forecast(
                    field_id,
                    state.draft.location,
                    start,
                    end,
                    **weather_kwargs,
                )
                result = snapshot.model_dump(mode="json")
            elif name == "query_current_weather":
                args = QueryCurrentWeatherArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                state = self.workbench.get_field(field_id)
                if state.draft.location is None:
                    raise ValueError("请先搜索并确认天气查询地点")
                if state.draft.start_date is None:
                    raise ValueError("请先保存计划起始日期")
                start = state.draft.start_date
                snapshot = self.weather.fetch_forecast(
                    field_id,
                    state.draft.location,
                    start,
                    start + timedelta(days=6),
                    **weather_kwargs,
                )
                result = snapshot.model_dump(mode="json")
            elif name == "search_locations":
                args = SearchLocationsArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                candidates = self.weather.search_locations(args.query, **weather_kwargs)
                result_items = []
                for candidate in candidates:
                    candidate_id = uuid4().hex
                    self._geocoding_candidates[candidate_id] = (field_id, candidate)
                    result_items.append(
                        {
                            "candidate_id": candidate_id,
                            "candidate": candidate.model_dump(mode="json"),
                        }
                    )
                result = {
                    "query": args.query,
                    "candidates": result_items,
                    "instruction": "请让用户按候选显示名称和查询精度明确选择。",
                }
            elif name == "propose_location":
                args = ProposeLocationArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                selected = self._geocoding_candidates.get(args.candidate_id)
                if selected is None or selected[0] != field_id:
                    raise ValueError("该地点候选不存在或不属于当前地块；请重新搜索")
                state = self.workbench.get_field(field_id)
                if state.draft.profile is None:
                    raise ValueError("先保存地块档案，再修改天气查询地点")
                candidate = selected[1]
                location = self.weather.select_location([candidate], candidate.id)
                updated = state.draft.model_copy(update={"location": location})
                proposal = AgentProposalV3(
                    kind="field_update",
                    field_id=field_id,
                    expected_draft_version=state.draft_version,
                    draft=updated,
                    summary="助手根据地名查找到天气查询点；请核对行政区和精度后确认。",
                    changes={
                        "天气查询地点": {
                            "原值": state.draft.location.model_dump(mode="json")
                            if state.draft.location
                            else None,
                            "候选值": location.model_dump(mode="json"),
                        }
                    },
                )
                result = {
                    "requires_user_confirmation": True,
                    "proposal": proposal.model_dump(mode="json"),
                }
            elif name == "preview_plan":
                args = PreviewPlanArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                preview = self.workbench.preview_plan(
                    field_id,
                    operation_id=f"agent-{run_id}-preview",
                    source="assistant",
                )
                result = preview.model_dump(mode="json")
                return result, None, preview, "success"
            elif name == "propose_field_update":
                args = ProposeFieldUpdateArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                proposal = self._field_update_proposal(field_id, args.changes)
                result = {
                    "requires_user_confirmation": True,
                    "proposal": proposal.model_dump(mode="json"),
                }
            elif name == "propose_feedback":
                args = ProposeFeedbackArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                proposal = self._feedback_proposal(field_id, args.validated_events())
                result = {
                    "requires_user_confirmation": True,
                    "proposal": proposal.model_dump(mode="json"),
                }
            elif name == "propose_task_series":
                args = ProposeTaskSeriesArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                if args.source_quote not in user_message:
                    raise ValueError("任务提案必须对应本轮用户原话")
                state = self.workbench.get_field(field_id)
                if state.draft.profile is None:
                    raise ValueError("先保存地块档案，再添加农事待办")
                if args.conditions_confirmed and not re.search(
                    r"已确认.*(?:条件|要求)|我确认.*(?:条件|要求)", args.source_quote
                ):
                    raise ValueError("任务条件只能在用户明确确认后标记为已确认")
                series = TaskSeriesV4(
                    id=f"agent-series-{uuid4().hex[:16]}",
                    field_id=field_id,
                    template={
                        "id": f"agent-template-{uuid4().hex[:12]}",
                        "version": 1,
                        "title": args.title,
                        "task_type": args.task_type,
                        "category": args.category,
                        "purpose": args.purpose,
                        "necessary_conditions": args.necessary_conditions,
                        "steps": args.steps,
                        "result_fields": args.result_fields,
                        "completion_standard": args.completion_standard,
                        "follow_up": args.follow_up,
                        "source_kind": "user_defined",
                        "agronomic_review_status": "not_assessed",
                    },
                    recurrence=args.recurrence,
                    anchor_date=args.anchor_date,
                    end_date=args.end_date,
                    selected_dates=args.selected_dates,
                    interval_days=args.interval_days,
                    duration_minutes=args.duration_minutes,
                    priority=args.priority,
                    earliest_date=args.earliest_date,
                    due_date=args.due_date,
                    prerequisites=args.prerequisites,
                    conditions_confirmed=args.conditions_confirmed,
                )
                proposal = AgentProposalV3(
                    kind="task_series",
                    field_id=field_id,
                    expected_draft_version=state.draft_version,
                    expected_fact_version=state.fact_version,
                    expected_task_version=state.task_version,
                    task_series=series,
                    summary="助手提出了一个自定义农事待办；确认后保存并生成计划预览。",
                    changes={
                        "任务名称": series.template.title,
                        "本次目的": series.template.purpose or "暂未填写",
                        "安排频次": series.recurrence,
                        "起始日期": series.anchor_date.isoformat(),
                        "预计时长（分钟）": series.duration_minutes,
                        "农艺状态": "用户自定，农艺适用性未评估",
                    },
                )
                result = {
                    "requires_user_confirmation": True,
                    "proposal": proposal.model_dump(mode="json"),
                }
            else:
                raise ValueError("工具未注册")
            return result, proposal, None, "success"
        except WeatherServiceError as error:
            return (
                {"error": f"weather_{error.code}", "message": str(error)},
                None,
                None,
                "failed",
            )
        except sqlite3.OperationalError:
            return (
                {"error": "database_busy", "message": "数据正在写入，请稍后重试"},
                None,
                None,
                "failed",
            )
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            KeyError,
        ) as error:
            return (
                {"error": "tool_validation_failed", "message": str(error)},
                None,
                None,
                "invalid_arguments",
            )
        except Exception:
            return (
                {"error": "tool_failed", "message": "工具执行失败；已保留已保存内容。"},
                None,
                None,
                "failed",
            )
        finally:
            _ = self._monotonic() - started

    @staticmethod
    def _check_field(active_field_id: str, requested_field_id: str) -> None:
        if requested_field_id != active_field_id:
            raise ValueError("工具只能读取当前选中的地块")

    def _field_update_proposal(self, field_id: str, changes: dict[str, object]) -> AgentProposalV3:
        state = self.workbench.get_field(field_id)
        if state.draft.profile is None:
            raise ValueError("先填写并保存地块基本信息")
        allowed = {
            "name",
            "province",
            "province_confirmed",
            "crop",
            "crop_confirmed",
            "stage",
            "stage_confirmed",
            "field_type",
            "area_mu",
            "inspection_requested",
            "maintenance_need_confirmed",
            "maintenance_problem_confirmed",
            "maintenance_purpose",
            "moisture",
            "ditch_connectivity",
            "standing_water",
            "leaf_count",
            "actual_rain_observed",
            "winter_before_context_confirmed",
            "inspection_duration_minutes",
            "maintenance_duration_minutes",
            "inspection_priority",
            "maintenance_priority",
            "inspection_earliest_date",
            "inspection_due_date",
            "maintenance_earliest_date",
            "maintenance_due_date",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不允许修改这些字段：{', '.join(sorted(unknown))}")
        updated = type(state.draft.profile).model_validate(
            {**state.draft.profile.model_dump(mode="python"), **changes}
        )
        new_draft = state.draft.model_copy(update={"profile": updated})
        differences = {
            key: {
                "old": getattr(state.draft.profile, key),
                "new": getattr(updated, key),
            }
            for key in changes
        }
        proposal = AgentProposalV3(
            kind="field_update",
            field_id=field_id,
            expected_draft_version=state.draft_version,
            draft=new_draft,
            summary="助手提出地块档案修改，确认后才保存。",
            changes=differences,
        )
        for pending in self.workbench.pending_agent_proposals(field_id):
            if (
                pending.kind == "field_update"
                and pending.expected_draft_version == state.draft_version
                and pending.draft == new_draft
            ):
                return pending
        return proposal

    def _feedback_proposal(
        self,
        field_id: str,
        events: list[FactEventInputV3],
    ) -> AgentProposalV3:
        state = self.workbench.get_field(field_id)
        current = self.workbench.get_current_plan(field_id)
        current_items = list(current.items) if current else []
        valid_task_ids = {item.task_id for item in current_items}
        event_ids = {event.event_id for event in self.workbench.list_events(field_id)}
        for event in events:
            feedback = event.feedback
            if feedback and feedback.plot_id not in {None, field_id}:
                raise ValueError("反馈地块与当前选择不一致")
            if feedback and feedback.task_id and feedback.task_id not in valid_task_ids:
                raise ValueError("反馈引用的任务不存在于当前采用版")
            if event.correction_of and event.correction_of not in event_ids:
                raise ValueError("更正引用的现场事件不存在")
            if feedback and feedback.kind == "completed":
                item = next(
                    (task for task in current_items if task.task_id == feedback.task_id), None
                )
                if item is None:
                    raise ValueError("完成反馈引用的任务不存在于当前采用版")
                if item.completed or item.start_at is None:
                    raise ValueError("只能提出当前已安排且尚未完成的任务完成反馈")

        def content(values: list[FactEventInputV3]) -> list[str]:
            return sorted(
                json.dumps(event.model_dump(mode="json", exclude={"event_id"}), sort_keys=True)
                for event in values
            )

        for pending in self.workbench.pending_agent_proposals(field_id):
            if (
                pending.kind == "facts"
                and pending.expected_fact_version == state.fact_version
                and content(pending.events) == content(events)
            ):
                return pending
        return AgentProposalV3(
            kind="facts",
            field_id=field_id,
            expected_fact_version=state.fact_version,
            events=events,
            summary="请核对要保存的现场情况。",
            changes={"event_count": len(events)},
        )

    @staticmethod
    def _tool_summary(result: dict[str, object]) -> str:
        if result.get("error"):
            return str(result.get("message", "工具未完成"))[:200]
        if "proposal" in result:
            proposal = result.get("proposal")
            if isinstance(proposal, dict):
                return str(proposal.get("summary", "等待用户确认"))[:200]
            return "等待用户确认"
        if "items" in result and isinstance(result["items"], list):
            return f"返回 {len(result['items'])} 个候选事项"
        if "days" in result and isinstance(result["days"], list):
            return f"返回 {len(result['days'])} 天天气数据"
        return "已完成本地查询"
