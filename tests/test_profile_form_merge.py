"""SYS-01: re-saving the main field form must preserve unrendered profile fields."""

from __future__ import annotations

from datetime import date

from nongshi.schema_v2 import FieldProfile, WorkabilityConfirmation
from nongshi.workbench import _profile_from_form


def test_main_form_resave_preserves_unrendered_profile_fields() -> None:
    previous = FieldProfile(
        id="field-1",
        name="一号田",
        maintenance_prerequisites=["B-I"],
        completed_maintenance_task_ids=["field-1-I"],
        actual_rain_observed=False,
        workability_by_date={date(2026, 10, 20): WorkabilityConfirmation(site=True)},
    )

    merged = _profile_from_form(previous, {"id": "field-1", "name": "一号田（改名）"})

    assert merged.name == "一号田（改名）"
    assert merged.maintenance_prerequisites == ["B-I"]
    assert merged.completed_maintenance_task_ids == ["field-1-I"]
    assert merged.actual_rain_observed is False
    assert date(2026, 10, 20) in merged.workability_by_date
