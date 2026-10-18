"""DeepSeek Chat Completions orchestration with bounded, validated local tools."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nongshi.config import Settings
from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.schema_v2 import LOCAL_ZONE
from nongshi.schema_v3 import (
    AgentProposalV3,
    AgentResponseV3,
    AgentRunV3,
    AgentToolMetricV3,
    AgentToolTraceV3,
    FactEventInputV3,
    PlanPreviewV3,
    RuleSearchQueryV3,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.weather import OpenMeteoService, WeatherServiceError

MAX_MODEL_REQUESTS = 3
MAX_TOOL_EXECUTIONS = 6


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


class PreviewPlanArgs(ToolArgs):
    field_id: str


class ProposeFieldUpdateArgs(ToolArgs):
    field_id: str
    changes: dict[str, Any] = Field(min_length=1)


class ProposeFeedbackArgs(ToolArgs):
    field_id: str
    events: list[FactEventInputV3] = Field(min_length=1, max_length=10)


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


TOOL_DEFINITIONS: list[dict[str, object]] = [
    _tool("read_field", "读取指定地块当前已保存的档案与版本状态。", ReadFieldArgs),
    _tool(
        "search_rules", "按地区、作物、生育期、田型和任务筛选有来源的规则候选。", SearchRulesArgs
    ),
    _tool("evaluate_field", "计算当前地块候选事项、四层状态、缺项与规则证据。", EvaluateFieldArgs),
    _tool(
        "query_weather",
        "查询已选择地点的逐日天气；只提供风险信息，不判断现场能否作业。",
        QueryWeatherArgs,
    ),
    _tool("preview_plan", "根据已保存的输入生成计划预览；不会采用或覆盖计划。", PreviewPlanArgs),
    _tool("propose_field_update", "提出档案修改，须由用户确认后才会保存。", ProposeFieldUpdateArgs),
    _tool("propose_feedback", "提出现场事实修改，须由用户确认后才会记录。", ProposeFeedbackArgs),
]


class AgentServiceV3:
    def __init__(
        self,
        settings: Settings,
        workbench: WorkbenchServiceV3,
        *,
        weather: OpenMeteoService | None = None,
        transport: httpx.BaseTransport | None = None,
        monotonic=time.perf_counter,
    ) -> None:
        self.settings = settings
        self.workbench = workbench
        self.weather = weather or OpenMeteoService(
            store=workbench.store,
            timeout_seconds=settings.weather_timeout_seconds,
        )
        self._client = httpx.Client(
            transport=transport,
            timeout=settings.llm_timeout_seconds,
            follow_redirects=True,
        )
        self._monotonic = monotonic
        self._retriever = RuleRetrieverV3(workbench.catalog)

    def close(self) -> None:
        self._client.close()
        self.weather.close()

    def run(
        self,
        field_id: str,
        user_message: str,
        *,
        conversation: list[dict[str, str]] | None = None,
    ) -> AgentResponseV3:
        self.workbench.get_field(field_id)
        run_id = str(uuid4())
        started_at = datetime.now(UTC)
        started = self._monotonic()
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

        def finish(
            status: str,
            *,
            text: str = "",
            error_message: str = "",
        ) -> AgentResponseV3:
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
            )
            self.workbench.store.save_agent_run(run)
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
            )

        if self.settings.llm_provider.strip().casefold() != "deepseek":
            return finish("error", error_message="当前只配置了 DeepSeek Chat Completions。")
        api_key = self.settings.llm_api_key.get_secret_value().strip()
        if not api_key:
            return finish(
                "missing_credentials",
                error_message="模型密钥尚未配置；可以继续使用下方的结构化表单。",
            )
        if not user_message.strip():
            return finish("invalid_response", error_message="请输入要处理的内容。")

        messages: list[dict[str, object]] = [
            {
                "role": "system",
                "content": self._system_message(
                    field_id, received_at=started_at.astimezone(LOCAL_ZONE)
                ),
            }
        ]
        for item in (conversation or [])[-8:]:
            if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
                messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": user_message})
        deadline = started + self.settings.llm_timeout_seconds
        last_tool_summary = ""
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"

        for _ in range(MAX_MODEL_REQUESTS):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return finish(
                    "timeout",
                    text=last_tool_summary,
                    error_message="本轮交互超过 20 秒；已保留工具结果和待确认草稿，可重试。",
                )
            request_count += 1
            try:
                response = self._client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": TOOL_DEFINITIONS,
                        "tool_choice": "auto",
                        "thinking": {"type": "disabled"},
                    },
                    timeout=min(remaining, self.settings.llm_timeout_seconds),
                )
                if response.status_code >= 400:
                    status, message = self._provider_error(response.status_code)
                    return finish(status, text=last_tool_summary, error_message=message)
                payload = response.json()
                completion = ModelCompletion.model_validate(payload)
            except httpx.TimeoutException:
                return finish(
                    "timeout",
                    text=last_tool_summary,
                    error_message="模型请求超时；已保留已保存事实和待确认草稿。",
                )
            except httpx.RequestError:
                return finish("error", text=last_tool_summary, error_message="模型服务暂不可用。")
            except (ValueError, ValidationError):
                return finish(
                    "invalid_response",
                    text=last_tool_summary,
                    error_message="模型响应格式无效；可改用结构化表单继续。",
                )

            if completion.usage:
                token_usage_seen = True
                prompt_tokens += completion.usage.prompt_tokens or 0
                completion_tokens += completion.usage.completion_tokens or 0
            message = completion.choices[0].message
            if not message.tool_calls:
                if not message.content:
                    return finish(
                        "invalid_response",
                        text=last_tool_summary,
                        error_message="模型没有返回说明或工具结果。",
                    )
                return finish(
                    "success",
                    text=self._safe_explanation(message.content.strip()),
                )

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
            for call in message.tool_calls:
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
                else:
                    tool_count += 1
                    tool_names.append(call.function.name)
                    tool_started = self._monotonic()
                    tool_result, proposal, preview, trace_status = self._execute_tool(
                        call.function.name,
                        call.function.arguments,
                        field_id=field_id,
                        run_id=run_id,
                    )
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
                        )
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                    }
                )

        return finish(
            "error",
            text=last_tool_summary,
            error_message="本轮最多请求模型三次；工具结果已保留，请检查后重试。",
        )

    @staticmethod
    def _system_message(field_id: str, *, received_at: datetime) -> str:
        return (
            "你是农时窗口的中文单地块助手。当前地块 ID 是 "
            f"{field_id}。本轮接收时间为北京时间 {received_at:%Y-%m-%d %H:%M}。"
            "只能根据本地工具结果解释任务、依据、天气和排程。"
            "先读取或核对状态，再按需调用天气、规则、评估或计划预览工具。"
            "最多提出三条影响当前判断的问题。"
            "相对日期只按本轮接收时的北京时间换算，并在待确认修改里写出完整日期；"
            "不能猜测缺失的日期。"
            "涉及档案或现场事实的修改只能调用 propose_* 工具，必须向用户逐项说明并等待确认；"
            "不能声称草稿已保存、事实已记录或计划已采用。不能编造任务、工时、天气、来源和农艺结论。"
            "未知保留为未知。天气预报不代表现场可作业。候选规则标记为开发验证、适用性待复核。"
            "最终只做简短解释，不另写一份任务列表；任务、日期、时段、状态和来源都由本地工具结果展示。"
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
    ) -> tuple[dict[str, object], AgentProposalV3 | None, PlanPreviewV3 | None, str]:
        started = self._monotonic()
        try:
            raw_arguments = json.loads(arguments_json)
            if not isinstance(raw_arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            proposal: AgentProposalV3 | None = None
            if name == "read_field":
                args = ReadFieldArgs.model_validate(raw_arguments)
                self._check_field(field_id, args.field_id)
                result = self.workbench.get_field(field_id).model_dump(mode="json")
            elif name == "search_rules":
                args = SearchRulesArgs.model_validate(raw_arguments)
                result = self._retriever.search(args.query).model_dump(mode="json")
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
                )
                result = snapshot.model_dump(mode="json")
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
                proposal = self._feedback_proposal(field_id, args.events)
                result = {
                    "requires_user_confirmation": True,
                    "proposal": proposal.model_dump(mode="json"),
                }
            else:
                raise ValueError("工具未注册")
            return result, proposal, None, "success"
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            KeyError,
            WeatherServiceError,
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
        return AgentProposalV3(
            kind="field_update",
            field_id=field_id,
            expected_draft_version=state.draft_version,
            draft=new_draft,
            summary="助手提出地块档案修改，确认后才保存。",
            changes=differences,
        )

    def _feedback_proposal(
        self,
        field_id: str,
        events: list[FactEventInputV3],
    ) -> AgentProposalV3:
        state = self.workbench.get_field(field_id)
        current = self.workbench.get_current_plan(field_id)
        valid_task_ids = {item.task_id for item in current.items} if current else set()
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
                item = (
                    next((task for task in current.items if task.task_id == feedback.task_id), None)
                    if current
                    else None
                )
                if item is None:
                    raise ValueError("完成反馈引用的任务不存在于当前采用版")
                if item.completed or item.start_at is None:
                    raise ValueError("只能提出当前已安排且尚未完成的任务完成反馈")
        return AgentProposalV3(
            kind="facts",
            field_id=field_id,
            expected_fact_version=state.fact_version,
            events=events,
            summary="助手拆分出现场事实，确认后才记录。",
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
