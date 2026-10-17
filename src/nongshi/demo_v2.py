"""Repeatable three-field profile for local v2 demonstrations."""

from __future__ import annotations

from datetime import date, time, timedelta

from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FieldProfile, PlanRequestV2, TimeWindow


def make_demo_request_v2(start_date: date) -> PlanRequestV2:
    catalog = load_rule_catalog()
    return PlanRequestV2(
        start_date=start_date,
        plots=[
            FieldProfile(
                id="A",
                name="一号模拟地",
                province="湖北",
                province_confirmed=True,
                stage="苗期",
                stage_confirmed=True,
                field_type="lowland_rice_stubble",
                area_mu=3,
                inspection_requested=True,
                maintenance_need_confirmed=True,
                moisture="wet",
                inspection_duration_minutes=30,
                maintenance_duration_minutes=90,
                inspection_priority=1,
                maintenance_priority=1,
                observation_source="synthetic",
            ),
            FieldProfile(
                id="B",
                name="二号模拟地",
                province="湖北",
                province_confirmed=True,
                stage="苗期",
                stage_confirmed=True,
                field_type="lowland_rice_stubble",
                area_mu=2,
                inspection_requested=True,
                maintenance_need_confirmed=None,
                inspection_duration_minutes=30,
                maintenance_duration_minutes=60,
                observation_source="synthetic",
            ),
            FieldProfile(
                id="C",
                name="三号模拟地",
                province="湖北",
                province_confirmed=True,
                stage="苗期",
                stage_confirmed=True,
                field_type="dry_hilly",
                area_mu=2,
                inspection_requested=True,
                maintenance_need_confirmed=False,
                moisture="dry",
                ditch_connectivity="clear",
                standing_water=False,
                inspection_duration_minutes=30,
                maintenance_duration_minutes=60,
                observation_source="synthetic",
            ),
        ],
        days=[
            DayWindow(
                day=start_date + timedelta(days=offset),
                site_workable=None,
                weather_workable=None,
                resource_minutes=120,
                intervals=[TimeWindow(start=time(9), end=time(11))],
            )
            for offset in range(7)
        ],
        **catalog.plan_metadata(),
        selected_rule_ids=sorted(catalog.development_rule_ids),
        synthetic=True,
    )
