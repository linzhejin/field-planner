"""Run a labelled synthetic-farm flow with live DeepSeek and Open-Meteo calls.

The database is temporary. The JSON report contains no credentials or private farm data.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from nongshi.agent import AgentServiceV3
from nongshi.config import Settings
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.schema_v2 import LOCAL_ZONE, DayWindow, FeedbackV2, FieldProfile, TimeWindow
from nongshi.schema_v3 import FactEventInputV3, FieldDraftV3, RuleSearchQueryV3
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.weather import OpenMeteoService, WeatherServiceError


def run(env_file: Path) -> dict:
    settings = Settings(_env_file=env_file)
    started = time.perf_counter()
    observed_at = datetime.now(UTC)
    start_date = observed_at.astimezone(LOCAL_ZONE).date() + timedelta(days=1)
    field_id = "public-synthetic-acceptance"
    report: dict = {
        "classification": "公开模拟农情；模型与天气为真实接口；非农艺或独立评测",
        "started_at": observed_at.isoformat(),
        "model": settings.llm_model,
        "input": {
            "field_name": "公开模拟油菜田",
            "province": "湖北",
            "crop": "油菜",
            "stage": "苗期",
            "start_date": start_date.isoformat(),
            "duration_source": "模拟用户估计：巡田30分钟，沟渠清理45分钟",
            "site_and_weather_workability": "模拟用户逐日确认，非预报推定",
        },
        "steps": {},
        "errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="nongshi_phase_acceptance_") as directory:
        database = Path(directory) / "plans.sqlite3"
        store = WorkbenchStoreV3(database)
        service = WorkbenchServiceV3(store)
        blank = FieldDraftV3(
            field_id=field_id,
            mode="demo",
            profile=FieldProfile(id=field_id, name="公开模拟油菜田"),
            start_date=start_date,
            input_source="demo",
        )
        state = service.save_field_draft(blank, expected_version=None, operation_id="accept-create")
        report["steps"]["blank_saved"] = state.draft_version

        agent = AgentServiceV3(settings, service)
        prompt = (
            "请把这块公开模拟田补充为：湖北省、油菜、苗期、低洼稻茬田，"
            "这些信息由用户确认；我要求巡田观察苗情，估计需30分钟。"
            "请调用 propose_field_update 提出档案修改，我确认后保存。"
        )
        answer = agent.run(field_id, prompt, received_at=observed_at)
        model_runs = store.list_agent_runs(field_id)
        report["steps"]["natural_language"] = {
            "status": answer.status,
            "model_requests": answer.request_count,
            "tools": [{"name": row.name, "status": row.status} for row in answer.tools],
            "prompt_tokens": answer.prompt_tokens,
            "completion_tokens": answer.completion_tokens,
            "proposal_count": len(answer.proposals),
            "elapsed_ms": model_runs[-1].elapsed_ms if model_runs else None,
            "error": answer.error_message,
        }
        proposal = next((row for row in answer.proposals if row.kind == "field_update"), None)
        if proposal and proposal.draft:
            updated = service.save_field_draft(
                proposal.draft,
                expected_version=proposal.expected_draft_version,
                operation_id="accept-confirm-proposal",
            )
            report["steps"]["proposal_confirmed"] = True
        else:
            updated = service.get_field(field_id)
            report["steps"]["proposal_confirmed"] = False
        agent.close()

        # The structured form remains available if the provider did not return all fields.
        profile = updated.draft.profile.model_copy(
            update={
                "province": "湖北",
                "province_confirmed": True,
                "crop": "油菜",
                "crop_confirmed": True,
                "stage": "苗期",
                "stage_confirmed": True,
                "field_type": "lowland_rice_stubble",
                "inspection_requested": True,
                "inspection_duration_minutes": 30,
                "maintenance_duration_minutes": 45,
            }
        )
        days = [
            DayWindow(
                day=start_date + timedelta(days=index),
                site_workable=True,
                weather_workable=True,
                resource_minutes=120,
                intervals=[TimeWindow(start=clock_time(9), end=clock_time(11))],
            )
            for index in range(7)
        ]
        weather = OpenMeteoService(store=store)
        try:
            candidates = weather.search_locations("武汉市洪山区")
            choice = next((row for row in candidates if "洪山" in row.label), None)
            if choice is None:
                raise ValueError("没有可由用户确认的洪山查询点")
            location = weather.select_location(candidates, choice.id)
            report["steps"]["location_selected"] = {
                "display_name": location.display_name,
                "resolution": location.resolution,
                "geocoding_source": location.geocoding_source,
            }
            revised = updated.draft.model_copy(
                update={"profile": profile, "days": days, "location": location}
            )
            updated = service.save_field_draft(
                revised,
                expected_version=updated.draft_version,
                operation_id="accept-confirm-location-and-resources",
            )
            snapshot = weather.fetch_forecast(
                field_id,
                location,
                start_date,
                start_date + timedelta(days=6),
                force_refresh=True,
            )
            report["steps"]["weather"] = {
                "status": "success",
                "source": snapshot.source,
                "coverage_start": snapshot.coverage_start.isoformat(),
                "coverage_end": snapshot.coverage_end.isoformat(),
                "days": len(snapshot.days),
                "timezone": snapshot.timezone,
                "fetched_at": snapshot.fetched_at.isoformat(),
                "grid_latitude": snapshot.grid_latitude,
                "grid_longitude": snapshot.grid_longitude,
                "units": snapshot.units,
            }
        except (WeatherServiceError, ValueError) as error:
            report["steps"]["weather"] = {"status": "failed", "reason": str(error)}
            if updated.draft.days == []:
                updated = service.save_field_draft(
                    updated.draft.model_copy(update={"profile": profile, "days": days}),
                    expected_version=updated.draft_version,
                    operation_id="accept-structured-fallback",
                )
        finally:
            weather.close()

        query = RuleSearchQueryV3(
            province="湖北",
            province_confirmed=True,
            crop="油菜",
            crop_confirmed=True,
            stage="苗期",
            stage_confirmed=True,
            field_type="lowland_rice_stubble",
            task_type="inspection",
        )
        retrieval = RuleRetrieverV3().search(query)
        report["steps"]["rules"] = {
            "status": retrieval.status,
            "roles": sorted({e.usage_role for hit in retrieval.hits for e in hit.evidence}),
        }
        first = service.preview_plan(field_id, operation_id="accept-preview-v1", source="demo")
        v1 = service.adopt_plan(
            first.id,
            expected_fact_version=first.expected_fact_version,
            expected_plan_id=first.expected_plan_id,
            operation_id="accept-adopt-v1",
        )
        inspection = next(item for item in v1.version.items if item.task_type == "inspection")
        report["steps"]["v1"] = {
            "adopted": True,
            "version_id": v1.version.id,
            "scheduled_inspection": inspection.start_at is not None,
        }

        if inspection.end_at:
            facts = [
                FactEventInputV3(
                    feedback=FeedbackV2(
                        kind="completed", task_id=inspection.task_id, at=inspection.end_at
                    )
                ),
                FactEventInputV3(
                    feedback=FeedbackV2(
                        kind="observation",
                        plot_id=field_id,
                        field="ditch_connectivity",
                        value="impaired",
                        at=inspection.end_at + timedelta(minutes=1),
                    )
                ),
            ]
            committed = service.commit_events(
                field_id,
                facts,
                operation_id="accept-feedback",
                expected_fact_version=service.get_field(field_id).fact_version,
            )
            report["steps"]["feedback"] = {
                "fact_version": committed.fact_version,
                "event_count": len(committed.events),
                "plan_stale": committed.plan_stale,
            }
            state = service.get_field(field_id)
            next_days = [row.model_copy(deep=True) for row in state.draft.days]
            next_days[0].resource_minutes = 60
            next_days[0].intervals = [TimeWindow(start=clock_time(10), end=clock_time(11))]
            service.save_field_draft(
                state.draft.model_copy(update={"days": next_days}),
                expected_version=state.draft_version,
                operation_id="accept-reduce-time",
            )
            second = service.preview_plan(field_id, operation_id="accept-preview-v2", source="demo")
            v2 = service.adopt_plan(
                second.id,
                expected_fact_version=second.expected_fact_version,
                expected_plan_id=second.expected_plan_id,
                operation_id="accept-adopt-v2",
            )
            reopened = WorkbenchServiceV3(WorkbenchStoreV3(database))
            payload = json.loads(reopened.export_json(field_id))
            report["steps"]["v2"] = {
                "adopted": True,
                "version_id": v2.version.id,
                "history_count": len(reopened.plan_history(field_id)),
                "completed_locked": any(
                    item.task_id == inspection.task_id and item.completed
                    for item in v2.version.items
                ),
                "reopened_fact_version": reopened.get_field(field_id).fact_version,
                "json_export_history_count": len(payload.get("history", [])),
                "markdown_export_bytes": len(reopened.export_markdown(field_id).encode("utf-8")),
            }
        for index in range(2, 5):
            extra_id = f"public-synthetic-extra-{index}"
            service.save_field_draft(
                FieldDraftV3(
                    field_id=extra_id,
                    mode="demo",
                    input_source="demo",
                    profile=FieldProfile(id=extra_id, name=f"公开模拟田{index}"),
                ),
                expected_version=None,
                operation_id=f"accept-extra-{index}",
            )
        report["steps"]["four_fields"] = {
            "count": len(service.list_fields(mode="demo")),
            "other_plan_history_empty": all(
                not service.plan_history(f"public-synthetic-extra-{index}") for index in range(2, 5)
            ),
        }
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    report["passed"] = bool(
        report["steps"]["natural_language"]["status"] == "success"
        and report["steps"]["natural_language"]["tools"]
        and report["steps"]["proposal_confirmed"]
        and report["steps"]["weather"]["status"] == "success"
        and report["steps"]["v2"]["history_count"] == 2
        and report["steps"]["v2"]["completed_locked"]
        and report["steps"]["four_fields"]["count"] == 4
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.env_file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"passed": report["passed"], "elapsed_ms": report["elapsed_ms"]}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
