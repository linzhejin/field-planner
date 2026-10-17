"""Fixed, explicitly simulated inputs for the local three-plot walkthrough."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from nongshi.schema import DayCapacity, PlanRequest, Plot, RuleCard, Task, WeatherSnapshot


def make_demo_request(start_date: date) -> PlanRequest:
    """Return a repeatable case; no card here is an audited agricultural source."""
    label = "工程演示设定（待核实）"
    return PlanRequest(
        start_date=start_date,
        demo_mode=True,
        plots=[
            Plot(
                id="p1",
                name="一号田",
                region="湖北",
                stage="苗期",
                stage_confirmed=True,
                observations={"ditch_condition": "clear", "field_access": "yes"},
            ),
            Plot(
                id="p2",
                name="二号田",
                region="湖北",
                stage="苗期",
                stage_confirmed=True,
                observations={"ditch_condition": "unknown", "field_access": "yes"},
            ),
            Plot(
                id="p3",
                name="三号田",
                region="湖北",
                stage="苗期",
                stage_confirmed=True,
                observations={"ditch_condition": "clear", "field_access": "yes"},
            ),
        ],
        tasks=[
            Task(
                id="p1-seedling",
                plot_id="p1",
                kind="inspect_seedling",
                title="巡田苗情检查",
                hours=2,
                priority=1,
            ),
            Task(
                id="p2-ditch-inspect",
                plot_id="p2",
                kind="inspect_ditch",
                title="沟渠检查",
                hours=1,
                priority=1,
            ),
            Task(
                id="p2-ditch-maintain",
                plot_id="p2",
                kind="maintain_ditch",
                title="沟渠维护候选任务",
                hours=2,
                priority=1,
                prerequisites=["p2-ditch-inspect"],
            ),
            Task(
                id="p3-seedling",
                plot_id="p3",
                kind="inspect_seedling",
                title="巡田苗情检查",
                hours=2,
                priority=2,
            ),
        ],
        days=[
            DayCapacity(day=start_date + timedelta(days=index), hours=hours)
            for index, hours in enumerate((3, 3, 3, 3, 2, 2, 2))
        ],
        rules=[
            RuleCard(
                id="demo-seedling",
                kind="inspect_seedling",
                region="湖北",
                stage="苗期",
                conditions={"field_access": "yes"},
                source_title=label,
                source_excerpt="演示条件：已由用户确认苗期；仅用于检验流程，不构成农事建议。",
                is_demo=True,
            ),
            RuleCard(
                id="demo-ditch-inspect",
                kind="inspect_ditch",
                region="湖北",
                stage="苗期",
                conditions={"field_access": "yes"},
                source_title=label,
                source_excerpt="演示条件：检查候选任务由用户确认；仅用于检验流程。",
                is_demo=True,
            ),
            RuleCard(
                id="demo-ditch-maintain",
                kind="maintain_ditch",
                region="湖北",
                stage="苗期",
                conditions={"field_access": "yes", "ditch_condition": "blocked"},
                confirmed_observation_fields=["ditch_condition"],
                required_prior_task_kinds=["inspect_ditch"],
                source_title=label,
                source_excerpt="演示条件：现场确认堵塞后才使维护候选任务进入排程；待核对真实依据。",
                is_demo=True,
            ),
        ],
        weather=WeatherSnapshot(
            status="fixed_demo",
            source_label="固定模拟天气",
            issued_at=datetime.combine(start_date, time(8), tzinfo=UTC),
            summary="未设置农业气象阈值；天气仅用于演示数据入口。",
        ),
    )
